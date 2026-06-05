"""Gate rug-dodge attribution (spec section 7) — the honest "is the safety gate catching rugs?" read.

The separation / rug_model tools ask whether the SCORE/FEATURES separate winners from rugs. This asks
the complementary, deterministic question the rug-avoidance doctrine actually lives on: of the tokens
the SAFETY GATE rejected, how many were genuinely HARMFUL (rug/dead) — a correct dodge — vs how many
were WINNERS we wrongly threw away (the false-reject cost)? And, per named gate reason (mint/freeze/
token2022/...), which check earns its keep.

It joins each mint's forward OUTCOME (from the logged price history, same labeler as separation) with
whether the gate ever PASSED it, into a rug-avoidance confusion matrix. The per-reason breakdown needs
the gate reasons logged into the observation features ('rule_reasons') — populated going forward — so a
deterministic mechanism check (e.g. the A1 Token-2022 hard-fail, which dodges rugs at ~zero winner cost
because a honeypot is never a winner) becomes measurable as data accrues.

Offline, read-only, no network. Honest caveat: 'gate passed' = the gate let the mint through at some
logged point; safety-gate coverage depends on the Helius budget, so a 'pass' can mean 'safety not yet
fetched'. Treat the numbers as a coarse, improving read, not a verdict.

  python -m memebot.backtest.gate_attribution --horizon 300
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter

from ..config import get_settings
from .dataset import build_dataset
from .label import _load_all_obs

_HARMFUL = {"rug", "dead"}          # the catastrophic outcomes the gate exists to dodge
_KEEP = {"winner"}                  # what a false-reject throws away


def _gate_by_mint(db_path: str) -> dict[str, dict]:
    """Per mint: {'passed': bool (the gate let it through at some logged point), 'reasons': Counter}.
    reasons come from the observation features 'rule_reasons' list when present (logged going forward)."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT mint, rule_passed, features FROM observations").fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    out: dict[str, dict] = {}
    for mint, rule_passed, feats in rows:
        g = out.setdefault(mint, {"passed": False, "reasons": Counter()})
        if rule_passed:
            g["passed"] = True
        else:
            try:
                f = json.loads(feats or "{}")
            except (ValueError, TypeError):
                continue
            for r in (f.get("rule_reasons") or []):
                g["reasons"][str(r)] += 1
    return out


def attribute(recs: list[dict], gate: dict[str, dict]) -> dict:
    """Join per-mint outcome with gate pass/reject -> rug-avoidance confusion + per-reason dodge.
    Pure; returns {} on no data."""
    harmful_total = sum(1 for r in recs if r["outcome"] in _HARMFUL)
    keep_total = sum(1 for r in recs if r["outcome"] in _KEEP)
    cm = {"dodged": 0, "lost_winner": 0, "miss": 0, "kept_winner": 0}
    reason_dodge: Counter = Counter()      # gate reason -> harmful tokens it (co-)rejected
    reason_falsereject: Counter = Counter()  # gate reason -> winners it (co-)rejected
    for r in recs:
        g = gate.get(r["mint"])
        if g is None:
            continue
        passed, outcome = g["passed"], r["outcome"]
        if outcome in _HARMFUL:
            cm["miss" if passed else "dodged"] += 1
            if not passed:
                for reason in g["reasons"]:
                    reason_dodge[reason] += 1
        elif outcome in _KEEP:
            cm["kept_winner" if passed else "lost_winner"] += 1
            if not passed:
                for reason in g["reasons"]:
                    reason_falsereject[reason] += 1
    return {
        "harmful_total": harmful_total, "keep_total": keep_total,
        "cm": cm,
        "rug_dodge_recall": (cm["dodged"] / harmful_total) if harmful_total else 0.0,
        "winner_loss_rate": (cm["lost_winner"] / keep_total) if keep_total else 0.0,
        "reason_dodge": dict(reason_dodge), "reason_falsereject": dict(reason_falsereject),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Gate rug-dodge attribution (read-only).")
    ap.add_argument("--horizon", type=float, default=None, help="forward label horizon seconds")
    args = ap.parse_args()
    s = get_settings()
    horizon = args.horizon if args.horizon is not None else s.calibrate_horizon_s
    rows = _load_all_obs(s.db_path)
    if not rows:
        print("no observations yet.")
        return
    recs = build_dataset(rows, horizon, s.calibrate_tol_s)
    gate = _gate_by_mint(s.db_path)
    a = attribute(recs, gate)
    if not a["harmful_total"] and not a["keep_total"]:
        print("not enough labeled winners/rugs yet — keep accruing.")
        return
    cm = a["cm"]
    print(f"Gate rug-dodge attribution | {a['harmful_total']} harmful (rug/dead) / {a['keep_total']} winners labeled")
    print("(coarse: 'passed' = the gate let it through at some logged point; safety coverage is Helius-budget bound)\n")
    print(f"  RUGS DODGED (rejected & harmful):   {cm['dodged']:>4}  -> rug-dodge recall {a['rug_dodge_recall']*100:.0f}%")
    print(f"  RUGS MISSED (passed  & harmful):    {cm['miss']:>4}  (the gate let these through)")
    print(f"  WINNERS LOST (rejected & winner):   {cm['lost_winner']:>4}  -> winner-loss rate {a['winner_loss_rate']*100:.0f}%")
    print(f"  WINNERS KEPT (passed  & winner):    {cm['kept_winner']:>4}")
    print("\n  -> the honest lever: raise rug-dodge WITHOUT raising winner-loss. A mechanism check")
    print("     (honeypot / token-2022 / can't-sell) dodges rugs at ~zero winner cost; a statistical")
    print("     veto trades them off. This is where the A1/A2/B1 deterministic checks should show a lift.")
    if a["reason_dodge"]:
        print("\n  per-gate-reason dodge (rugs co-rejected by each reason | winners co-rejected):")
        for reason, n in sorted(a["reason_dodge"].items(), key=lambda kv: kv[1], reverse=True):
            print(f"    {reason:28} rugs {n:>4}  | winners {a['reason_falsereject'].get(reason, 0):>4}")
    else:
        print("\n  per-reason breakdown: no 'rule_reasons' logged yet (populates going forward post-deploy).")


if __name__ == "__main__":
    main()
