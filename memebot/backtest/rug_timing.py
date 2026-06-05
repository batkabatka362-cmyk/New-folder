"""Rug-TIMING / hazard study (NEW, survival-first) — WHEN do rugs happen, and can a time-stop dodge
them without cutting winners short?

The rug-avoidance classifier showed the rug class separates more STABLY than the winner. The natural
follow-up: rugs may also cluster in TIME. If a token's catastrophic collapse typically lands at a
predictable age, then a HOLD time-stop placed just before that window dodges the rug — and as long as
winners tend to PEAK earlier than rugs collapse, it costs little. This is the asymmetric, doctrine-
aligned lever: a time-stop only ever exits EARLIER, so a wrong stop = a small give-back, a right one =
dodging a -90%. Offline, no network; the recommended stop is then validated by the real exitlab.

  python -m memebot.backtest.rug_timing [--rug-drop 0.8]
"""
from __future__ import annotations

import argparse
import statistics
from collections import defaultdict

from ..config import get_settings
from .label import _load_all_obs


def mint_timing(obs: list, rug_drop: float, up: float) -> tuple[str, float | None]:
    """obs: a mint's [(ts, price), ...] oldest-first. Returns (outcome, age_s) where outcome is
    'rug' (price first fell >= rug_drop below entry; age = WHEN it first crossed), 'winner' (peak
    return >= up and never rugged first; age = the PEAK's age), or 'other'. age is seconds since the
    first observation. None when unmeasurable."""
    if len(obs) < 2:
        return "other", None
    t0, p0 = obs[0]
    if p0 <= 0:
        return "other", None
    rets = [(ts - t0, price / p0 - 1.0) for ts, price in obs[1:] if price > 0]
    if not rets:
        return "other", None
    rug_age = next((age for age, r in rets if r <= -abs(rug_drop)), None)
    if rug_age is not None:
        return "rug", rug_age                          # the collapse moment — a rug even if it pumped first
    peak_age, peak_ret = max(rets, key=lambda x: x[1])
    if peak_ret >= up:
        return "winner", peak_age
    return "other", None


def timestop_sweep(rug_ages: list, winner_ages: list, stops: list) -> list[dict]:
    """For each candidate time-stop T (seconds): the fraction of rugs that collapse AFTER T (so exiting
    at T would DODGE them) and the fraction of winners whose PEAK is after T (so exiting at T CUTS them
    short). `net` = dodged - cut is a crude desirability score (higher = a stop that skips more rugs
    than winners). Pure."""
    out = []
    nr, nw = len(rug_ages), len(winner_ages)
    for T in stops:
        dodged = (sum(1 for a in rug_ages if a > T) / nr) if nr else 0.0
        cut = (sum(1 for a in winner_ages if a > T) / nw) if nw else 0.0
        out.append({"stop": T, "rugs_dodged": dodged, "winners_cut": cut, "net": dodged - cut})
    return out


def _pct(xs: list, q: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    i = min(len(xs) - 1, int(q * len(xs)))
    return xs[i]


def main() -> None:
    ap = argparse.ArgumentParser(description="When do rugs happen, and what time-stop dodges them?")
    ap.add_argument("--db", default=None)
    ap.add_argument("--rug-drop", type=float, default=0.8, help="fraction drop from entry that counts as a rug")
    ap.add_argument("--up", type=float, default=0.5, help="peak return that counts as a winner")
    args = ap.parse_args()

    s = get_settings()
    rows = _load_all_obs(args.db or s.db_path)
    by: dict[str, list] = defaultdict(list)
    for mint, ts, price, feats in rows:
        if price > 0 and float(feats.get("liquidity_usd", 0.0)) > 0:   # in-distribution only
            by[mint].append((ts, price))
    rug_ages, win_ages = [], []
    for obs in by.values():
        obs.sort(key=lambda x: x[0])
        outcome, age = mint_timing(obs, args.rug_drop, args.up)
        if outcome == "rug" and age is not None:
            rug_ages.append(age)
        elif outcome == "winner" and age is not None:
            win_ages.append(age)

    if len(rug_ages) < 5:
        print(f"only {len(rug_ages)} witnessed rugs in-distribution — too few to study timing yet; keep collecting.")
        return
    print(f"\nrug-timing over {len(by)} indexed mints | {len(rug_ages)} rugs, {len(win_ages)} winners\n")
    print(f"  time-to-RUG (s):    median {statistics.median(rug_ages):.0f}  "
          f"p25 {_pct(rug_ages, 0.25):.0f}  p75 {_pct(rug_ages, 0.75):.0f}  max {max(rug_ages):.0f}")
    if win_ages:
        print(f"  time-to-WINNER-peak (s): median {statistics.median(win_ages):.0f}  "
              f"p25 {_pct(win_ages, 0.25):.0f}  p75 {_pct(win_ages, 0.75):.0f}")
    print()
    print(f"  {'time-stop':>9}  {'rugs_dodged':>11}  {'winners_cut':>11}  {'net':>6}")
    sweep = timestop_sweep(rug_ages, win_ages, [60, 120, 180, 300, 450, 600, 900, 1200])
    for r in sweep:
        print(f"  {r['stop']:>8}s  {r['rugs_dodged'] * 100:>10.0f}%  {r['winners_cut'] * 100:>10.0f}%  {r['net']:>+6.2f}")
    best = max(sweep, key=lambda r: r["net"])
    print(f"\n  best net time-stop: {best['stop']}s (dodges {best['rugs_dodged'] * 100:.0f}% of rugs, "
          f"cuts {best['winners_cut'] * 100:.0f}% of winners). VALIDATE in exitlab before wiring — this is "
          "a crude peak/collapse-timing proxy, not a path simulation, and a stop only helps if rugs "
          "systematically land LATER than winners peak.")


if __name__ == "__main__":
    main()
