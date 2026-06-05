"""Exit-ladder backtest (P8) — the under-studied lever.

The audit's evidence: the ENTRY edge doesn't exist on free data (winner/rug features overlap), but the
EXIT policy was never swept. This replays each mint's LOGGED price path (from `observations`) through
the REAL exit ladder — `Position.should_exit` (tp / sl / breakeven-arm / be_trail / trail / stall /
timeout) AND the FULL scale-out it drives in the live manage loop: `should_take_partial` (P3 partial
take-profit) and a PRICE-ONLY proactive principal-recovery derisk (N5, `derisk_proactive_pct`). It
weights the multi-leg return by the fraction sold at each leg — entering at the first observation, and
sweeps `ExitParams` variants to find which exit policy gives the best net-of-fees return. Faithful (it
calls the production exit/partial code), not a re-implementation.

N2: the scale-out ('bank profit FAST, free-roll the rest') is the user's #1 doctrine — modelling the
partial/derisk legs is what lets it be VALIDATED offline instead of tuned by feel.

LIMITATION: ~3s-resolution polled prices (not the intrabar path), entry = first-in-scope observation
(not the live entry gate). The RISK-gated derisk (concentration/sell-pressure tells) is NOT replayable
here — per-tick concentration isn't logged — only the PRICE-driven partial + proactive derisk are; so
a 'proactive derisk wins' result does NOT validate the production risk-gated path. Compares exit
policies RELATIVELY; it is not a PnL forecast. Use it to pick a better trail/arm/SL/partial/derisk,
then confirm forward.

  python -m memebot.backtest.exitlab --min-len 8
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import replace

from ..config import get_settings
from ..portfolio.portfolio import ExitParams, Position
from ..utils.money import round_trip_cost_pct, sell_cost_pct


def load_paths(db_path: str, *, min_len: int = 8) -> list[tuple[str, str, list]]:
    """Per-mint (mint, mode, [(ts, price_usd), ...]) price paths from observations, oldest-first.
    Only mints with >= min_len priced points (a real path to replay an exit policy against)."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT mint, mode, ts, price_usd FROM observations WHERE price_usd > 0 ORDER BY mint, ts ASC"
        ).fetchall()
    finally:
        conn.close()
    by: dict[str, list] = defaultdict(list)
    modes: dict[str, str] = {}
    for mint, mode, ts, price in rows:
        by[mint].append((float(ts or 0.0), float(price)))
        modes[mint] = mode or "scalp"
    return [(m, modes[m], p) for m, p in by.items() if len(p) >= min_len]


def replay_exit(path: list, mode: str, params: ExitParams, sell_cost: float = 0.0) -> tuple[str, float, float]:
    """Walk a price path through the REAL exit ladder + scale-out from entry=path[0]. Returns
    (final_exit_reason, QTY-WEIGHTED gross multiple, hold_s) — the multiple sums each leg's
    fraction*(price/entry), so a partial banked at +20% and the rest exited at -10% nets correctly.
    'end' = the remainder never hit a full exit within the path. Mirrors the live manage-loop order:
    a FULL exit (should_exit) wins over a partial; the proactive derisk (price-only) takes precedence
    over the P3 partial at the same tick (it secures principal at a lower/own bar)."""
    t0, p0 = path[0]
    if p0 <= 0:
        return "skip", 1.0, 0.0
    pos = Position(mint="x", symbol="", mode=mode, qty=1.0, cost_sol=p0, opened_ts=t0, peak_price_sol=p0)
    realized = 0.0          # banked gross multiple from partial/derisk legs (fraction * price/p0)
    remaining = 1.0         # fraction of the original position still open
    for ts, price in path[1:]:
        if price <= 0:
            continue
        do_exit, reason = pos.should_exit(price, params, now=ts)   # updates peak; full exit wins
        if do_exit:
            realized += remaining * (price / p0)
            return reason, realized, ts - t0
        # A3 TAKE-INITIAL (price-only, OWN latch): recover principal at >= take_initial_pct (default 2x),
        # ride the rest as house money. Independent of the P3 partial (separate latch), so it composes;
        # takes precedence over the partial at the same tick (mirrors the live manage-loop elif order).
        if (not pos.initial_taken and params.take_initial_pct > 0.0
                and pos.pnl_pct(price) >= params.take_initial_pct):
            f = pos.derisk_fraction(price, sell_cost, params.derisk_max_frac)
            if f > 0.0:
                realized += remaining * f * (price / p0)
                remaining *= (1.0 - f)
                pos.initial_taken = True
                pos.breakeven_armed = True
                continue
        if not pos.partial_taken:
            # N5 proactive principal-recovery (price-only): bank principal once up >= the bar, free-roll
            # the rest. Mutually exclusive with the P3 partial via the same partial_taken latch (as live).
            if params.derisk_proactive_pct > 0.0 and pos.pnl_pct(price) >= params.derisk_proactive_pct:
                f = pos.derisk_fraction(price, sell_cost, params.derisk_max_frac)
                if f > 0.0:
                    realized += remaining * f * (price / p0)
                    remaining *= (1.0 - f)
                    pos.partial_taken = True
                    pos.breakeven_armed = True       # protect the free-roll tail with the be-trail
                    continue
            # P3 partial take-profit (price-driven)
            if pos.should_take_partial(price, params):
                f = params.partial_tp_frac
                realized += remaining * f * (price / p0)
                remaining *= (1.0 - f)
                pos.partial_taken = True
                pos.breakeven_armed = True
    last_ts, last_p = path[-1]
    realized += remaining * (last_p / p0)
    return "end", realized, last_ts - t0


def evaluate(paths: list, params: ExitParams, rtc: float, sell_cost: float = 0.0) -> dict:
    """Aggregate net-of-fees return over every path for one exit policy. The round-trip cost is charged
    once on the full notional regardless of how many legs the scale-out sells in — every leg's fractions
    sum to 1.0, so total sell cost == one-way × 1 and buy cost == one-way × 1 == rtc (a tiny per-leg
    timing approximation)."""
    rets: list[float] = []
    reasons: Counter = Counter()
    for _mint, mode, path in paths:
        reason, mult, _hold = replay_exit(path, mode, params, sell_cost)
        if reason == "skip":
            continue
        rets.append(mult - 1.0 - rtc)            # net return after the round-trip fee+slippage
        reasons[reason] += 1
    if not rets:
        return {"n": 0, "net": 0.0, "avg": 0.0, "win_rate": 0.0, "median": 0.0, "reasons": {}}
    wins = sum(1 for r in rets if r > 0)
    return {"n": len(rets), "net": sum(rets), "avg": sum(rets) / len(rets),
            "win_rate": wins / len(rets), "median": statistics.median(rets), "reasons": dict(reasons)}


def default_variants(base: ExitParams) -> dict[str, ExitParams]:
    """The baseline live ExitParams plus single-knob perturbations — isolates each lever's effect,
    INCLUDING the N2 scale-out levers (partial take-profit + the N5 proactive principal-recovery)."""
    return {
        "baseline": base,
        "trail_tight_04": replace(base, trail_after_arm_pct=0.04),
        "trail_loose_10": replace(base, trail_after_arm_pct=0.10),
        "arm_sooner_08": replace(base, breakeven_arm_pct=0.08),
        "arm_later_20": replace(base, breakeven_arm_pct=0.20),
        "hold_trail_20": replace(base, hold_trail_pct=0.20),
        "hold_trail_40": replace(base, hold_trail_pct=0.40),
        "sl_tight": replace(base, scalp_sl_pct=0.12, hold_sl_pct=0.25),
        "sl_wide": replace(base, scalp_sl_pct=0.25, hold_sl_pct=0.45),
        # N2 scale-out levers — the user's "bank profit FAST" doctrine, now measurable:
        "no_partial": replace(base, partial_tp_frac=0.0),            # isolate the partial's contribution
        "partial_sooner_10": replace(base, partial_tp_pct=0.10),    # bank a smaller move
        "partial_more_70": replace(base, partial_tp_frac=0.7),      # sell more of the position on the partial
        "derisk_30": replace(base, derisk_proactive_pct=0.30),      # N5: bank principal at +30%, free-roll
        "derisk_50": replace(base, derisk_proactive_pct=0.50),      # N5: later, bigger free-roll tail
    }


def sweep(paths: list, variants: dict, rtc: float, sell_cost: float = 0.0) -> list[tuple[str, dict]]:
    return sorted(((name, evaluate(paths, p, rtc, sell_cost)) for name, p in variants.items()),
                  key=lambda kv: kv[1]["net"], reverse=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Sweep exit-policy variants over logged price paths.")
    ap.add_argument("--db", default=None)
    ap.add_argument("--min-len", type=int, default=8, help="min priced observations for a usable path")
    args = ap.parse_args()
    s = get_settings()
    paths = load_paths(args.db or s.db_path, min_len=args.min_len)
    if not paths:
        print("No replayable price paths yet (need mints with >= --min-len priced observations).")
        return
    rtc = round_trip_cost_pct(s.fees)
    sell_cost = sell_cost_pct(s.fees)        # one-way cost the proactive-derisk principal solve assumes
    print(f"\n{len(paths)} replayable mint paths | round-trip cost {rtc * 100:.1f}% | net = mult-1-cost\n")
    print(f"  {'variant':18} {'n':>4} {'net':>9} {'avg':>8} {'win':>5} {'median':>8}   top exits")
    for name, r in sweep(paths, default_variants(s.exit), rtc, sell_cost):
        top = ", ".join(f"{k}:{v}" for k, v in sorted(r["reasons"].items(), key=lambda x: -x[1])[:3])
        marker = "  <- baseline" if name == "baseline" else ""
        print(f"  {name:18} {r['n']:>4} {r['net']:>+9.3f} {r['avg']:>+8.3f} "
              f"{r['win_rate'] * 100:>4.0f}% {r['median']:>+8.3f}   {top}{marker}")
    print("\nA variant that beats `baseline` on net AND avg is a real exit-policy improvement to adopt "
          "(then confirm forward). All policies negative => the leak is entries/data, not exits.")


if __name__ == "__main__":
    main()
