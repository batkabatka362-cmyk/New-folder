"""Selectivity-vs-loss-avoided curve (P10b #15) — "how much does raising the bar actually save?".

A COUNTERFACTUAL over the trades we ACTUALLY took and closed: sweep a hypothetical entry-score
threshold upward and, at each step, report what we would have kept vs dropped. Because defense in
this game is in SELECTIVITY (not tighter stops, per RESEARCH.md), the question "would a higher bar
have net-helped?" is the one the loss-minimisation thesis lives or dies on — yet it was unmeasured.

This is strictly a re-threshold of ALREADY-REALIZED outcomes (entry_score -> realized pnl_sol from
the trade_outcomes table). No look-ahead: it never invents trades we didn't take, and it is ADVISORY
output ONLY — it must NEVER auto-adjust entry_threshold or any deterministic gate. Glitch round-trips
(>20x, the same pricing-glitch class pnl_reconstruct excludes) are dropped so one bad fill can't skew
the curve. Read-only; dependency-free.

  python -m memebot.backtest.selectivity            # uses DB_PATH from .env
  python -m memebot.backtest.selectivity --steps 12
"""
from __future__ import annotations

import argparse
import sqlite3

from ..config import get_settings

# the same pricing-glitch ceiling the honest book uses (proceeds/cost > 20x = SOL-scale mismatch, not alpha)
_GLITCH_RET_MULT = 20.0


def _load_outcomes(db_path: str) -> list[tuple[float, float]]:
    """(entry_score, pnl_sol) for every CLEAN closed round-trip. Glitch round-trips excluded via pnl_pct."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT entry_score, pnl_sol, pnl_pct FROM trade_outcomes "
            "WHERE entry_score IS NOT NULL AND pnl_sol IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    out = []
    for score, pnl, pct in rows:
        # pnl_pct = pnl/cost, so proceeds/cost = 1 + pnl_pct; drop >20x as a pricing glitch
        if pct is not None and (1.0 + float(pct)) > _GLITCH_RET_MULT:
            continue
        out.append((float(score), float(pnl)))
    return out


def sweep(outcomes: list[tuple[float, float]], steps: int = 10) -> list[dict]:
    """For each threshold from the current floor up to the max observed entry_score, the counterfactual
    book of trades with entry_score >= T: retained count, net retained PnL, losers avoided / winners
    foregone among the DROPPED trades. Pure; returns [] on no data."""
    if not outcomes:
        return []
    scores = [s for s, _ in outcomes]
    lo, hi = min(scores), max(scores)
    n = len(outcomes)
    base_net = sum(p for _, p in outcomes)
    table = []
    span = max(1e-9, hi - lo)
    for i in range(steps + 1):
        t = lo + span * i / steps
        retained = [(s, p) for s, p in outcomes if s >= t]
        dropped = [(s, p) for s, p in outcomes if s < t]
        net_ret = sum(p for _, p in retained)
        losers_avoided = sum(1 for _, p in dropped if p < 0)
        winners_foregone = sum(1 for _, p in dropped if p > 0)
        loss_avoided_sol = -sum(p for _, p in dropped if p < 0)   # SOL we would NOT have lost
        gain_foregone_sol = sum(p for _, p in dropped if p > 0)   # ...minus the upside we'd skip
        table.append({
            "threshold": t,
            "retained": len(retained),
            "retained_frac": len(retained) / n,
            "net_retained_sol": net_ret,
            "delta_vs_base_sol": net_ret - base_net,         # how much the higher bar changes the book
            "losers_avoided": losers_avoided,
            "winners_foregone": winners_foregone,
            "loss_avoided_sol": loss_avoided_sol,
            "gain_foregone_sol": gain_foregone_sol,
        })
    return table


def main() -> None:
    ap = argparse.ArgumentParser(description="Counterfactual selectivity-vs-loss-avoided curve (ADVISORY only).")
    ap.add_argument("--steps", type=int, default=10, help="threshold sweep granularity")
    args = ap.parse_args()
    s = get_settings()
    outcomes = _load_outcomes(s.db_path)
    if not outcomes:
        print("no closed trades with entry_score yet (need realized trade_outcomes rows). "
              "Let the paper bot accumulate closed trades, then re-run.")
        return
    n = len(outcomes)
    base = sum(p for _, p in outcomes)
    print(f"Selectivity counterfactual over {n} CLEAN closed trades | current net = {base:+.4f} SOL")
    print("(ADVISORY — a re-threshold of trades already taken; never auto-applied to the live gate)\n")
    print(f"  {'thresh':>7} {'kept':>6} {'kept%':>6} {'net_SOL':>9} {'vs_now':>9} "
          f"{'losers_avd':>10} {'wins_4gone':>10} {'loss_avd':>9} {'gain_4gone':>10}")
    for r in sweep(outcomes, args.steps):
        print(f"  {r['threshold']:>7.3f} {r['retained']:>6d} {r['retained_frac']*100:>5.0f}% "
              f"{r['net_retained_sol']:>+9.4f} {r['delta_vs_base_sol']:>+9.4f} "
              f"{r['losers_avoided']:>10d} {r['winners_foregone']:>10d} "
              f"{r['loss_avoided_sol']:>+9.4f} {r['gain_foregone_sol']:>+10.4f}")
    best = max(sweep(outcomes, args.steps), key=lambda r: r["net_retained_sol"])
    print(f"\n  best counterfactual net at threshold {best['threshold']:.3f}: {best['net_retained_sol']:+.4f} SOL "
          f"({best['delta_vs_base_sol']:+.4f} vs the current book). ADVISORY — calibrate, do not auto-apply.")


if __name__ == "__main__":
    main()
