"""Self-calibration: turn the classified dataset into ADVISORY threshold suggestions + per-creator
reputation (P8 self-improvement). Pure, offline, deterministic — the bot uses these to LEARN from
its own outcomes, but they are NEVER auto-applied to the live risk config (deterministic risk must
not be silently re-tuned by a model). Suggestions are logged + stored as a recalled lesson; creator
reputation is fed to the brain as an advisory signal.
"""
from __future__ import annotations

# features worth comparing winner-vs-rug, with a human-readable "higher is worse?" direction.
# direction = +1 means a HIGHER median in rugs is the danger (suggest an upper bound / penalty);
# -1 means a LOWER median in rugs is the danger (suggest a lower floor). NOTE: only features that
# are naturally >= 0 (no unknown sentinel) belong here — top5_concentration_pct is DELIBERATELY
# excluded: it encodes unknown as -1 (the common case on the public RPC), so a naive median compare
# fabricates a fake separation (e.g. winner -1 vs rug 95). Concentration has its own coverage-aware
# handling (the entry veto, the conc_rise cut, holder_quality); calibrate it there, not here.
# Each entry: (feature, direction, label, signed). `signed`=True means the feature can be
# LEGITIMATELY negative (a velocity/trajectory signal) — so the unknown-sentinel skip (w<0/r<0) must
# NOT drop it; signed features default to 0 (= no change) when absent, never -1. P10b CAL2 widened
# this to the trajectory/velocity features the separation module already tests, since RESEARCH.md says
# DIRECTION (momentum rolling over, pool draining, breadth shrinking) separates better than static level.
_WATCH = [
    ("sell_pressure", +1, "sell pressure on the tape", False),
    ("creator_launches", +1, "creator launch count", False),
    ("buy_sell_ratio", -1, "buy/sell ratio", False),
    ("vol_h1", -1, "1h volume", False),
    ("liquidity_usd", -1, "liquidity", False),
    ("vol_per_min", -1, "volume velocity (per minute)", False),
    ("vol_to_mcap_pct", -1, "volume / mcap", False),
    ("off_high_pct", +1, "distance below the recent high", False),
    # signed velocity/trajectory signals (a negative value is real, not an unknown sentinel):
    ("price_change_m5", -1, "5m price momentum", True),
    ("price_change_h1", -1, "1h price change", True),
    ("trend_chg_pct", -1, "rolling price trend", True),
    ("buyer_growth", -1, "unique-buyer breadth velocity", True),
    ("liq_trend_chg_pct", -1, "liquidity trend (pool draining)", True),
]


def _sep(winner: float, rug: float) -> float:
    """Symmetric relative separation of two medians, in [0,1]. ~0 = overlapping (noise);
    higher = the feature genuinely diverges between winners and rugs."""
    denom = abs(winner) + abs(rug)
    return abs(winner - rug) / denom if denom > 1e-9 else 0.0


def suggest_thresholds(summary: dict, *, min_class_n: int = 5, min_sep: float = 0.2) -> list[dict]:
    """Compare the WINNER and RUG per-class median features and emit a suggestion for each feature
    whose medians DIVERGE cleanly (>= min_sep) with enough samples. A feature that overlaps is noise
    and is skipped — so the bot proposes calibrating ONLY what its own data actually separates.
    Returns suggestions sorted by separation (strongest first). Advisory; never auto-applied."""
    classes = (summary or {}).get("classes", {})
    win, rug = classes.get("winner"), classes.get("rug")
    if not win or not rug or win["n"] < min_class_n or rug["n"] < min_class_n:
        return []
    out = []
    for feat, direction, label, signed in _WATCH:
        w, r = float(win.get(feat, 0.0)), float(rug.get(feat, 0.0))
        # P10b CAL2: only NON-signed features use the unknown-sentinel guard. For a signed velocity
        # feature a negative median is a real signal (momentum down / pool draining), not missing data,
        # so dropping it would discard exactly the trajectory separators we want to detect.
        if not signed and (w < 0 or r < 0):
            continue              # unknown-data sentinel (-1) contaminates a non-negative median
        sep = _sep(w, r)
        if sep < min_sep:
            continue
        rug_higher = r > w
        # only emit when the divergence matches the KNOWN danger direction (else it's a confusing
        # inversion we shouldn't act on without more data)
        if (direction > 0) != rug_higher:
            continue
        if direction > 0:
            note = (f"rugs run HIGHER {label} (rug median {r:.2f} vs winner {w:.2f}) "
                    f"-> weight an upper bound / penalty on it")
        else:
            note = (f"rugs run LOWER {label} (rug median {r:.2f} vs winner {w:.2f}) "
                    f"-> demand a floor on it")
        out.append({"feature": feat, "winner": w, "rug": r, "separation": sep, "note": note})
    out.sort(key=lambda x: x["separation"], reverse=True)
    return out


def creator_reputation(classified: list[dict], mint_creator: dict, *, min_tokens: int = 3,
                       now_ts: float | None = None, half_life_s: float = 0.0) -> dict:
    """Learn each creator's REALIZED outcome track record from our own classified tokens. Returns
    {creator: {n, rugs, wins, rug_rate, win_rate, rug_rate_recent, win_rate_recent}} for creators with
    >= min_tokens classified tokens (below that the rate is too noisy to weigh). The bot's own memory
    of who burned it.

    P10b CAL3: when half_life_s>0 and the recs carry 't0', ALSO compute a RECENCY-WEIGHTED rate where
    each token's contribution decays as 0.5 ** (age / half_life_s) — so a creator that rugged long ago
    but has launched cleanly since is judged less harshly (a reformed/active operator), while a fresh
    rug still weighs full. Raw integer counts are kept for the min_tokens gate + honest display. Advisory
    only — this feeds the brain, NEVER the deterministic risk layer (rules.py keeps the count-based rate)."""
    decay_on = half_life_s > 0 and now_ts is not None
    agg: dict[str, dict] = {}
    for rec in classified:
        creator = mint_creator.get(rec.get("mint"))
        if not creator:
            continue
        a = agg.setdefault(creator, {"n": 0, "rugs": 0, "wins": 0, "w": 0.0, "w_rugs": 0.0, "w_wins": 0.0})
        a["n"] += 1
        w = 0.5 ** (max(0.0, now_ts - float(rec.get("t0", now_ts))) / half_life_s) if decay_on else 1.0
        a["w"] += w
        outcome = rec.get("outcome")
        if outcome == "rug":
            a["rugs"] += 1
            a["w_rugs"] += w
        elif outcome == "winner":
            a["wins"] += 1
            a["w_wins"] += w
    out = {}
    for creator, a in agg.items():
        if a["n"] < min_tokens:
            continue
        wd = a["w"] if a["w"] > 1e-9 else float(a["n"])
        out[creator] = {
            "n": a["n"], "rugs": a["rugs"], "wins": a["wins"],
            "rug_rate": a["rugs"] / a["n"], "win_rate": a["wins"] / a["n"],
            "rug_rate_recent": (a["w_rugs"] / wd) if decay_on else a["rugs"] / a["n"],
            "win_rate_recent": (a["w_wins"] / wd) if decay_on else a["wins"] / a["n"],
        }
    return out


def readiness(summary: dict, *, min_per_class: int = 30) -> dict:
    """#1 data-readiness: is the classified set big enough, with BOTH winners and rugs, to calibrate
    / train on? Reports the counts + a boolean so the log can say honestly when we're data-ready."""
    classes = (summary or {}).get("classes", {})
    n = (summary or {}).get("n", 0)
    wins = classes.get("winner", {}).get("n", 0)
    rugs = classes.get("rug", {}).get("n", 0)
    return {
        "n": n, "winners": wins, "rugs": rugs,
        "ready": wins >= min_per_class and rugs >= min_per_class,
    }
