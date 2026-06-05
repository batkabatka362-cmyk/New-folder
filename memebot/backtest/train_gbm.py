"""Train a LightGBM classifier on a labeled dataset and export the model.

  pip install lightgbm numpy
  python -m memebot.backtest.train_gbm --data dataset.csv --out gbm_model.txt

The exported model is loaded automatically by the live bot (filter/gbm.py) on
next start, replacing the rule scorer. Column order MUST match
filter/features.FEATURE_NAMES (label.py guarantees this).
"""
from __future__ import annotations

import argparse
import csv

from ..filter.features import FEATURE_NAMES, dark_features, feature_coverage


def _read(path: str):
    import numpy as np

    xs, ys = [], []
    with open(path, newline="") as fh:
        r = csv.reader(fh)
        header = next(r)
        if header[:-1] != FEATURE_NAMES or header[-1] != "label":
            raise SystemExit(
                f"dataset columns don't match FEATURE_NAMES.\n  got:    {header}\n"
                f"  expect: {FEATURE_NAMES + ['label']}\nRe-run label.py with the current code."
            )
        for row in r:
            if not row:
                continue
            xs.append([float(v) for v in row[:-1]])
            ys.append(int(float(row[-1])))
    return np.asarray(xs, dtype=float), np.asarray(ys, dtype=int)


def main() -> None:
    ap = argparse.ArgumentParser(description="Train LightGBM on a labeled candidate dataset.")
    ap.add_argument("--data", default="dataset.csv")
    ap.add_argument("--out", default="gbm_model.txt")
    ap.add_argument("--val-frac", type=float, default=0.2)
    ap.add_argument("--rounds", type=int, default=300)
    ap.add_argument("--no-balance", action="store_true",
                    help="skip the scale_pos_weight=n_neg/n_pos class-prior correction (on by default)")
    args = ap.parse_args()

    try:
        import lightgbm as lgb
        import numpy as np
    except ImportError:
        raise SystemExit("pip install lightgbm numpy")

    x, y = _read(args.data)
    n = len(y)
    if n < 50:
        raise SystemExit(f"only {n} rows — collect more data (aim for hundreds+).")
    if y.min() == y.max():
        raise SystemExit("dataset has only one class — need both winners and losers.")

    # N6: surface DARK/constant features BEFORE training so their importance can't be misread as
    # signal (e.g. the G3-tape columns are all -1 on the no-stream population). Don't drop them — that
    # would break the load-bearing FEATURE_NAMES column order the live scorer serves on — just WARN.
    cov = feature_coverage(x.tolist())
    dark = dark_features(cov)
    if dark:
        # ASCII only — this CLI prints to the Windows cp1252 console, which crashes on non-ASCII.
        print(f"[N6 WARNING] {len(dark)} DARK/constant feature(s) add NOTHING; do NOT read their "
              f"importance as signal: {dark}")
    thin = sorted(((c["coverage"], name) for name, c in cov.items() if 0.0 < c["coverage"] < 0.10))
    if thin:
        print("  thin coverage (<10% known): " + ", ".join(f"{nm}={cv*100:.0f}%" for cv, nm in thin))

    # label.py emits ONE sample per mint, time-ordered by first-seen, so this
    # positional cut is a group-safe, look-ahead-free train/val split.
    cut = int(n * (1.0 - args.val_frac))
    train = lgb.Dataset(x[:cut], label=y[:cut], feature_name=FEATURE_NAMES)
    valid = lgb.Dataset(x[cut:], label=y[cut:], reference=train, feature_name=FEATURE_NAMES)

    params = {
        "objective": "binary",
        "metric": "auc",
        "num_leaves": 31,
        "learning_rate": 0.05,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "min_data_in_leaf": 20,
        "verbose": -1,
        "num_threads": 1,            # match the live single-row predict path
    }
    # class-prior correction for the rare-positive target (shared with retrain.py); --no-balance to skip.
    from .retrain import balanced_params
    params = balanced_params(params, y[:cut], not args.no_balance)
    booster = lgb.train(
        params, train, num_boost_round=args.rounds, valid_sets=[valid],
        callbacks=[lgb.early_stopping(30), lgb.log_evaluation(50)],
    )
    booster.save_model(args.out)
    print(f"saved model -> {args.out}  (train={cut}, val={n - cut}, best_iter={booster.best_iteration})")
    print("importance:", dict(zip(FEATURE_NAMES, booster.feature_importance().tolist())))


if __name__ == "__main__":
    main()
