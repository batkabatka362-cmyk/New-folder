"""Deterministic GOOD-SETUP classifier (survival-first, free-tier).

We can't catch launches, so we trade DexScreener-indexed tokens caught LATE — and
the only way to survive a late entry is to refuse the two patterns that bleed:
  1) the already-pumped-now-fading token (high momentum, far off its high) — the
     momentum score ranks these HIGHEST right before they dump on a late buyer;
  2) the thin / ghost indexed pool where the ~9.5% round-trip fee guarantees a loss.

`classify_setup` returns (grade, reasons, quality): grade in {good, marginal, bad}.
A `bad` setup is SKIPPED regardless of its raw score. `quality` (0..1) scales size so
marginal setups risk less. Thresholds are config (tunable/backtestable), not edges.
"""
from __future__ import annotations

from ..config import Settings
from ..constants import MODE_HOLD
from ..filter.rules import scam_likelihood
from ..models import Candidate


def setup_type(c: Candidate, s: Settings) -> str:
    """Classify the SITUATION / playbook (the 'workflow' to apply), orthogonal to the good/marginal/bad
    grade — the structured situation-classification a skilled trader does (recognize the setup, apply the
    right playbook + timeframe). The brain reasons within the right frame; we log it to study which
    TYPES actually win vs rug. Survival-first ORDERING: the riskiest label wins (a token can be both
    'gold' and 'risky' -> caution dominates).

      risky        — elevated scam-likelihood (below the hard veto) or warn-zone concentration: smaller/faster.
      creator_gold — the dev is fully OUT but the price proves organic strength (user's gold setup).
      safe_hold    — a cleanly graduated token (both authorities revoked) holding up: can hold for a bigger move.
      momentum     — positive short-term action, not (yet) one of the stronger frames: scalp-grade attention.
      unproven     — no clear strength signal yet: default skip-lean.
    """
    g = s.safety
    trend = (c.features.get("trend") or {}).get("dir")
    chp = c.features.get("creator_holding_pct")
    conc = c.top5_concentration_pct
    # SUSTAINED strength (same bar as the creator-sold gold test) — not a single 5-min blip.
    # P10b SO3: require non-negative SHORT-TERM action even on the rising-trend branch, so a token whose
    # 1h trend label is 'rising' but whose price is ROLLING OVER right now (m5<0) is NOT promoted to
    # creator_gold/safe_hold — it falls through to momentum/unproven (the cautious 'defined-move/SKIP'
    # playbook). Survival-first: the strongest labels demand sustained m5, not a stale rising label.
    thriving = c.price_change_m5 >= 0.0 and (trend == "rising" or (c.price_change_h1 > 0.0 and trend != "falling"))
    if scam_likelihood(c, s) >= s.setup_risky_scam or (conc is not None and conc >= g.concentration_warn_pct):
        return "risky"
    if chp is not None and chp < g.creator_sold_pct and thriving:
        return "creator_gold"
    if c.mode == MODE_HOLD and c.mint_revoked and c.freeze_revoked and thriving:
        return "safe_hold"
    if thriving or c.price_change_m5 > 0.0:
        return "momentum"
    return "unproven"


def classify_setup(c: Candidate, s: Settings) -> tuple[str, list[str], float]:
    g = s
    trend = c.features.get("trend") or {}
    tech = c.features.get("tech") or {}
    tdir = trend.get("dir", "unknown")
    off_high = float(trend.get("off_high_pct", 0.0))
    liq = float(c.liquidity_usd)
    mcap = max(1.0, float(c.market_cap_usd))
    liq_mcap = liq / mcap
    bsr = float(c.buy_sell_ratio)
    buyers = int(c.unique_buyers)

    # ── hard vetoes -> bad (skip no matter how high the score) ──────────────────
    reasons: list[str] = []
    if liq < g.setup_min_liquidity_usd:
        reasons.append("thin_liquidity")
    if liq_mcap < g.setup_min_liq_mcap_ratio:
        reasons.append("ghost_liq_mcap")           # tiny liq vs a big mcap = uninvestable
    if off_high >= g.setup_max_off_high_pct:
        reasons.append("far_off_high")             # already dumped from the top = falling knife
    if c.price_change_h1 >= g.setup_max_h1_pump_for_fade and c.price_change_m5 <= 0:
        reasons.append("post_pump_fade")           # huge 1h pump now rolling over
    if tdir == "falling" and c.price_change_m5 < 0:
        reasons.append("downtrend")
    if tech.get("breakout") == "down" or tech.get("ema_signal") == "bear":
        reasons.append("breaking_down")
    if bsr > g.setup_bsr_hi and buyers < 5:
        reasons.append("wash_spike")               # 1-sided spike from a few wallets reverts
    if scam_likelihood(c, s) >= 0.5 or c.freeze_revoked is False:
        reasons.append("unsafe")
    if reasons:
        return "bad", reasons, 0.0

    # ── soft quality (0..1) for ranking / size scaling ──────────────────────────
    q_offhigh = max(0.0, 1.0 - off_high / max(1.0, g.setup_max_off_high_pct))   # near the high = better
    q_buyers = min(1.0, buyers / max(1, g.setup_min_buyers_good))
    q_liqmcap = min(1.0, liq_mcap / 0.10)
    q_ema = 1.0 if tech.get("ema_signal") == "bull" else 0.5
    q_m5 = 1.0 if c.price_change_m5 > 0 else 0.4
    quality = round((q_offhigh + q_buyers + q_liqmcap + q_ema + q_m5) / 5.0, 3)

    # ── grade: good vs marginal. Unknown trend / thin breadth is NEVER "good" ───
    if tdir == "unknown" or buyers < g.setup_min_buyers_marginal:
        return "marginal", [], quality
    good = (buyers >= g.setup_min_buyers_good
            and tdir in ("rising", "choppy")
            and g.setup_bsr_lo <= bsr <= g.setup_bsr_hi
            and tech.get("ema_signal") != "bear")
    return ("good" if good else "marginal"), [], quality
