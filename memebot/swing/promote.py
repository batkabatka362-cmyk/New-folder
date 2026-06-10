"""WL25 — the self-improvement PROMOTION gate (the AGI core): let the autonomous discovery loop replace
the live mean-reversion config with a genuinely better one, but ONLY through a conservative forward-gate.

The discovery loop already walk-forward-validates configs every cycle. This decides whether a CHALLENGER
should replace the LIVE config. Safety (paper-only, reversible):
- the challenger must be an OOS-validated (multi-split PASS) MEAN-REVERSION config (the live engine only
  implements mean-reversion — a Bollinger winner can't be promoted),
- it must beat the LIVE config's out-of-sample gmean by a MARGIN (not a coin-flip edge),
- it must do so for several CONSECUTIVE discovery runs (persistence over weeks of accruing data — a single
  lucky cycle can't flip the live strategy).
Only then is `swing_live_params.json` written; the runner loads it on next start, so promotion is explicit,
logged, and trivially reverted (delete the file). Pure + deterministic, so it is unit-tested.
"""
from __future__ import annotations

import re

_MEANREV = re.compile(r"^meanrev w(\d+) k([\d.]+)$")


def parse_meanrev(name: str):
    """'meanrev w24 k0.18' -> {'window':24,'dip_k':0.18}; None for any other (e.g. Bollinger) config."""
    m = _MEANREV.match(name or "")
    if not m:
        return None
    return {"window": int(m.group(1)), "dip_k": float(m.group(2))}


def live_name(window: int, dip_k: float) -> str:
    return f"meanrev w{window} k{dip_k:.2f}"


def decide_promotion(rows: list, live_window: int, live_dip_k: float, streak: dict, *,
                     margin: float = 0.15, streak_needed: int = 3):
    """rows = discovery output [(test_gmean, name, metrics, passed)]. Returns (new_params|None, new_streak,
    note). new_params is set ONLY when a challenger has beaten live by `margin` for `streak_needed`
    consecutive runs. new_streak is the updated per-challenger consecutive-win counter to persist."""
    lname = live_name(live_window, live_dip_k)
    live_g = next((g for g, n, _r, _p in rows if n == lname), None)
    # eligible challengers: PASSING, mean-reversion, not the live config
    cands = [(g, n) for g, n, _r, p in rows if p and parse_meanrev(n) and n != lname]
    if live_g is None or not cands:
        return None, {}, "no live result or no eligible challenger -> hold"
    best_g, best_n = max(cands)
    if best_g < live_g * (1.0 + margin):
        return None, {}, (f"best challenger {best_n} ({best_g:.2f}) does not beat live {lname} "
                          f"({live_g:.2f}) by {margin:.0%} -> hold")
    new_streak = {best_n: int(streak.get(best_n, 0)) + 1}     # reset all others by only keeping the winner
    n = new_streak[best_n]
    if n >= streak_needed:
        return parse_meanrev(best_n), {}, (f"PROMOTE {best_n} ({best_g:.2f} vs live {live_g:.2f}) after "
                                           f"{n} consecutive winning runs")
    return None, new_streak, f"challenger {best_n} leads live by >={margin:.0%}; streak {n}/{streak_needed}"
