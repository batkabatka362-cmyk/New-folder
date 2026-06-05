"""Honest realized-PnL reconstruction from the durable `trades` table (P8 gap #1/#2).

Why this exists: the in-memory portfolio stats reset on restart, `trade_outcomes` only logs post-P2
trades, and — most dangerously — ONE pricing-glitch fill (a curve-priced buy crossing to a
DexScreener-priced sell at a different SOL scale) can be ~100% of all reported profit. The go-live
readiness gate must NOT read that fabricated number.

This rebuilds realized PnL per FULLY-CLOSED mint from every logged buy/sell, FLAGS glitch round-trips
(proceeds above a sane multiple of cost), and reports the book BOTH with and without the glitches —
plus a winsorized view — so you can see the real edge, not the artifact.

  python -m memebot.backtest.pnl_reconstruct            # honest realized book from memebot.db
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics

from ..config import get_settings


def reconstruct(trades: list, *, glitch_mult: float = 20.0, closed_frac: float = 0.99) -> list[dict]:
    """Aggregate (mint, side, sol, tokens) trade rows per mint into realized round-trips.

    Only mints whose SOLD tokens >= closed_frac of BOUGHT tokens are counted (a still-open position
    hasn't realized its remainder, so including it would understate/mislead). A round-trip whose
    proceeds exceed `glitch_mult`x its cost is flagged `glitch` (a pricing-feed artifact, not alpha).
    """
    from collections import defaultdict
    agg: dict[str, dict] = defaultdict(lambda: {"buy_sol": 0.0, "sell_sol": 0.0,
                                                 "buy_tok": 0.0, "sell_tok": 0.0, "n_buy": 0, "n_sell": 0})
    for mint, side, sol, tokens in trades:
        a = agg[mint]
        if side == "buy":
            a["buy_sol"] += sol; a["buy_tok"] += tokens; a["n_buy"] += 1
        else:
            a["sell_sol"] += sol; a["sell_tok"] += tokens; a["n_sell"] += 1
    out = []
    for mint, a in agg.items():
        if a["n_sell"] == 0 or a["buy_sol"] <= 0 or a["buy_tok"] <= 0:
            continue                                   # never sold / no cost basis -> not a round-trip
        if a["sell_tok"] < a["buy_tok"] * closed_frac:
            continue                                   # still substantially OPEN -> don't book it yet
        pnl = a["sell_sol"] - a["buy_sol"]
        ret_mult = a["sell_sol"] / a["buy_sol"]
        out.append({"mint": mint, "cost": a["buy_sol"], "proceeds": a["sell_sol"], "pnl": pnl,
                    "ret_mult": ret_mult, "glitch": glitch_mult > 0 and ret_mult > glitch_mult})
    return out


def _stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "net": 0.0, "median": 0.0, "wins": 0, "losses": 0, "win_rate": 0.0, "profit_factor": 0.0}
    pnls = [r["pnl"] for r in rows]
    wins = [p for p in pnls if p > 0]
    gross_win, gross_loss = sum(wins), -sum(p for p in pnls if p < 0)
    return {
        "n": len(rows), "net": sum(pnls), "median": statistics.median(pnls),
        "wins": len(wins), "losses": sum(1 for p in pnls if p < 0),
        "win_rate": len(wins) / len(rows),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
    }


def summarize(rows: list[dict]) -> dict:
    """The honest book three ways: ALL round-trips, CLEAN (glitches removed), and WINSORIZED
    (glitch returns capped at the median non-glitch winner). Plus the flagged glitch list."""
    clean = [r for r in rows if not r["glitch"]]
    glitches = [r for r in rows if r["glitch"]]
    win_pnls = [r["pnl"] for r in clean if r["pnl"] > 0]
    cap = statistics.median(win_pnls) if win_pnls else 0.0
    winsor = clean + [{**r, "pnl": cap} for r in glitches]      # replace each glitch pnl with the cap
    return {
        "all": _stats(rows), "clean": _stats(clean), "winsorized": _stats(winsor),
        "glitches": [{"mint": r["mint"], "pnl": r["pnl"], "ret_mult": r["ret_mult"]} for r in glitches],
    }


def _load_trades(path: str) -> list:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT mint, side, sol, tokens FROM trades").fetchall()
    finally:
        conn.close()


def _fmt(label: str, s: dict) -> str:
    pf = "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
    return (f"  {label:11} n={s['n']:>3}  net={s['net']:+.3f} SOL  median={s['median']:+.4f}  "
            f"win={s['win_rate'] * 100:>4.0f}% ({s['wins']}W/{s['losses']}L)  pf={pf}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Honest realized-PnL reconstruction from the trades table.")
    ap.add_argument("--db", default=None)
    ap.add_argument("--glitch-mult", type=float, default=20.0, help="proceeds > this x cost = a pricing glitch")
    args = ap.parse_args()
    rows = reconstruct(_load_trades(args.db or get_settings().db_path), glitch_mult=args.glitch_mult)
    if not rows:
        print("No fully-closed round-trips in the trades table yet.")
        return
    s = summarize(rows)
    # NOTE: stats are PER-MINT-NET (all of a mint's buys/sells aggregated into one round-trip), not
    # per-trade — so `net` is the honest realized total, but win_rate/pf/median are per-mint, not
    # per-fill. A mint sold for more tokens than it was bought (multi-entry / rounding) can also
    # slightly inflate its net (proceeds from tokens with no logged cost basis).
    print(f"\nHonest realized book ({s['all']['n']} closed mints, per-mint-net):")
    print(_fmt("ALL", s["all"]))
    print(_fmt("CLEAN", s["clean"]), " <- glitches removed (the number to trust)")
    print(_fmt("WINSORIZED", s["winsorized"]), " <- glitches capped at the median winner")
    if s["glitches"]:
        print(f"\n  {len(s['glitches'])} PRICING-GLITCH round-trip(s) excluded (curve-buy vs DexScreener-sell scale):")
        for g in sorted(s["glitches"], key=lambda x: x["pnl"], reverse=True)[:5]:
            print(f"    {g['mint'][:10]}  pnl {g['pnl']:+.2f} SOL  ({g['ret_mult']:.0f}x return — not alpha)")
    print("\nThe go-live gate should read CLEAN, never ALL. A flat/negative CLEAN book = edge unproven.")


if __name__ == "__main__":
    main()
