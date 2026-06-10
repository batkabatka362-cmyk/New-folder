"""Signal separation monitor (WL9) — do the LOG-only winner/rug signals actually SEPARATE on the
realized book? The single read-out to run as data accrues, before any of them earns a veto.

Every on-chain free-data signal we have measured separates at AUC ~0.5 on the traded set, so the new
LOG-only signals — the vision IMAGE scam-score (WL6), the free-data SMART-MONEY confluence (WL9), and the
entry-latency / branding-reuse captures — are all logged onto each trade's entry features and must EARN
a veto by separating first. This walks `trade_outcomes`, and for each signal key reports: n scored, the
winner-vs-loser medians, and AUC (the honest Mann-Whitney separation). >0.6 (or <0.4) = a real tell worth
gating; ~0.5 = noise, keep it LOG-only.

  python -m memebot.backtest.signal_separation
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from ..config import get_settings

_GLITCH_RET_MULT = 20.0          # same pricing-glitch ceiling as pnl_reconstruct
# (key, higher_is_worse) — does a HIGHER value mean a worse outcome (a scam/rug tell) or a better one?
_SIGNALS = (
    ("image_scam_score", True),     # higher = more scam-looking -> expect losers higher
    ("smart_buyer_count", False),   # more smart early-buyers -> expect WINNERS higher (the confluence edge)
    ("dumper_buyer_count", True),   # more dumper early-buyers -> expect losers higher
    ("early_buyers", False),        # broader early breadth -> mild winner lean (organic vs sniped)
    ("name_reuse_count", True),     # branding reuse -> scam-factory (found NOT to separate on the traded set)
    ("entry_latency_s", True),      # slower entry -> chasing (found NOT robust)
    ("st_risk_score", True),        # WL14 Solana Tracker 1-10 risk score (higher = riskier)
    ("st_top10", True),             # WL14 top-10 holder concentration % (higher = worse)
    ("st_snipers_pct", True),       # WL14 sniper share % (higher = worse)
    ("st_insiders_pct", True),      # WL14 insider share % (higher = worse)
    ("st_bundlers_pct", True),      # WL14 bundled coordinated-wallet supply % (the literature's #1 rug tell)
    ("st_dev_pct", True),           # WL14 dev/creator current holding % (higher = more dump risk)
)


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2.0


def _auc(pos: list[float], neg: list[float]) -> float:
    """Mann-Whitney AUC = P(pos > neg), ties 0.5. pos/neg are the two groups; 0.5 = no separation."""
    if not pos or not neg:
        return 0.0
    wins = sum(1.0 if a > b else 0.5 if a == b else 0.0 for a in pos for b in neg)
    return wins / (len(pos) * len(neg))


def pairs_for(rows: list[tuple], key: str) -> list[tuple[float, bool]]:
    """rows = [(mint, pnl_sol, pnl_pct, entry_features_json)] -> [(signal_value, won?)] for mints whose
    entry carried `key`. Per-mint slice PnL summed; pricing-glitch round-trips excluded."""
    agg: dict[str, float] = defaultdict(float)
    val: dict[str, float] = {}
    glitch: set[str] = set()
    for mint, pnl_sol, pnl_pct, ef in rows:
        if pnl_pct is not None and (1.0 + float(pnl_pct)) > _GLITCH_RET_MULT:
            glitch.add(mint)
            continue
        agg[mint] += float(pnl_sol or 0.0)
        if mint not in val:
            try:
                d = json.loads(ef or "{}")
            except (ValueError, TypeError):
                d = {}
            if key in d and d[key] is not None:
                try:
                    val[mint] = float(d[key])
                except (TypeError, ValueError):
                    pass
    return [(val[m], agg[m] > 0) for m in val if m not in glitch]


def separate(pairs: list[tuple[float, bool]], higher_is_worse: bool = True) -> dict:
    """Pure: winner/loser medians + AUC oriented so >0.5 means the signal points the EXPECTED way
    (higher_is_worse -> AUC(loser>winner); else AUC(winner>loser))."""
    win = [v for v, w in pairs if w]
    lose = [v for v, w in pairs if not w]
    auc = _auc(lose, win) if higher_is_worse else _auc(win, lose)
    return {"n": len(pairs), "n_win": len(win), "n_lose": len(lose),
            "win_med": _median(win), "lose_med": _median(lose), "auc": auc}


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
    out = {}
    for key, hiw in _SIGNALS:
        out[key] = {"higher_is_worse": hiw, **separate(pairs_for(rows, key), hiw)}
    return out


def main() -> None:
    s = get_settings()
    res = load(s.db_path)
    print("=== SIGNAL SEPARATION (WL9) — do the LOG-only signals separate on the realized book? ===")
    print(f"  {'signal':20} {'n':>4} {'win_med':>9} {'lose_med':>9} {'AUC':>6}  verdict")
    any_data = False
    for key, hiw in _SIGNALS:
        r = res[key]
        if r["n"] < 8:
            print(f"  {key:20} {r['n']:>4} {'—':>9} {'—':>9} {'—':>6}  not enough scored trades yet")
            continue
        any_data = True
        wm = "n/a" if r["win_med"] is None else f"{r['win_med']:.3g}"
        lm = "n/a" if r["lose_med"] is None else f"{r['lose_med']:.3g}"
        v = ("SEPARATES — candidate veto" if r["auc"] >= 0.6 else "noise (keep LOG-only)")
        print(f"  {key:20} {r['n']:>4} {wm:>9} {lm:>9} {r['auc']:>6.2f}  {v}  ({'hi=bad' if hiw else 'hi=good'})")
    if not any_data:
        print("\n  No signal has >=8 scored trades yet — enable the scorers (BUYER_INTEL_ENABLED / "
              "IMAGE_SCAM_ENABLED) and let buys accrue, then re-run.")
    else:
        print("\n  AUC >= 0.60 (oriented) = a real separator worth a calibrated veto; ~0.50 = noise. "
              "LOG-first: nothing gates a buy until it clears this bar on enough trades.")


if __name__ == "__main__":
    main()
