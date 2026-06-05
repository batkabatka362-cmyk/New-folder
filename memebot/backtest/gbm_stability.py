"""GBM signal-STABILITY diagnostic — is the offline edge real, or one-split noise?

A single 80/20 forward split gives a wildly unstable AUC on thin data: the same in-distribution set
read minutes apart (126 vs 157 mints) swung the forward-val AUC from 0.79 to 0.48. So ONE AUC number
is meaningless here. This tool runs K EXPANDING-WINDOW forward folds (train on a growing time-prefix,
validate on the next block — no look-ahead) and reports the AUC DISTRIBUTION + a verdict: a STABLE
edge has mean AUC above the bar AND a floor that stays above chance; NOISE has mean ~0.5 with folds
straddling it. This is the honest readiness signal — far more trustworthy than a single split — for
the eventual go/no-go decision. Shares the class-prior correction + precision gate with retrain.py.

  python -m memebot.backtest.gbm_stability [--folds 5] [--death-drop 0.9]
"""
from __future__ import annotations

import argparse

from ..config import get_settings
from ..filter.features import FEATURE_NAMES, row_from_feats
from .label import _load_all_obs, fixed_horizon_labels
from .retrain import balanced_params, precision_over_base


def fold_cuts(n: int, folds: int, min_train: int = 40, min_val: int = 10) -> list[tuple[int, int]]:
    """Expanding-window forward splits over n time-ordered rows. Returns [(cut, end), ...] so each fold
    trains on rows[:cut] and validates on rows[cut:end] — train GROWS, the val block SLIDES forward,
    never peeking ahead. [] when there isn't room for even one fold."""
    if folds < 1 or n < min_train + min_val:
        return []
    val = max(min_val, (n - min_train) // folds)
    cuts: list[tuple[int, int]] = []
    start = min_train
    while start + min_val <= n:
        cuts.append((start, min(start + val, n)))
        start += val
    return cuts


def _mean_std(xs: list[float]) -> tuple[float, float]:
    n = len(xs)
    if n == 0:
        return 0.0, 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / n
    return m, var ** 0.5


def summarize_stability(aucs: list[float], edge_auc: float = 0.55) -> dict:
    """Aggregate per-fold AUCs into a stability verdict. STABLE_EDGE: mean >= edge_auc AND the WORST
    fold still beats chance (>=0.5). NOISE: mean <= 0.55 (the edge is indistinguishable from a coin).
    INCONCLUSIVE otherwise (a promising mean undermined by a sub-chance fold = not yet trustworthy)."""
    n = len(aucs)
    if n == 0:
        return {"folds": 0, "mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0,
                "frac_above_edge": 0.0, "verdict": "no-data"}
    mean, std = _mean_std(aucs)
    lo, hi = min(aucs), max(aucs)
    above = sum(1 for a in aucs if a >= edge_auc) / n
    if mean >= edge_auc and lo >= 0.5:
        verdict = "STABLE_EDGE"
    elif mean <= 0.55:
        verdict = "NOISE"
    else:
        verdict = "INCONCLUSIVE"
    return {"folds": n, "mean": mean, "std": std, "min": lo, "max": hi,
            "frac_above_edge": above, "verdict": verdict}


def _fold_auc(x_tr, y_tr, x_va, y_va, threshold: float, balance: bool):
    """Train one fold; return (auc, precision, base_rate, n_pred_pos) or None if a split is single-class."""
    import lightgbm as lgb
    import numpy as np
    if len(set(y_tr)) < 2 or len(set(y_va)) < 2:
        return None
    x_tr = np.asarray(x_tr, dtype=float); x_va = np.asarray(x_va, dtype=float)   # lgb needs ndarray, not list
    y_tr = np.asarray(y_tr, dtype=int); y_va = np.asarray(y_va, dtype=int)
    tr = lgb.Dataset(x_tr, label=y_tr, feature_name=FEATURE_NAMES)
    va = lgb.Dataset(x_va, label=y_va, reference=tr, feature_name=FEATURE_NAMES)
    params = balanced_params(
        {"objective": "binary", "metric": "auc", "num_leaves": 31, "learning_rate": 0.05,
         "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1,
         "min_data_in_leaf": 20, "verbose": -1, "num_threads": 1},
        y_tr, balance)
    b = lgb.train(params, tr, num_boost_round=300, valid_sets=[va],
                  callbacks=[lgb.early_stopping(30), lgb.log_evaluation(0)])
    auc = float(b.best_score["valid_0"]["auc"])
    scores = b.predict(x_va)
    prec, base, npp = precision_over_base([int(v) for v in y_va], [float(v) for v in scores], threshold)
    return auc, prec, base, npp


def main() -> None:
    ap = argparse.ArgumentParser(description="GBM signal-stability diagnostic over expanding-window folds.")
    ap.add_argument("--db", default=None)
    ap.add_argument("--horizon", type=float, default=300.0)
    ap.add_argument("--up", type=float, default=0.5, help="forward return for a positive label (0.5 = +50%)")
    ap.add_argument("--tol", type=float, default=900.0)
    ap.add_argument("--death-drop", type=float, default=0.9)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--edge-auc", type=float, default=0.55)
    args = ap.parse_args()

    try:
        import lightgbm  # noqa: F401
    except ImportError:
        raise SystemExit("pip install lightgbm numpy")

    s = get_settings()
    rows = _load_all_obs(args.db or s.db_path)
    labeled = fixed_horizon_labels(rows, args.horizon, args.up, args.tol,
                                   death_drop=(args.death_drop or None), indexed_only=True)
    n = len(labeled)
    cuts = fold_cuts(n, args.folds)
    if not cuts:
        print(f"only {n} labeled mints — not enough for {args.folds} forward folds yet; keep collecting.")
        return
    xs = [row_from_feats(f) for _m, f, _l, _fwd in labeled]   # serve-consistent encoding
    ys = [int(lbl) for _m, _f, lbl, _fwd in labeled]
    threshold = s.gbm_entry_threshold

    print(f"\nGBM stability over {len(cuts)} expanding-window folds | n={n} pos={sum(ys)} "
          f"({sum(ys) / n * 100:.0f}%) | thr {threshold}\n")
    print(f"  {'fold':>4} {'train':>6} {'val':>5} {'auc':>7} {'precision':>10} {'base':>6} {'pred+':>6}")
    aucs: list[float] = []
    for i, (cut, end) in enumerate(cuts, 1):
        r = _fold_auc(xs[:cut], ys[:cut], xs[cut:end], ys[cut:end], threshold, s.gbm_balance_classes)
        if r is None:
            print(f"  {i:>4} {cut:>6} {end - cut:>5}     (single-class split — skipped)")
            continue
        auc, prec, base, npp = r
        aucs.append(auc)
        print(f"  {i:>4} {cut:>6} {end - cut:>5} {auc:>7.3f} {prec:>10.3f} {base:>6.3f} {npp:>6}")

    summ = summarize_stability(aucs, edge_auc=args.edge_auc)
    print(f"\n  AUC across folds: mean {summ['mean']:.3f} +/- {summ['std']:.3f}  "
          f"[min {summ['min']:.3f}, max {summ['max']:.3f}]  "
          f"{summ['frac_above_edge'] * 100:.0f}% of folds >= {args.edge_auc}")
    print(f"  VERDICT: {summ['verdict']}")
    if summ["verdict"] == "STABLE_EDGE":
        print("  -> a consistent edge across time folds; the precision gate still decides deployment.")
    elif summ["verdict"] == "NOISE":
        print("  -> the offline 'edge' is indistinguishable from chance across folds. Keep collecting; "
              "do NOT chase a single lucky split.")
    else:
        print("  -> promising mean but a fold dips below chance — not yet trustworthy. Keep collecting.")


if __name__ == "__main__":
    main()
