"""Synthetic price-path simulator for EXIT logic.

The forward-return backtest (simulate.py) cannot test TP / SL / breakeven / trail
because it only knows the price "now", not the intrabar path the position lived
through. This module runs hand-built price paths through `Position.should_exit`
so exit/risk parameters can be compared deterministically — e.g. the current
profile vs a defensive (capital-preservation) one.

    python -m memebot.backtest.scenarios             # current params, both modes
    python -m memebot.backtest.scenarios --compare   # current vs defensive, side by side

It is illustrative, not predictive: the scenarios are representative memecoin
shapes (moon, pump-dump, slow bleed, chop, rug, quick scalp, fakeout-recover),
not a sample of real outcomes. Use it to reason about how a parameter change
shifts exit *behavior* and worst-case loss, then confirm with live paper data.
"""
from __future__ import annotations

import argparse
from dataclasses import replace

from ..config import get_settings
from ..constants import MODE_HOLD, MODE_SCALP
from ..portfolio.portfolio import ExitParams, Position
from ..utils.money import round_trip_cost_pct

# (name, [(dt_seconds, price_multiple_vs_entry), ...])  — entry multiple is 1.0 at dt=0.
SCENARIOS: list[tuple[str, list[tuple[float, float]]]] = [
    ("moon",            [(0, 1.0), (30, 1.2), (60, 1.6), (90, 2.2), (120, 3.0), (150, 4.0), (240, 6.0)]),
    ("pump_dump",       [(0, 1.0), (20, 1.3), (40, 1.6), (60, 1.3), (80, 0.9), (100, 0.6), (120, 0.5)]),
    ("slow_bleed",      [(0, 1.0), (30, 0.97), (60, 0.92), (90, 0.85), (120, 0.78), (150, 0.72), (180, 0.70)]),
    ("chop",            [(0, 1.0), (20, 1.12), (40, 0.95), (60, 1.10), (80, 0.93), (100, 1.08), (140, 1.05), (200, 1.0)]),
    ("rug",             [(0, 1.0), (20, 1.10), (40, 1.05), (60, 0.30), (80, 0.05)]),
    ("quick_scalp",     [(0, 1.0), (20, 1.20), (40, 1.35), (60, 1.28), (90, 1.22), (180, 1.20)]),
    ("fakeout_recover", [(0, 1.0), (20, 0.90), (40, 0.88), (60, 1.05), (80, 1.25), (100, 1.50), (200, 1.70)]),
]


def run_scenario(path: list[tuple[float, float]], mode: str, params: ExitParams,
                 *, rt_cost: float = 0.0, entry: float = 1.0) -> dict:
    """Step a price path through Position.should_exit; return the realized exit."""
    pos = Position(mint="x", symbol="X", mode=mode, qty=1.0, cost_sol=entry,
                   peak_price_sol=entry)
    pos.opened_ts = 0.0
    reason, exit_mult, held = "open", path[-1][1], path[-1][0]
    for dt, mult in path:
        hit, why = pos.should_exit(entry * mult, params, now=dt)
        if hit:
            reason, exit_mult, held = why, mult, dt
            break
    gross = exit_mult - 1.0
    return {"mode": mode, "reason": reason, "exit_mult": exit_mult,
            "gross_pct": gross, "net_pct": gross - rt_cost, "held_s": held}


def overtightened_params(base: ExitParams) -> ExitParams:
    """The REJECTED over-tightening profile, kept as a cautionary comparison.
    It cuts stops and TPs aggressively. The sim shows this whipsaws winners (a
    tight SL is stopped out on a fakeout dip) and trims the right tail WITHOUT
    improving the rug worst-case — which is why the chosen defensive profile
    preserves capital through SIZE/exposure (see RiskLimits) instead of tighter
    stops. breakeven arm(0.10) stays > floor(0.04) so the profile is coherent."""
    return replace(
        base,
        scalp_sl_pct=0.12,        # tighter -> whipsaws fakeouts (see fakeout_recover)
        scalp_tp_pct=0.30,        # lower -> R:R inverts toward negative-EV
        scalp_max_hold_s=120.0,
        hold_sl_pct=0.25,
        hold_trail_pct=0.20,
        hold_tp_pct=2.0,
        breakeven_arm_pct=0.10,
        breakeven_floor_pct=0.04,
    )


def _summary(results: list[dict]) -> dict:
    nets = [r["net_pct"] for r in results]
    wins = [n for n in nets if n > 0]
    return {"avg": sum(nets) / len(nets), "total": sum(nets),
            "worst": min(nets), "best": max(nets),
            "win_rate": len(wins) / len(nets)}


def _run_all(params: ExitParams, rt_cost: float) -> dict[str, dict]:
    out = {}
    for name, path in SCENARIOS:
        for mode in (MODE_SCALP, MODE_HOLD):
            out[f"{name}/{mode}"] = run_scenario(path, mode, params, rt_cost=rt_cost)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Synthetic exit-logic scenario simulator.")
    ap.add_argument("--compare", action="store_true",
                    help="compare current params vs the defensive profile")
    args = ap.parse_args()
    s = get_settings()
    base = getattr(s, "exit", None) or ExitParams()   # uses s.exit once exit params are wired into config
    rt = round_trip_cost_pct(s.fees)
    print(f"round-trip cost = {rt * 100:.1f}%  (net = gross - round-trip)\n")

    if not args.compare:
        cur = _run_all(base, rt)
        print(f"{'scenario/mode':>24} {'reason':>10} {'net%':>8} {'held_s':>7}")
        for k, r in cur.items():
            print(f"{k:>24} {r['reason']:>10} {r['net_pct'] * 100:>+8.1f} {r['held_s']:>7.0f}")
        s2 = _summary(list(cur.values()))
        print(f"\n  avg net {s2['avg']*100:+.1f}% | total {s2['total']*100:+.1f}% | "
              f"worst {s2['worst']*100:+.1f}% | win% {s2['win_rate']*100:.0f}")
        return

    dfn = overtightened_params(base)
    cur, new = _run_all(base, rt), _run_all(dfn, rt)
    print(f"{'scenario/mode':>24} {'DEFAULT rsn':>11} {'default%':>9} | "
          f"{'TIGHT rsn':>11} {'tight%':>9}")
    for k in cur:
        a, b = cur[k], new[k]
        print(f"{k:>24} {a['reason']:>11} {a['net_pct']*100:>+9.1f} | "
              f"{b['reason']:>11} {b['net_pct']*100:>+9.1f}")
    sa, sb = _summary(list(cur.values())), _summary(list(new.values()))
    print(f"\n  DEFAULT (defensive): avg {sa['avg']*100:+.1f}% | total {sa['total']*100:+.1f}% | "
          f"worst {sa['worst']*100:+.1f}% | best {sa['best']*100:+.1f}% | win% {sa['win_rate']*100:.0f}")
    print(f"  over-tightened (rejected): avg {sb['avg']*100:+.1f}% | total {sb['total']*100:+.1f}% | "
          f"worst {sb['worst']*100:+.1f}% | best {sb['best']*100:+.1f}% | win% {sb['win_rate']*100:.0f}")
    print("\n  (NOTE: tightening exit stops does NOT shrink the rug worst-case "
          "(instant, between ticks) and can whipsaw winners. Real capital "
          "preservation comes from SIZE/exposure/selectivity, not tighter SL.)")


if __name__ == "__main__":
    main()
