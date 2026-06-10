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


def _eval_split(data: dict, decide, warmup: int, fee: float, clip: float, split: float,
                slippage_bps: float = 0.0) -> dict:
    """Walk-forward: per token, fit/observe on bars[:cut] (TRAIN) and judge on bars[cut:] (TEST). Returns
    train/test geometric-mean multiples + how many tokens the strategy beat buy-and-hold in each slice.
    Costs (fee + slippage) are applied identically to the strategy and the hold baseline for a fair OOS test."""
    tr_mults, te_mults, tr_beat, te_beat, n, te_trades = [], [], 0, 0, 0, 0
    for _sym, bars in data.items():
        cut = int(len(bars) * split)
        tr, te = bars[:cut], bars[cut:]
        if len(tr) < warmup + 5 or len(te) < warmup + 5:
            continue
        n += 1
        h_tr = _simulate(tr, buy_hold, fee, 0, clip, slippage_bps=slippage_bps)[0]
        h_te = _simulate(te, buy_hold, fee, 0, clip, slippage_bps=slippage_bps)[0]
        e_tr = _simulate(tr, decide, fee, warmup, clip, slippage_bps=slippage_bps)[0]
        sim_te = _simulate(te, decide, fee, warmup, clip, slippage_bps=slippage_bps)
        e_te = sim_te[0]
        te_trades += sim_te[1]
        tr_mults.append(e_tr)
        te_mults.append(e_te)
        tr_beat += (e_tr > h_tr)
        te_beat += (e_te > h_te)
    return {"n": n, "train_gmean": _gmean(tr_mults), "test_gmean": _gmean(te_mults),
            "train_beat": tr_beat, "test_beat": te_beat, "te_trades": te_trades}


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


def _passes_split(r: dict) -> bool:
    return bool(r["n"]) and r["test_beat"] / r["n"] >= 0.70 and r["test_gmean"] > 1.0 \
        and r["train_beat"] / r["n"] >= 0.60


def run_discovery(data: dict, fee: float, clip: float, split: float, slippage_bps: float = 0.0,
                  splits: list | None = None, min_trades: int = 15) -> list:
    """Pure: search the space, walk-forward-validate each config across MULTIPLE splits, return rows
    sorted by OOS gmean as [(test_gmean, name, metrics, passed)]. Shared by the CLI and the swing runner's
    autonomous loop.

    Multiple-testing / overfit control (WL20 rank-8): searching 12 configs and blessing one on a SINGLE
    lucky split inflates false positives, so a config PASSES only if it clears the per-split gate (beat
    hold >=70% OOS + profitable OOS + >=60% in-sample) on a MAJORITY of several splits AND traded enough
    (>= min_trades OOS, so a 1-trade fluke like the refuted regime configs can't pass). Advisory only."""
    splits = splits or sorted({0.5, 0.6, split, 0.8})
    rows = []
    for name, decide, warmup in _search_space():
        per = [_eval_split(data, decide, warmup, fee, clip, sp, slippage_bps=slippage_bps) for sp in splits]
        mid = per[len(per) // 2]                       # representative split for display
        if not mid["n"]:
            continue
        sp_pass = sum(1 for r in per if _passes_split(r))
        enough = mid.get("te_trades", 0) >= min_trades
        passed = enough and sp_pass >= (len(splits) + 1) // 2     # majority of splits + meaningful activity
        mid = {**mid, "splits_passed": f"{sp_pass}/{len(splits)}", "n_tests": len(_search_space())}
        rows.append((mid["test_gmean"], name, mid, passed))
    rows.sort(reverse=True, key=lambda x: x[0])
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Discover + out-of-sample-validate swing strategies.")
    ap.add_argument("--fee", type=float, default=0.02)
    ap.add_argument("--clip", type=float, default=2.0)
    ap.add_argument("--type", default="4h")
    ap.add_argument("--split", type=float, default=0.7, help="train fraction; the rest is held-out test")
    ap.add_argument("--slippage", type=float, default=0.0, help="per-side slippage bps (honest dip-buy cost)")
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
    print(f"  {'strategy':18} {'te_gmean':>8} {'te>hold':>8} {'splits':>7} {'trades':>7}  verdict")
    rows = run_discovery(data, args.fee, args.clip, args.split, slippage_bps=args.slippage)
    n_pass = 0
    for _te_g, name, r, passed in rows:
        n_pass += passed
        v = "PASS (multi-split)" if passed else "fail"
        print(f"  {name:18} {r['test_gmean']:>8.2f} {r['test_beat']:>3}/{r['n']:<4} "
              f"{r.get('splits_passed', '?'):>7} {r.get('te_trades', 0):>7}  {v}")
    print(f"\n  {n_pass}/{len(rows)} configs PASS (majority of splits beat hold IN+OUT of sample, profitable "
          f"OOS, >=15 OOS trades). Searched {rows[0][2].get('n_tests', len(rows)) if rows else 0} configs — the\n"
          "  majority-of-splits + min-trades gate is the multiple-testing control so a single lucky split "
          "can't crown a noise config.\n  NOTE the selectivity/significance tradeoff: deeper-dip configs "
          "(k0.18+) have the highest gmean and are tail-safe but trade LESS (wider error bars); shallower "
          "configs trade more. Advisory: a PASS earns paper-deploy + forward-test, never auto-live.")


if __name__ == "__main__":
    main()
