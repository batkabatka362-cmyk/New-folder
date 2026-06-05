"""Canonical feature extraction — the SINGLE source of truth shared by the
live GBM scorer (filter/gbm.py) and offline training (backtest/train_gbm.py).

If you add/reorder a feature here, both scoring and training stay in sync
automatically. Unknown tri-state safety flags encode as -1 so the model can
learn "unverified" as its own signal.
"""
from __future__ import annotations

from ..constants import MODE_HOLD
from ..models import Candidate

# Order is load-bearing: the model is trained on this exact column order. The TRAJECTORY / VELOCITY
# block (P8) is APPENDED after the original static block so any prefix-trained artifact stays valid.
# It carries what token_state already computes but the model was BLIND to — the actual separator
# between a fade and a winner is the path + the velocity, not the static snapshot level.
FEATURE_NAMES = [
    "liquidity_usd",
    "market_cap_usd",
    "vol_to_mcap_pct",
    "buy_sell_ratio",
    "unique_buyers",
    "vol_spike",
    "vol_h1",
    "mint_revoked",            # tri-state: 1 yes / 0 no / -1 unknown
    "freeze_revoked",
    "lp_burned_pct",           # -1 unknown
    "top5_concentration_pct",  # -1 unknown
    "is_hold",                 # 1 if HOLD mode else 0
    # ── P8 trajectory / velocity (was computed by token_state but dropped before the model) ──
    "price_change_m5",         # DexScreener 5-min % change
    "price_change_h1",         # DexScreener 1-hour % change
    "trend_dir",               # polled-trend direction: rising 1 / choppy 0 / falling -1 / unknown -2
    "trend_chg_pct",           # % change over the polled window
    "off_high_pct",            # % below the recent polled high (catching-a-knife tell)
    "ema_signal",              # EMA cross: bull 1 / flat 0 / bear -1 / n-a -2
    "breakout",                # vs recent range: up 1 / none 0 / down -1
    "age_min",                 # token age in minutes (a 3-min and 50-min token are different animals)
    "vol_per_min",             # AGE-NORMALIZED volume velocity: vol_h1 / minutes-of-life (capped at 60)
    "liq_trend_dir",           # polled-liquidity direction: rising 1 / flat 0 / falling -1 / unknown -2 (falling = pool draining = rug pre-tell)
    "liq_trend_chg_pct",       # % change of the polled liquidity series
    "creator_holding_pct",     # % of supply the CREATOR wallet still holds (0 = dumped their allocation = strong rug tell; -1 unknown)
    # ── G3 tape signals (only populated for trade-stream-watched tokens; -1 unknown) ──
    "sniper_share",            # share of buy volume by the first-N wallets (high = snipers dominate, they dump)
    "creator_dump_ratio",      # creator sell/buy on the live tape (0 holding .. >=0.5 dumping; real-time dev dump)
    "bundle_share",            # share of buy volume from coordinated same-block BUNDLES (the research's #1 missing signal; concealed concentration)
    "smart_money_share",       # share of buy volume from wallets with a PROFITABLE cross-token track record (copy-trade; the POSITIVE axis; -1 unknown)
    "buyer_growth",            # N13: change in the rolling-1h buy count across the polled window (breadth velocity; signed; 0 = unknown/flat, like trend_chg_pct)
    # P10b SR6: support/resistance distance — already computed by technicals() + shown to the brain, but
    # the model was blind to it. APPENDED at the END so any prefix-trained artifact stays valid; 0.0
    # default matches what technicals() emits while warming up, so old logged rows encode 'unknown' the same.
    "res_dist_pct",            # % to the recent resistance (overhead room)
    "sup_dist_pct",            # % above the recent support (downside cushion)
]

# categorical encodings (shared so the backtest inverse in simulate.py stays faithful). "choppy"
# (price) and "flat" (liquidity) both encode 0.0 — a flat/sideways series.
TREND_DIR_CODE = {"rising": 1.0, "choppy": 0.0, "flat": 0.0, "falling": -1.0, "unknown": -2.0}
EMA_CODE = {"bull": 1.0, "flat": 0.0, "bear": -1.0, "n/a": -2.0}
BREAKOUT_CODE = {"up": 1.0, "none": 0.0, "down": -1.0}


def _tri(b: bool | None) -> float:
    return -1.0 if b is None else (1.0 if b else 0.0)


def build_features(c: Candidate) -> dict[str, float]:
    trend = c.features.get("trend") or {}
    tech = c.features.get("tech") or {}
    liq_trend = c.features.get("liq_trend") or {}
    age_min = float(c.features.get("age_s", 0.0)) / 60.0
    vol_h1 = float(c.features.get("vol_h1", 0.0))
    return {
        "liquidity_usd": float(c.liquidity_usd),
        "market_cap_usd": float(c.market_cap_usd),
        "vol_to_mcap_pct": float(c.vol_to_mcap_pct),
        "buy_sell_ratio": float(c.buy_sell_ratio),
        "unique_buyers": float(c.unique_buyers),
        "vol_spike": float(c.features.get("vol_spike", 0.0)),
        "vol_h1": vol_h1,
        "mint_revoked": _tri(c.mint_revoked),
        "freeze_revoked": _tri(c.freeze_revoked),
        "lp_burned_pct": -1.0 if c.lp_burned_pct is None else float(c.lp_burned_pct),
        "top5_concentration_pct": -1.0 if c.top5_concentration_pct is None else float(c.top5_concentration_pct),
        "is_hold": 1.0 if c.mode == MODE_HOLD else 0.0,
        "price_change_m5": float(c.price_change_m5),
        "price_change_h1": float(c.price_change_h1),
        "trend_dir": TREND_DIR_CODE.get(str(trend.get("dir", "unknown")), -2.0),
        "trend_chg_pct": float(trend.get("chg_pct", 0.0)),
        "off_high_pct": float(trend.get("off_high_pct", 0.0)),
        "ema_signal": EMA_CODE.get(str(tech.get("ema_signal", "n/a")), -2.0),
        "breakout": BREAKOUT_CODE.get(str(tech.get("breakout", "none")), 0.0),
        "age_min": age_min,
        "vol_per_min": (vol_h1 / max(1.0, min(60.0, age_min))) if age_min > 0 else 0.0,
        "liq_trend_dir": TREND_DIR_CODE.get(str(liq_trend.get("dir", "unknown")), -2.0),
        "liq_trend_chg_pct": float(liq_trend.get("chg_pct", 0.0)),
        "creator_holding_pct": (-1.0 if c.features.get("creator_holding_pct") is None
                                else float(c.features["creator_holding_pct"])),
        "sniper_share": (-1.0 if c.features.get("sniper_share") is None
                         else float(c.features["sniper_share"])),
        "creator_dump_ratio": (-1.0 if c.features.get("creator_dump_ratio") is None
                               else float(c.features["creator_dump_ratio"])),
        "bundle_share": (-1.0 if c.features.get("bundle_share") is None
                         else float(c.features["bundle_share"])),
        "smart_money_share": (-1.0 if c.features.get("smart_money_share") is None
                              else float(c.features["smart_money_share"])),
        # N13: signed breadth velocity. 0.0 default (neutral/unknown) — NOT a -1 sentinel, because the
        # value is a SIGNED delta and -1 is a real growth; matches the trend_chg_pct/vol_per_min pattern.
        "buyer_growth": float(c.features.get("buyer_growth", 0.0)),
        # P10b SR6: 0.0 = warming up (technicals() emits 0.0 before there's enough history) — serve-consistent.
        "res_dist_pct": float(tech.get("res_dist_pct", 0.0)),
        "sup_dist_pct": float(tech.get("sup_dist_pct", 0.0)),
    }


def feature_vector(c: Candidate) -> list[float]:
    f = build_features(c)
    return [f[name] for name in FEATURE_NAMES]


# Per-feature default for a MISSING key in an OLD logged row (logged before a feature was added).
# It MUST match what the live serve-path (build_features) emits for an unknown/absent value, or a
# model retrained over a window straddling the schema change learns spurious structure (a blanket
# 0.0 would encode a missing trend_dir as the REAL "choppy" code, not "unknown"). Unknown safety
# tri-states -> -1; unknown trend/ema categoricals -> -2; everything else -> 0.0.
FEATURE_DEFAULTS = {name: 0.0 for name in FEATURE_NAMES}
FEATURE_DEFAULTS.update({
    "mint_revoked": -1.0, "freeze_revoked": -1.0, "lp_burned_pct": -1.0, "top5_concentration_pct": -1.0,
    "trend_dir": -2.0, "ema_signal": -2.0, "liq_trend_dir": -2.0, "creator_holding_pct": -1.0,
    "sniper_share": -1.0, "creator_dump_ratio": -1.0, "bundle_share": -1.0, "smart_money_share": -1.0,
})


def row_from_feats(f: dict) -> list[float]:
    """Build the FEATURE_NAMES row from a stored feature dict, defaulting a MISSING key to its
    serve-consistent FEATURE_DEFAULTS value (NOT a blanket 0.0). Used by the offline labelers so old
    rows encode 'unknown' the same way the live scorer does."""
    return [float(f.get(name, FEATURE_DEFAULTS[name])) for name in FEATURE_NAMES]


def feature_coverage(rows: list) -> dict[str, dict]:
    """N6: per-feature data coverage over FEATURE_NAMES-ordered rows. For each column reports how many
    rows carry a NON-default ('known') value and whether the column is CONSTANT (no variance). A
    constant / near-zero-coverage column contributes NOTHING to a GBM — and crucially, its LightGBM
    importance must NOT be read as 'the model uses this signal' (e.g. a G3-tape column that is all -1
    on the no-stream population). `rows` is an iterable of float lists in FEATURE_NAMES order."""
    rows = list(rows)
    n = len(rows)
    out: dict[str, dict] = {}
    for i, name in enumerate(FEATURE_NAMES):
        col = [r[i] for r in rows if i < len(r)]
        default = FEATURE_DEFAULTS[name]
        # "known" = AWAY from the serve sentinel (the model can't tell a real sentinel-valued reading
        # from a missing one either), so this is serve-consistent — NOT literal missing-ness. It thus
        # slightly understates presence for tri-states whose real value can equal the sentinel
        # (e.g. a genuine mint_revoked=0). Intended; don't "fix" it to literal NaN counting.
        known = sum(1 for v in col if v != default)
        constant = len(set(col)) <= 1 if col else True
        out[name] = {"n": n, "known": known,
                     "coverage": (known / n if n else 0.0), "constant": constant}
    return out


def dark_features(coverage: dict[str, dict], min_coverage: float = 0.01) -> list[str]:
    """N6: the features that would enter a GBM as DEAD weight — CONSTANT (no variance) or with
    'known' coverage below min_coverage. These must be flagged so their model importance isn't
    mistaken for real signal, and so the dark G3-tape columns can't silently masquerade as detection."""
    return [name for name, c in coverage.items() if c["constant"] or c["coverage"] < min_coverage]
