"""Winner-loss attribution (spec section 7) — WHICH safety-gate check is rejecting WINNERS?

The gate dodges ~51% of rugs but loses ~41% of winners (see gate_attribution). That false-reject is the
#1 lever on the loss-minimisation net: every winner the gate wrongly throws away is forgone upside. This
re-derives the gate REASON offline on historical data — reconstruct each rejected mint's candidate from
its logged features, re-run rules.evaluate, and read which named reason fired — then tallies, per reason,
the WINNERS it wrongly rejected vs the RUGS it correctly dodged.

The verdict per reason: a check that loses many winners but dodges few rugs is TOO STRICT -> calibrate it
looser (raise the net by keeping winners); a check that dodges rugs at low winner cost EARNS ITS KEEP.

Offline, read-only, advisory (it NEVER auto-tunes the live config). Faithful-enough: the market + safety
gate fields are reconstructed by candidate_from_features; some scam_likelihood sub-features absent from
old logs default to no-penalty, so scam_likelihood is slightly UNDER-counted (it can only miss a reason,
never invent one) — and the gate's ACTUAL live pass/reject decision (observations.rule_passed) is used
for the kept/lost split, so only the REASON is re-derived, not the decision.

  python -m memebot.backtest.winner_loss --horizon 300
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict

from ..config import get_settings
from ..filter.rules import evaluate
from .dataset import build_dataset
from .label import _load_all_obs
from .simulate import candidate_from_features

_HARMFUL = {"rug", "dead"}
_KEEP = {"winner"}


def _gate_facts(db_path: str) -> dict[str, dict]:
    """Per mint: {'passed': bool (the gate let it through at some logged point), 'feats': the most-
    resolved feature dict, 'price': its price}. The most-resolved row (most feature keys) carries the
    safety fields needed to re-derive a safety-gate reason."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT mint, rule_passed, price_usd, features FROM observations").fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    out: dict[str, dict] = {}
    for mint, rule_passed, price, feats in rows:
        try:
            f = json.loads(feats or "{}")
        except (ValueError, TypeError):
            f = {}
        g = out.setdefault(mint, {"passed": False, "feats": {}, "price": 0.0, "_keys": -1})
        if rule_passed:
            g["passed"] = True
        if len(f) > g["_keys"]:            # keep the most-resolved feature snapshot
            g["feats"], g["price"], g["_keys"] = f, float(price or 0.0), len(f)
    return out


def _reasons_for(feats: dict, price: float, settings) -> list[str]:
    """Re-derive the gate reasons by reconstructing the candidate + re-running the REAL rule gate."""
    c = candidate_from_features(feats, price)
    evaluate(c, settings)
    return list(c.rule_reasons)


def attribute(recs: list[dict], gate: dict[str, dict], settings) -> dict:
    """Per gate reason: winners wrongly rejected vs rugs correctly dodged (from the mints the gate
    ACTUALLY rejected). Pure-ish (re-runs the rule gate). Returns {} on no data."""
    win_lost: Counter = Counter()      # reason -> winners the gate rejected (carrying that reason)
    rug_dodged: Counter = Counter()    # reason -> rugs the gate rejected
    n_win_lost = n_rug_dodged = 0
    for r in recs:
        g = gate.get(r["mint"])
        if g is None or g["passed"]:
            continue                    # gate kept it -> not a reject; only attribute the REJECTED mints
        outcome = r["outcome"]
        if outcome not in _HARMFUL and outcome not in _KEEP:
            continue
        reasons = _reasons_for(g["feats"], g["price"], settings)
        if not reasons:
            continue                    # the gate rejected it live but our re-derivation can't explain it
        if outcome in _KEEP:
            n_win_lost += 1
            for reason in set(reasons):
                win_lost[reason] += 1
        else:
            n_rug_dodged += 1
            for reason in set(reasons):
                rug_dodged[reason] += 1
    reasons_all = set(win_lost) | set(rug_dodged)
    table = []
    for reason in reasons_all:
        w, d = win_lost[reason], rug_dodged[reason]
        table.append({"reason": reason, "winners_lost": w, "rugs_dodged": d,
                      "precision": (d / (d + w)) if (d + w) else 0.0})   # rugs / (rugs + winners) it rejected
    table.sort(key=lambda x: x["winners_lost"], reverse=True)
    return {"n_win_lost": n_win_lost, "n_rug_dodged": n_rug_dodged, "table": table}


def main() -> None:
    ap = argparse.ArgumentParser(description="Winner-loss attribution (which gate check rejects winners). Read-only/advisory.")
    ap.add_argument("--horizon", type=float, default=None)
    args = ap.parse_args()
    s = get_settings()
    horizon = args.horizon if args.horizon is not None else s.calibrate_horizon_s
    rows = _load_all_obs(s.db_path)
    if not rows:
        print("no observations yet.")
        return
    recs = build_dataset(rows, horizon, s.calibrate_tol_s)
    a = attribute(recs, _gate_facts(s.db_path), s)
    if not a["table"]:
        print("not enough rejected winners/rugs to attribute yet — keep accruing.")
        return
    print(f"Winner-loss attribution | {a['n_win_lost']} rejected WINNERS / {a['n_rug_dodged']} rejected RUGS "
          "(gate's actual live rejects; reason re-derived offline)")
    print("(advisory — never auto-tunes the live gate; scam_likelihood is slightly under-counted on old logs)\n")
    print(f"  {'gate reason':28} {'winners_lost':>12} {'rugs_dodged':>12} {'precision':>10}  verdict")
    for r in a["table"]:
        v = ("TOO STRICT (loosen)" if r["winners_lost"] > r["rugs_dodged"]
             else "earns its keep" if r["rugs_dodged"] >= 2 * max(1, r["winners_lost"])
             else "mixed")
        print(f"  {r['reason']:28} {r['winners_lost']:>12} {r['rugs_dodged']:>12} {r['precision']*100:>9.0f}%  {v}")
    print("\n  -> reasons rejecting more WINNERS than RUGS are the calibration levers: loosening them keeps")
    print("     winners (raising the loss-min net) at little rug cost. ADVISORY — calibrate, don't auto-apply.")


if __name__ == "__main__":
    main()
