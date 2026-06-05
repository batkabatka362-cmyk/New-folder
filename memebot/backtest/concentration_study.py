"""W4 — does holder concentration actually separate RUGS from WINNERS?

The honest test the W3/W4 hypothesis needs: join each CLOSED paper trade's ENTRY top-5 holder
concentration (captured in `trade_outcomes.entry_features` at buy time — real, not a proxy) to its
REALIZED PnL, and compare winners vs losers. If losers are meaningfully MORE concentrated, a veto
near the winner side is justified; if the two overlap, static concentration is NOT the rug filter
(fresh pump.fun tokens are uniformly ~82-96% concentrated) and it should stay advisory.

    python -m memebot.backtest.concentration_study

Pure `concentration_split` is unit-tested; the CLI prints the split + an honest verdict. Needs a
handful of closed trades WITH known entry concentration (concentration only populates on a Helius
RPC — see W4), so run it after the bot has traded for a while with HELIUS_RPC_URL set.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics

from ..config import get_settings


def concentration_split(rows: list[tuple[float, float]]) -> dict:
    """rows = [(entry_top5_concentration_pct, realized_pnl_sol), ...] for closed trades with KNOWN
    entry concentration. Returns winner vs loser concentration medians and the SEPARATION
    (loser_median - winner_median): positive => losers run MORE concentrated => concentration is a
    usable rug tell (veto/penalty near the winner side); ~0 or negative => it does NOT separate."""
    wins = sorted(c for c, p in rows if p > 0)
    losses = sorted(c for c, p in rows if p <= 0)
    wm = statistics.median(wins) if wins else None
    lm = statistics.median(losses) if losses else None
    sep = (lm - wm) if (wm is not None and lm is not None) else None
    return {
        "n": len(rows), "winners": len(wins), "losers": len(losses),
        "winner_median_conc": (round(wm, 1) if wm is not None else None),
        "loser_median_conc": (round(lm, 1) if lm is not None else None),
        "winner_max_conc": (round(wins[-1], 1) if wins else None),
        "loser_min_conc": (round(losses[0], 1) if losses else None),
        "separation": (round(sep, 1) if sep is not None else None),
    }


def verdict(r: dict, min_each: int = 5, min_sep: float = 5.0) -> str:
    """Honest read of the split (only conclusive with enough of BOTH classes)."""
    if r["winners"] < min_each or r["losers"] < min_each:
        return (f"INCONCLUSIVE -- need >= {min_each} of each class with known entry concentration "
                f"(have {r['winners']}W / {r['losers']}L). Keep accruing; do NOT re-tune the veto yet.")
    sep = r["separation"] or 0.0
    if sep >= min_sep:
        return (f"SEPARATES: losers run {sep:.0f}pp more concentrated than winners. A veto between "
                f"winner_max ({r['winner_max_conc']}) and loser_min ({r['loser_min_conc']}) could cut "
                "rugs without blocking winners — verify the band is clean before tightening.")
    return ("NO clean separation: winners and losers overlap on entry concentration -> static "
            "concentration is NOT the rug filter on free-tier memecoins. Keep it advisory; the "
            "residual rugs are a structural cost (real fix = G3 real-time curve data).")


def load_rows(db_path: str) -> list[tuple[float, float]]:
    conn = sqlite3.connect(db_path)
    try:
        raw = conn.execute("SELECT pnl_sol, entry_features FROM trade_outcomes").fetchall()
    finally:
        conn.close()
    out = []
    for pnl, ef in raw:
        try:
            conc = json.loads(ef or "{}").get("top5_concentration_pct")
        except (TypeError, ValueError):
            conc = None
        if conc is not None and conc >= 0:
            out.append((float(conc), float(pnl)))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Test whether entry holder concentration separates rugs from winners.")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    rows = load_rows(args.db or get_settings().db_path)
    r = concentration_split(rows)
    print("== concentration vs realized outcome (closed trades, known entry concentration) ==")
    print(f"  n={r['n']}  winners={r['winners']} (median conc {r['winner_median_conc']}, max {r['winner_max_conc']})  "
          f"losers={r['losers']} (median conc {r['loser_median_conc']}, min {r['loser_min_conc']})")
    print(f"  separation (loser_med - winner_med) = {r['separation']}")
    print("\nverdict:", verdict(r))


if __name__ == "__main__":
    main()
