"""Loss decomposition (WL7, the user's strategy lens) — of our net-LOSSES, how much is fixable by
EXITS vs is a SELECTION problem?

The user's strategy: ">95% of coins fail; for the ones that PUMP, recover the principal at 1-2x so an
immediate loss becomes impossible, then ride the rest." This tool sizes that lever HONESTLY. It replays
each logged price path through the REAL exit ladder, finds the net-LOSERS, and buckets them by the PEAK
they reached before the policy exited:

  - a loser that PUMPED >= peak_threshold (default +30%) then faded to a loss is EXIT-ADDRESSABLE —
    banking principal earlier (a lower take_initial bar) saves it (it had a gain to bank).
  - a loser that NEVER pumped is a SELECTION problem — no exit can bank a gain that never happened;
    only NOT entering it (the image / filter funnel, not exits) avoids it.

The split tells you where the next unit of work belongs: tune exits (small, bounded) vs improve
selection (the bulk). Offline, read-only.

  python -m memebot.backtest.loss_decomp [--peak 0.30] [--min-len 8]
"""
from __future__ import annotations

import argparse

from ..config import get_settings
from ..utils.money import round_trip_cost_pct, sell_cost_pct
from .exitlab import load_paths, replay_exit

# the peak buckets reported under the headline split (lower edge inclusive, upper exclusive)
_BUCKETS = ((-1.0, 0.10), (0.10, 0.30), (0.30, 0.50), (0.50, 1.0), (1.0, 1e9))
_BUCKET_LABELS = ("never +10%", "+10-30%", "+30-50%", "+50-100%", ">+100%")


def path_peak(path: list) -> float:
    """Max forward return over a price path (entry = path[0]); 0.0 if it never rose."""
    if not path or path[0][1] <= 0:
        return 0.0
    p0 = path[0][1]
    # peak GAIN floors at 0.0: a path that only fell never "pumped" (0%), it didn't peak negative.
    return max(0.0, max((pr / p0 - 1.0 for _, pr in path[1:] if pr > 0), default=0.0))


def bucket_losers(pairs: list, peak_threshold: float = 0.30) -> dict:
    """pairs = [(peak_ret, net_ret)] — pure. Buckets the net-LOSERS (net<0) by peak reached and splits
    addressable (peaked >= threshold) vs never-pump. Returns counts + summed SOL per bucket/split."""
    losers = [(pk, r) for pk, r in pairs if r < 0]
    nl = len(losers)
    buckets = []
    for (lo, hi), label in zip(_BUCKETS, _BUCKET_LABELS):
        grp = [r for pk, r in losers if lo <= pk < hi]
        buckets.append({"label": label, "n": len(grp), "sol": sum(grp)})
    addr = [(pk, r) for pk, r in losers if pk >= peak_threshold]
    never = [(pk, r) for pk, r in losers if pk < peak_threshold]
    return {
        "n_paths": len(pairs), "n_losers": nl,
        "buckets": buckets,
        "addressable": {"n": len(addr), "sol": sum(r for _, r in addr)},   # exit-fixable
        "selection": {"n": len(never), "sol": sum(r for _, r in never)},   # never pumped
    }


def decompose(paths: list, params, rtc: float, sell_cost: float, peak_threshold: float = 0.30) -> dict:
    """Replay each path through the REAL exit ladder, then bucket the losers by their peak."""
    pairs = []
    for _mint, mode, path in paths:
        reason, mult, _ = replay_exit(path, mode, params, sell_cost)
        if reason == "skip":
            continue
        pairs.append((path_peak(path), mult - 1.0 - rtc))
    return bucket_losers(pairs, peak_threshold)


def main() -> None:
    ap = argparse.ArgumentParser(description="Decompose net-losses into exit-addressable vs selection-problem.")
    ap.add_argument("--db", default=None)
    ap.add_argument("--min-len", type=int, default=8)
    ap.add_argument("--peak", type=float, default=0.30, help="peak a loser must reach to be exit-addressable")
    args = ap.parse_args()
    s = get_settings()
    paths = load_paths(args.db or s.db_path, min_len=args.min_len)
    if not paths:
        print("No replayable price paths yet (need mints with >= --min-len priced observations).")
        return
    rtc, sc = round_trip_cost_pct(s.fees), sell_cost_pct(s.fees)
    d = decompose(paths, s.exit, rtc, sc, args.peak)
    if not d["n_losers"]:
        print(f"{d['n_paths']} paths, 0 net-losers — nothing to decompose.")
        return
    print(f"\n{d['n_paths']} replayable paths | {d['n_losers']} net-LOSERS — bucketed by the PEAK they reached:\n")
    for b in d["buckets"]:
        pct = b["n"] / d["n_losers"] * 100.0
        print(f"  peak {b['label']:11}: {b['n']:>4} losers ({pct:>4.0f}%)  total {b['sol']:>+8.3f} SOL")
    a, sel = d["addressable"], d["selection"]
    ap_, sp = a["n"] / d["n_losers"] * 100.0, sel["n"] / d["n_losers"] * 100.0
    print(f"\n  EXIT-ADDRESSABLE (peaked >= +{args.peak * 100:.0f}% then faded): {a['n']} losers ({ap_:.0f}%), "
          f"{a['sol']:+.3f} SOL  <- bank principal earlier")
    print(f"  SELECTION PROBLEM (never pumped): {sel['n']} losers ({sp:.0f}%), {sel['sol']:+.3f} SOL  "
          f"<- only NOT entering avoids it (image / filter funnel, NOT exits)")
    print("\n  Read it as: exits can only recover the addressable slice; the selection slice is the wall "
          "the image/branding/funnel work targets. Don't tune exits to fix a selection problem.")


if __name__ == "__main__":
    main()
