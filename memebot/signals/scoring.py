"""Rule-based momentum/quality score (0..1), scalp vs hold.

PHASE 1 STUB: a transparent weighted score so the paper loop can trade and we
can measure win-rate. Phase 2 replaces the ranking with a calibrated LightGBM
model; the scalp/hold weight split below mirrors the design doc.
"""
from __future__ import annotations

from ..config import Settings
from ..constants import MODE_HOLD, MODE_SCALP
from ..data.token_state import TokenState
from ..models import Candidate


def _norm(value: float, full_at: float) -> float:
    """0 at <=0, 1 at >= full_at."""
    if full_at <= 0:
        return 0.0
    return max(0.0, min(1.0, value / full_at))


def select_mode(st: TokenState) -> str:
    """Graduated + broad => eligible for HOLD; otherwise SCALP."""
    return MODE_HOLD if st.migrated else MODE_SCALP


def score_candidate(c: Candidate, s: Settings) -> float:
    m = s.momentum
    vol_spike = float(c.features.get("vol_spike", 0.0))

    s_volspike = _norm(vol_spike, m.volume_spike_mult)
    s_bsr = _norm(c.buy_sell_ratio, m.min_buy_sell_ratio * 1.5)
    s_volmcap = _norm(c.vol_to_mcap_pct, m.min_vol_to_mcap_pct * 2.0)   # saturate at 2x the gate (was 3x)
    s_buyers = _norm(float(c.unique_buyers), m.min_unique_buyers * 2.0)  # 2x the gate (was 3x)
    s_liq = _norm(c.liquidity_usd, s.risk.min_liquidity_usd * 5.0)
    s_vol_h1 = _norm(float(c.features.get("vol_h1", 0.0)), m.min_vol_h1 * 2.0)   # R2-a: 1h USD volume

    # R2-a (study + AI5): VOLUME is the #1 winner signal, buy/sell ratio is NOT predictive.
    # So lead with volume (vol_h1 + spike + vol/mcap) and cut the buy/sell weight to a minor 0.10.
    momentum = (0.30 * s_vol_h1 + 0.20 * s_volspike + 0.20 * s_volmcap
                + 0.20 * s_buyers + 0.10 * s_bsr)

    # No social term (the old `social = 0.5` was a fixed placeholder that only added a
    # dead +0.05 and capped headroom); its weight is redistributed to live features.
    if c.mode == MODE_SCALP:
        raw = 0.78 * momentum + 0.22 * s_liq
    else:  # MODE_HOLD — safety/quality weighted higher; known-safe now reaches 1.0
        safety_quality = 1.0 if not c.features.get("safety_unknown") else 0.4
        raw = 0.45 * safety_quality + 0.40 * momentum + 0.15 * s_liq

    # P10b SO4 (orchestration): rank DOWN the off-high / falling survivors the survival-first SETUP GATE
    # already distrusts, so the scorer's RANKING agrees with those separators. Multiplicative penalties
    # applied BEFORE the affine rescale (monotonic within the band) — they only ever REDUCE the score,
    # never relax a gate. Reuse setup_max_off_high_pct so the scorer and the gate share one threshold.
    trend = c.features.get("trend") or {}
    off_high = float(trend.get("off_high_pct", 0.0))
    if off_high > s.rule_offhigh_penalty_at:
        gate = max(s.rule_offhigh_penalty_at + 1e-6, s.setup_max_off_high_pct)
        frac = min(1.0, (off_high - s.rule_offhigh_penalty_at) / (gate - s.rule_offhigh_penalty_at))
        raw *= (1.0 - s.rule_offhigh_penalty_max * frac)
    if trend.get("dir") == "falling":
        raw *= (1.0 - s.rule_downtrend_penalty)

    # The rule sum is a heuristic RANK compressed into a narrow band (~[0.3,0.62]), not a
    # probability. Affine-rescale [ref_lo, ref_hi] -> [0,1] (strictly monotonic, so ranking
    # is preserved) so entry/high-conf/size thresholds operate on a real 0..1 scale.
    span = max(1e-6, s.rule_score_ref_hi - s.rule_score_ref_lo)
    c.score = max(0.0, min(1.0, (raw - s.rule_score_ref_lo) / span))
    return c.score


class Scorer:
    """Ranks a candidate 0..1. Uses the trained GBM if available (Phase 2),
    otherwise the transparent rule score (Phase 1). The result is written to
    c.score either way."""

    def __init__(self, settings: Settings, gbm=None) -> None:
        self.s = settings
        self.gbm = gbm

    @property
    def kind(self) -> str:
        return "gbm" if self.gbm is not None else "rule"

    def score(self, c: Candidate) -> float:
        if self.gbm is not None:
            try:
                c.score = self.gbm.score(c)
                return c.score
            except Exception:  # noqa: BLE001 — never let a model error stop scoring
                pass
        return score_candidate(c, self.s)
