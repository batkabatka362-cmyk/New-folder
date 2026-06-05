"""Rug-avoidance classifier study (NEW) — is the RUG more predictable than the WINNER?

The project has only ever trained WINNER classifiers (+50% target) and got AUC ~0.5 / NOISE — winners
are rare and noise-dominated. But the stated DOCTRINE is rug-AVOIDANCE (loss-minimisation). This flips
the target: predict the CATASTROPHIC outcome (rug or dead) and measure — over the SAME expanding-window
folds, head-to-head with the winner classifier on the identical mints — whether the rug class separates
MORE STABLY. The asymmetry that motivates it: a rug may carry structural tells (concentration, creator/
funder behaviour, liquidity drain) that a winner doesn't.

If the rug class separates better, the real edge is a rug-avoidance VETO: skip a buy when P(rug) is
high. That is survival-first and SAFE — it only ever SKIPS, never forces a buy, so a wrong veto costs
a missed trade (cheap) while a right one dodges a -90% (expensive). Honest + measured, not assumed.

  python -m memebot.backtest.rug_model [--folds 5]
"""
from __future__ import annotations

import argparse

from ..config import get_settings
from ..filter.features import FEATURE_NAMES, row_from_feats
from .dataset import build_dataset
from .gbm_stability import _fold_auc, fold_cuts, summarize_stability
from .label import _load_all_obs


def rug_label(outcome: str) -> int:
    """The AVOID class: a witnessed >=80% collapse (rug) OR unpriceable at the horizon (dead) — both
    are capital you lose. Everything else priced (winner/fade/flat/loser) = survived. glitch is
    excluded upstream (a pricing artifact, neither)."""
    return 1 if outcome in ("rug", "dead") else 0


def win_label(outcome: str) -> int:
    return 1 if outcome == "winner" else 0


def _auc_folds(xs, ys, cuts, threshold, balance) -> list[float]:
    aucs: list[float] = []
    for cut, end in cuts:
        r = _fold_auc(xs[:cut], ys[:cut], xs[cut:end], ys[cut:end], threshold, balance)
        if r is not None:
            aucs.append(r[0])
    return aucs


def dodge_tradeoff(rug_scores: list, winner_scores: list, thresholds: list) -> list[dict]:
    """The honest rug-VETO cost curve. At each P(rug) veto threshold T: rugs_dodged = fraction of rugs
    with score >= T (correctly skipped), winners_lost = fraction of winners with score >= T (wrongly
    skipped). 'Dodge 60-90% of rugs' is only a WIN if winners_lost stays much lower — on a weak
    separation the two move together. Pure."""
    nr, nw = len(rug_scores), len(winner_scores)
    out = []
    for t in thresholds:
        rd = (sum(1 for s in rug_scores if s >= t) / nr) if nr else 0.0
        wl = (sum(1 for s in winner_scores if s >= t) / nw) if nw else 0.0
        out.append({"threshold": round(t, 2), "rugs_dodged": rd, "winners_lost": wl})
    return out


def _oof_rug_scores(xs, recs, cuts, balance) -> tuple[list, list]:
    """Pooled OUT-OF-FOLD P(rug): each expanding-window fold trains on its prefix and predicts the next
    block (so every val mint is scored by a model that never saw it), tagged by its real outcome.
    Returns (rug_scores, winner_scores) for the dodge trade-off."""
    import lightgbm as lgb
    import numpy as np
    from .retrain import balanced_params
    y_rug = [rug_label(r["outcome"]) for r in recs]
    base = {"objective": "binary", "metric": "auc", "num_leaves": 31, "learning_rate": 0.05,
            "min_data_in_leaf": 20, "verbose": -1, "num_threads": 1}
    rug_s, win_s = [], []
    for cut, end in cuts:
        ytr = y_rug[:cut]
        if len(set(ytr)) < 2:
            continue
        ds = lgb.Dataset(np.asarray(xs[:cut], dtype=float), label=np.asarray(ytr, dtype=int), feature_name=FEATURE_NAMES)
        b = lgb.train(balanced_params(base, ytr, balance), ds, num_boost_round=80)
        preds = b.predict(np.asarray(xs[cut:end], dtype=float))
        for i, p in zip(range(cut, end), preds):
            oc = recs[i]["outcome"]
            if oc in ("rug", "dead"):
                rug_s.append(float(p))
            elif oc == "winner":
                win_s.append(float(p))
    return rug_s, win_s


def main() -> None:
    ap = argparse.ArgumentParser(description="Rug-avoidance vs winner classifier — which separates more stably?")
    ap.add_argument("--db", default=None)
    ap.add_argument("--horizon", type=float, default=300.0)
    ap.add_argument("--tol", type=float, default=900.0)
    ap.add_argument("--folds", type=int, default=5)
    args = ap.parse_args()
    try:
        import lightgbm  # noqa: F401
    except ImportError:
        raise SystemExit("pip install lightgbm numpy")

    s = get_settings()
    rows = _load_all_obs(args.db or s.db_path)
    recs = [r for r in build_dataset(rows, args.horizon, args.tol, indexed_only=True)
            if r["outcome"] != "glitch"]                      # drop pricing artifacts entirely
    n = len(recs)
    cuts = fold_cuts(n, args.folds)
    if not cuts:
        print(f"only {n} classified mints — not enough for {args.folds} folds yet; keep collecting.")
        return
    xs = [row_from_feats(r["features"]) for r in recs]        # serve-consistent encoding (shared)
    y_rug = [rug_label(r["outcome"]) for r in recs]
    y_win = [win_label(r["outcome"]) for r in recs]
    from collections import Counter
    dist = Counter(r["outcome"] for r in recs)

    print(f"\nrug-avoidance vs winner separation | n={n} | outcomes={dict(dist)}")
    print(f"  rug+dead (AVOID) rate: {sum(y_rug) / n * 100:.0f}%   winner rate: {sum(y_win) / n * 100:.0f}%\n")

    rug = summarize_stability(_auc_folds(xs, y_rug, cuts, 0.5, s.gbm_balance_classes))
    win = summarize_stability(_auc_folds(xs, y_win, cuts, 0.5, s.gbm_balance_classes))
    for name, st in (("RUG-avoidance (predict rug/dead)", rug), ("WINNER (predict +50%)", win)):
        print(f"  {name:34} mean AUC {st['mean']:.3f} +/- {st['std']:.3f} "
              f"[{st['min']:.3f},{st['max']:.3f}] over {st['folds']} folds -> {st['verdict']}")

    # what does the (more-stable) rug signal actually LEAN ON? full-data fit, importances only —
    # diagnostic, NOT deployed. Tells us whether the rug edge is a real tell (concentration / creator /
    # liquidity-drain) or scattered noise.
    try:
        import lightgbm as lgb
        import numpy as np
        from .retrain import balanced_params
        base = {"objective": "binary", "metric": "auc", "num_leaves": 31, "learning_rate": 0.05,
                "min_data_in_leaf": 20, "verbose": -1, "num_threads": 1}
        ds = lgb.Dataset(np.asarray(xs, dtype=float), label=np.asarray(y_rug, dtype=int), feature_name=FEATURE_NAMES)
        booster = lgb.train(balanced_params(base, y_rug, s.gbm_balance_classes), ds, num_boost_round=60)
        imp = sorted(zip(FEATURE_NAMES, booster.feature_importance().tolist()), key=lambda kv: -kv[1])
        top = [(name, i) for name, i in imp if i > 0][:6]
        print("  rug tells (top features the rug model leans on): " +
              (", ".join(f"{name}={i}" for name, i in top) if top else "none — the signal is scattered/noise"))
        print()
    except Exception:  # noqa: BLE001
        pass

    # THE honest answer to "can we dodge 60-90% of rugs like the good traders?" — the rug-VETO cost curve
    # on out-of-fold P(rug). What fraction of rugs each veto threshold skips, and the winners it costs.
    try:
        rs, ws = _oof_rug_scores(xs, recs, cuts, s.gbm_balance_classes)
        if rs and ws:
            print(f"  rug-veto trade-off (skip when out-of-fold P(rug) >= T) | {len(rs)} rugs, {len(ws)} winners")
            print(f"    {'P(rug)>=':>8}  {'rugs_dodged':>11}  {'winners_lost':>12}")
            for r in dodge_tradeoff(rs, ws, [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]):
                print(f"    {r['threshold']:>8}  {r['rugs_dodged'] * 100:>10.0f}%  {r['winners_lost'] * 100:>11.0f}%")
            print("    -> dodging 60-90% of rugs is only a WIN if winners_lost stays MUCH lower; on a weak "
                  "separation they move together. This is the honest ceiling on 'filter out the rugs'.")
            print()
    except Exception:  # noqa: BLE001
        pass

    if rug["folds"] and win["folds"]:
        if rug["mean"] > win["mean"] + 0.03 and rug["verdict"] != "NOISE":
            print("VERDICT: the RUG class separates MORE stably than the winner — a rug-avoidance VETO is "
                  "the more promising, doctrine-aligned use of a model. Validate forward before wiring it.")
        elif win["mean"] > rug["mean"] + 0.03:
            print("VERDICT: neither beats the other decisively; the winner edge (such as it is) is not "
                  "worse than the rug edge here. Keep collecting.")
        else:
            print("VERDICT: rug and winner separate about equally (both likely near chance on this data). "
                  "Keep collecting; the rug framing is still the SAFER one to deploy if either firms up.")


if __name__ == "__main__":
    main()
