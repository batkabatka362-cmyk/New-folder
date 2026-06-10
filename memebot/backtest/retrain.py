"""E3 / P4 — periodic GBM retrain with a SAFE, in-distribution deploy gate.

Regenerates the 5m fixed-horizon dataset from the live DB, trains a candidate
LightGBM (forward time-split), and DEPLOYS it (to gbm_model_path) only if it clears
ALL gates: forward-val AUC >= max(floor, currently-deployed) AND — the P4 addition —
operating-threshold PRECISION beats the positive base rate by a margin on a non-trivial
number of predicted-positives (AUC alone hides a useless entry point on a rare-event
imbalanced problem). It NEVER deploys a worse model. Run on a cadence as data accrues.

P4 in-distribution training (only once dataquality shows ~hundreds of labelable_5m_indexed
mints with both classes — NOT the survivorship-biased candidates set):
    python -m memebot.backtest.retrain --population observations --indexed-only --death-drop 0.9

Legacy (gate-survivor candidates, AUC-only spirit but now also precision-gated):
    python -m memebot.backtest.retrain [--horizon 300 --up 0.5 --tol 90 --min-auc 0.6]
"""
from __future__ import annotations

import argparse
import os

from ..config import get_settings
from ..filter.features import FEATURE_NAMES, row_from_feats
from .label import _load_all_obs, fixed_horizon_labels

_PARAMS = {
    "objective": "binary", "metric": "auc", "num_leaves": 31, "learning_rate": 0.05,
    "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1,
    "min_data_in_leaf": 20, "verbose": -1, "num_threads": 1,
}


def balanced_params(base: dict, y_train, balance: bool = True) -> dict:
    """Class-prior correction for the rare-positive 5m target. Without it the model's probabilities
    cluster near the low base rate, so it predicts ~zero positives at the entry threshold (an AUC that
    ranks fine but gives no usable entry point). scale_pos_weight = n_neg/n_pos shifts the minority
    class up. Pure; a class-prior fix (correct regardless of sample size), NOT data-snooping. No-op
    when disabled or a split is single-class."""
    if not balance:
        return dict(base)
    pos = sum(1 for v in y_train if int(v) == 1)
    neg = len(y_train) - pos
    if pos <= 0 or neg <= 0:
        return dict(base)
    return {**base, "scale_pos_weight": neg / pos}


def precision_over_base(y_true, y_score, threshold: float):
    """Pure (no numpy): precision among PREDICTED-positives (score>=threshold) vs the positive
    base rate. Returns (precision, base_rate, n_predicted_positive). For a rare-event imbalanced
    problem, this is the metric that matters — AUC can look fine while the operating point fires
    mostly on negatives (precision ~= base rate = no usable edge)."""
    n = len(y_true)
    if n == 0:
        return 0.0, 0.0, 0
    base = sum(y_true) / n
    pred_pos = [yt for yt, ys in zip(y_true, y_score) if ys >= threshold]
    npp = len(pred_pos)
    prec = (sum(pred_pos) / npp) if npp else 0.0
    return prec, base, npp


def should_deploy(candidate_auc: float, deployed_auc: float, min_auc: float, *,
                  precision: float | None = None, base_rate: float | None = None,
                  min_precision_lift: float = 0.0, n_pred_pos: int = 0, min_pred_pos: int = 10) -> bool:
    """Deploy only if the candidate clears the AUC floor AND is no worse than what's live. When
    precision metrics are supplied (P4), ALSO require the operating-threshold precision to beat the
    positive base rate by >= min_precision_lift, on >= min_pred_pos predicted-positives — so we never
    ship a model whose AUC looks fine but whose actual entry point has no precision edge over chance."""
    if candidate_auc < max(min_auc, deployed_auc):
        return False
    if precision is not None:
        if n_pred_pos < min_pred_pos:               # too few predicted-positives -> precision is noise
            return False
        if precision < (base_rate or 0.0) + min_precision_lift:
            return False
    return True


def _train(xs, ys, threshold: float, val_frac: float = 0.2, rounds: int = 300, balance: bool = True):
    """Forward-split train; returns (booster, val_auc, val_metrics) or (None, 0.0, {}) if untrainable.
    val_metrics carries precision/base_rate/n_pred_pos at the operating `threshold` for the deploy gate."""
    import lightgbm as lgb
    import numpy as np
    x, y = np.asarray(xs, dtype=float), np.asarray(ys, dtype=int)
    n = len(y)
    cut = int(n * (1.0 - val_frac))
    if cut < 20 or n - cut < 10 or y[:cut].min() == y[:cut].max() or y[cut:].min() == y[cut:].max():
        return None, 0.0, {}   # too few / single-class in a split -> not trainable
    tr = lgb.Dataset(x[:cut], label=y[:cut], feature_name=FEATURE_NAMES)
    va = lgb.Dataset(x[cut:], label=y[cut:], reference=tr, feature_name=FEATURE_NAMES)
    params = balanced_params(_PARAMS, y[:cut], balance)   # class-prior correction (rare-positive target)
    b = lgb.train(params, tr, num_boost_round=rounds, valid_sets=[va],
                  callbacks=[lgb.early_stopping(30), lgb.log_evaluation(0)])
    auc = float(b.best_score["valid_0"]["auc"])
    val_scores = b.predict(x[cut:])
    prec, base, npp = precision_over_base([int(v) for v in y[cut:]], [float(v) for v in val_scores], threshold)
    return b, auc, {"precision": prec, "base_rate": base, "n_pred_pos": npp}


def run_once(s, *, db=None, horizon=300.0, up=0.5, tol=90.0, min_auc=0.6, population="observations",
             indexed_only=True, death_drop=0.9, min_precision_lift=0.05, min_pred_pos=10,
             log_fn=print) -> dict:
    """One label -> train -> SAFE-gate -> deploy-or-keep cycle. Reusable by the CLI and the bot's
    autonomous retrain loop. Returns {'action', 'auc', 'deployed', 'n', 'pos', ...}; never deploys a
    model that doesn't clear the AUC floor AND beat the operating-threshold precision over base rate.
    Importing lightgbm is the caller's responsibility (the loop guards it)."""
    rows = _load_all_obs(db or s.db_path, source=population)
    indexed_only = indexed_only or population == "observations"
    dd = death_drop if death_drop and death_drop > 0 else None
    labeled = fixed_horizon_labels(rows, horizon, up, tol, death_drop=dd, indexed_only=indexed_only)
    if len(labeled) < 50:
        log_fn(f"retrain: only {len(labeled)} labeled mints at +{horizon:.0f}s "
               f"(pop={population}, indexed_only={indexed_only}, death_drop={dd}) -- collect more data.")
        return {"action": "insufficient", "n": len(labeled)}
    xs = [row_from_feats(f) for _m, f, _l, _fwd in labeled]   # serve-consistent FEATURE_DEFAULTS encoding
    ys = [lbl for _m, _f, lbl, _fwd in labeled]
    pos = sum(ys)
    threshold = s.gbm_entry_threshold
    booster, auc, vm = _train(xs, ys, threshold, balance=s.gbm_balance_classes)
    if booster is None:
        log_fn(f"retrain: not trainable yet (n={len(ys)}, positives={pos}) — need both classes per split.")
        return {"action": "untrainable", "n": len(ys), "pos": pos}
    auc_file = s.gbm_model_path + ".auc"
    deployed = 0.0
    if os.path.exists(auc_file):
        try:
            deployed = float(open(auc_file).read().strip())
        except (ValueError, OSError):
            deployed = 0.0
    log_fn(f"retrain: candidate AUC {auc:.3f} | deployed {deployed:.3f} | floor {min_auc:.3f} | "
           f"n={len(ys)} pos={pos} ({pos / len(ys) * 100:.0f}%) | val_precision {vm['precision']:.3f} "
           f"vs base {vm['base_rate']:.3f} (+lift {min_precision_lift:.2f}, n_pred_pos={vm['n_pred_pos']}) @ thr {threshold}")
    out = {"action": "kept", "auc": auc, "deployed": deployed, "n": len(ys), "pos": pos, **vm}
    if should_deploy(auc, deployed, min_auc, precision=vm["precision"], base_rate=vm["base_rate"],
                     min_precision_lift=min_precision_lift, n_pred_pos=vm["n_pred_pos"], min_pred_pos=min_pred_pos):
        booster.save_model(s.gbm_model_path)
        with open(auc_file, "w") as fh:
            fh.write(f"{auc:.4f}")
        log_fn(f"retrain: DEPLOYED -> {s.gbm_model_path} (restart the bot to load it).")
        out["action"] = "deployed"
    else:
        why = ("AUC below bar" if auc < max(min_auc, deployed)
               else f"precision {vm['precision']:.3f} < base {vm['base_rate']:.3f}+{min_precision_lift:.2f} "
                    f"or n_pred_pos {vm['n_pred_pos']}<{min_pred_pos}")
        log_fn(f"retrain: KEPT the current model ({why}).")
        out["why"] = why
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Retrain the GBM and deploy only if it improves.")
    ap.add_argument("--db", default=None)
    ap.add_argument("--horizon", type=float, default=300.0)
    ap.add_argument("--up", type=float, default=0.5)
    ap.add_argument("--tol", type=float, default=90.0)
    ap.add_argument("--min-auc", type=float, default=0.6)
    ap.add_argument("--population", choices=("candidates", "observations"), default="candidates",
                    help="training population: 'candidates' (gate-survivors, historical default) or "
                         "'observations' (P2 unbiased in-distribution set -- pair with P4's in-distribution work)")
    ap.add_argument("--indexed-only", action="store_true",
                    help="P4: label only IN-DISTRIBUTION snapshots (liquidity>0); auto-on for --population observations")
    ap.add_argument("--death-drop", type=float, default=0.0,
                    help="P4: death-aware recall — a witnessed >= this-fraction collapse labels 0 (0=off, e.g. 0.9)")
    ap.add_argument("--min-precision-lift", type=float, default=0.05,
                    help="P4: require val precision at the entry threshold to beat the base rate by >= this")
    ap.add_argument("--min-pred-pos", type=int, default=10,
                    help="P4: minimum predicted-positives on the val split for the precision estimate to count "
                         "(small val sets early in P4 may need a lower value)")
    args = ap.parse_args()
    s = get_settings()
    try:
        import lightgbm  # noqa: F401
    except ImportError:
        raise SystemExit("pip install lightgbm numpy")
    run_once(s, db=args.db, horizon=args.horizon, up=args.up, tol=args.tol, min_auc=args.min_auc,
             population=args.population, indexed_only=args.indexed_only, death_drop=args.death_drop,
             min_precision_lift=args.min_precision_lift, min_pred_pos=args.min_pred_pos)


if __name__ == "__main__":
    main()
