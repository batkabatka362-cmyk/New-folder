"""WL19 — the autonomous strategy-DISCOVERY + out-of-sample validation engine (the Phase-3 / AGI core).

This automates what was done by hand to find the WL17 swing edge: search a space of mean-reversion
strategies, backtest each, and keep only those that survive ADVERSARIAL, anti-overfit validation. The
key rigor the manual pass lacked is WALK-FORWARD: every config is fit/observed on a TRAIN slice and judged
on a held-out TEST slice it never saw, and a config only PASSES if it beats buy-and-hold in BOTH slices
(consistency across time = a real edge, not a single-regime fluke or a multiple-testing artifact).

  python -m memebot.backtest.swing_discover [--fee 0.02 --clip 2.0 --type 4h --split 0.7]

Two signal families are searched: fixed-percent mean-reversion (the known WL17 edge) and Bollinger
(volatility-scaled bands) — so the engine can DISCOVER whether vol-scaling beats a fixed dip, not just
re-tune the known rule. Output: every config ranked by out-of-sample gmean with a PASS/FAIL verdict. This
is the seed of a loop that proposes -> backtests -> adversarially validates -> (later) paper-deploys new
strategies on its own.
"""
from __future__ import annotations

import argparse
import asyncio

from ..config import get_settings
from .swing_lab import _gmean, _simulate, buy_hold, load_bars, mean_rev


def bollinger(window: int, k: float):
    """Volatility-scaled mean-reversion: buy when price < SMA - k*stdev, sell on reversion above the SMA."""
    def d(c, h, l, i, pos):
        if i + 1 < window:
            return pos
        seg = c[i - window + 1:i + 1]
        m = sum(seg) / window
        sd = (sum((x - m) ** 2 for x in seg) / window) ** 0.5
        if pos == 0 and sd > 0 and c[i] < m - k * sd:
            return 1
        if pos == 1 and c[i] > m:
            return 0
        return pos
    return d


def _eval_split(data: dict, decide, warmup: int, fee: float, clip: float, split: float) -> dict:
    """Walk-forward: per token, fit/observe on bars[:cut] (TRAIN) and judge on bars[cut:] (TEST). Returns
    train/test geometric-mean multiples + how many tokens the strategy beat buy-and-hold in each slice."""
    tr_mults, te_mults, tr_beat, te_beat, n = [], [], 0, 0, 0
    for _sym, bars in data.items():
        cut = int(len(bars) * split)
        tr, te = bars[:cut], bars[cut:]
        if len(tr) < warmup + 5 or len(te) < warmup + 5:
            continue
        n += 1
        h_tr = _simulate(tr, buy_hold, fee, 0, clip)[0]
        h_te = _simulate(te, buy_hold, fee, 0, clip)[0]
        e_tr = _simulate(tr, decide, fee, warmup, clip)[0]
        e_te = _simulate(te, decide, fee, warmup, clip)[0]
        tr_mults.append(e_tr)
        te_mults.append(e_te)
        tr_beat += (e_tr > h_tr)
        te_beat += (e_te > h_te)
    return {"n": n, "train_gmean": _gmean(tr_mults), "test_gmean": _gmean(te_mults),
            "train_beat": tr_beat, "test_beat": te_beat}


def _search_space():
    """The strategies to propose. Modest grids keep multiple-testing honest; mean-rev (known edge) +
    Bollinger (vol-scaled) so the engine can discover which family is more robust."""
    space = []
    for w in (12, 24):
        for k in (0.12, 0.18, 0.25):
            space.append((f"meanrev w{w} k{k:.2f}", mean_rev(w, k), w))
        for kb in (1.5, 2.0, 2.5):
            space.append((f"bolling w{w} k{kb:.1f}", bollinger(w, kb), w))
    return space


def run_discovery(data: dict, fee: float, clip: float, split: float) -> list:
    """Pure: search the space, walk-forward-validate each config, return rows sorted by OOS gmean as
    [(test_gmean, name, metrics, passed)]. Shared by the CLI and the swing runner's autonomous loop.
    PASS = beats hold >=70% OUT-of-sample AND profitable OOS AND consistent IN-sample (>=60%)."""
    rows = []
    for name, decide, warmup in _search_space():
        r = _eval_split(data, decide, warmup, fee, clip, split)
        if not r["n"]:
            continue
        passed = (r["test_beat"] / r["n"] >= 0.70 and r["test_gmean"] > 1.0
                  and r["train_beat"] / r["n"] >= 0.60)
        rows.append((r["test_gmean"], name, r, passed))
    rows.sort(reverse=True, key=lambda x: x[0])
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Discover + out-of-sample-validate swing strategies.")
    ap.add_argument("--fee", type=float, default=0.02)
    ap.add_argument("--clip", type=float, default=2.0)
    ap.add_argument("--type", default="4h")
    ap.add_argument("--split", type=float, default=0.7, help="train fraction; the rest is held-out test")
    args = ap.parse_args()
    s = get_settings()
    if not (s.solanatracker_enabled and s.solanatracker_api_key):
        print("SOLANATRACKER_ENABLED + key required.")
        return
    data = asyncio.run(load_bars(s, args.type))
    data = {k: v for k, v in data.items() if v and len(v) > 100}
    if not data:
        print("no chart data.")
        return
    print(f"=== STRATEGY DISCOVERY ({args.type}, fee {args.fee*100:.0f}%, clip {args.clip}, "
          f"train {args.split*100:.0f}% / test {(1-args.split)*100:.0f}%, {len(data)} tokens) ===")
    print(f"  {'strategy':18} {'tr_gmean':>8} {'te_gmean':>8} {'tr>hold':>8} {'te>hold':>8}  verdict")
    rows = run_discovery(data, args.fee, args.clip, args.split)
    n_pass = 0
    for _te_g, name, r, passed in rows:
        n_pass += passed
        v = "PASS (OOS-robust)" if passed else "fail"
        print(f"  {name:18} {r['train_gmean']:>8.2f} {r['test_gmean']:>8.2f} "
              f"{r['train_beat']:>3}/{r['n']:<3}{'':1} {r['test_beat']:>3}/{r['n']:<3}  {v}")
    print(f"\n  {n_pass}/{len(rows)} configs PASS the walk-forward gate (beat hold IN + OUT of sample, "
          "profitable OOS).\n  PASS = a strategy the engine would (next step) paper-deploy + forward-test; "
          "fail = killed as overfit / regime-luck. The held-out TEST is the honest judge.")


if __name__ == "__main__":
    main()
