"""WL6 read-side — does the vision IMAGE scam-score actually separate rugs from winners?

This is the validation loop for the image scorer (the user's visual edge, the ONE untested modality —
every on-chain free-data signal we have measured separates at AUC ~0.5 on the traded set). The scorer
(`agent/vision.py`, OFF by default) writes `image_scam_score` onto a trade's entry features; this reads
those scores, joins to the realized (glitch-excluded) outcome, and asks the honest question: do the
mints we LOST on score HIGHER (more scam-looking) than the ones we won on? Then it sweeps candidate veto
thresholds (rugs-cut vs winners-cut, with precision) so a veto can be calibrated FROM LABELS — never by
feel, and only if the image actually separates.

Offline, read-only, ADVISORY. Honest by construction: it reports "not enough scored trades yet" until
the scorer is enabled (`ollama pull llava`, `IMAGE_SCAM_ENABLED=true`) and buys accrue a score.

  python -m memebot.backtest.image_separation
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from ..config import get_settings

_GLITCH_RET_MULT = 20.0          # same pricing-glitch ceiling as pnl_reconstruct / brain_audit
_CANDIDATE_THRESHOLDS = (0.5, 0.6, 0.7, 0.8, 0.9)


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2.0


def _auc(pos: list[float], neg: list[float]) -> float:
    """Mann-Whitney AUC = P(pos > neg) with ties at 0.5. Here pos=losers, neg=winners, so >0.5 means
    losers score HIGHER (the image scorer is picking up scam-look). 0.5 = no separation."""
    if not pos or not neg:
        return 0.0
    wins = sum(1.0 if a > b else 0.5 if a == b else 0.0 for a in pos for b in neg)
    return wins / (len(pos) * len(neg))


def score_and_outcome(rows: list[tuple]) -> dict[str, tuple[float, bool]]:
    """rows = [(mint, pnl_sol, pnl_pct, entry_features_json)] -> mint -> (image_scam_score, won?).
    Per-mint slice PnL summed; glitch round-trips excluded; only mints whose entry carried a score."""
    agg: dict[str, float] = defaultdict(float)
    score: dict[str, float] = {}
    glitch: set[str] = set()
    for mint, pnl_sol, pnl_pct, ef in rows:
        if pnl_pct is not None and (1.0 + float(pnl_pct)) > _GLITCH_RET_MULT:
            glitch.add(mint)
            continue
        agg[mint] += float(pnl_sol or 0.0)
        if mint not in score:
            try:
                d = json.loads(ef or "{}")
            except (ValueError, TypeError):
                d = {}
            v = d.get("image_scam_score")
            if v is not None:
                try:
                    score[mint] = float(v)
                except (TypeError, ValueError):
                    pass
    return {m: (score[m], agg[m] > 0) for m in score if m not in glitch}


def analyze(pairs: dict[str, tuple[float, bool]],
            thresholds: tuple[float, ...] = _CANDIDATE_THRESHOLDS) -> dict:
    """Pure: separation AUC (losers vs winners image-score) + a veto-threshold sweep."""
    win = [s for s, w in pairs.values() if w]
    lose = [s for s, w in pairs.values() if not w]
    sweep = []
    for t in thresholds:
        rugs_cut = sum(1 for s in lose if s >= t)
        wins_cut = sum(1 for s in win if s >= t)
        denom = rugs_cut + wins_cut
        sweep.append({"threshold": t, "losers_cut": rugs_cut, "winners_cut": wins_cut,
                      "precision": (rugs_cut / denom) if denom else 0.0})
    return {"n": len(pairs), "n_win": len(win), "n_lose": len(lose),
            "winner_med": _median(win), "loser_med": _median(lose),
            "auc": _auc(lose, win), "sweep": sweep}


def load(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        try:
            rows = conn.execute(
                "SELECT mint, pnl_sol, pnl_pct, entry_features FROM trade_outcomes").fetchall()
        except sqlite3.OperationalError:
            rows = []
    finally:
        conn.close()
    return analyze(score_and_outcome(rows))


def main() -> None:
    s = get_settings()
    a = load(s.db_path)
    print("=== IMAGE SCAM-SCORE SEPARATION (WL6) — does the vision score separate rugs from winners? ===")
    if a["n"] < 8:
        print(f"  only {a['n']} scored trades yet — not enough to judge. Enable the scorer to accrue:")
        print("    ollama pull llava   &&   set IMAGE_SCAM_ENABLED=true   &&   restart the bot")
        print("  each buy then logs an image_scam_score; re-run as they accrue. Read-only/advisory.")
        return
    wm = "n/a" if a["winner_med"] is None else f"{a['winner_med']:.2f}"
    lm = "n/a" if a["loser_med"] is None else f"{a['loser_med']:.2f}"
    print(f"  {a['n']} scored trades ({a['n_win']} winners / {a['n_lose']} losers)")
    print(f"  median image scam-score:  winners {wm}   losers {lm}")
    print(f"  >> AUC(loser > winner) = {a['auc']:.3f}   (0.5 = no separation; >0.6 = the image is a real tell)")
    print(f"\n  {'veto @ score':>12}  {'rugs_cut':>9}  {'winners_cut':>12}  {'precision':>10}")
    for r in a["sweep"]:
        print(f"  {r['threshold']:>12.2f}  {r['losers_cut']:>9}  {r['winners_cut']:>12}  {r['precision']*100:>9.0f}%")
    print("\n  -> if losers score materially higher (AUC > ~0.6) AND a threshold cuts many rugs at few")
    print("     winners, THAT calibrates an image veto. Until then it stays LOG-only. ADVISORY.")


if __name__ == "__main__":
    main()
