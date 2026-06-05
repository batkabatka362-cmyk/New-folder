"""Deterministic rule gate — hard vetoes evaluated before any scoring.

Two kinds of gate:
  - MARKET gates (liquidity, vol/mcap, buy/sell breadth) — always computable
    from DexScreener / curve data.
  - SAFETY gates (mint/freeze authority, LP burn, concentration) — only enforced
    when the on-chain data is present (None == not yet checked). Wire Helius so
    these activate; until then they are skipped and `safety_unknown` is flagged.

Microsecond, free, auditable. The GBM scorer (Phase 2) only ranks survivors.
"""
from __future__ import annotations

from ..config import Settings
from ..models import Candidate


def scam_likelihood(c: Candidate, s: Settings) -> float:
    """A graded 0..1 danger estimate the brain WEIGHS (distinct from evaluate()'s hard veto):
    0 = clean, 1 = very likely scam. ACTIVE authority is the danger; UNKNOWN is mild uncertainty."""
    g = s.safety
    score = 0.0
    score += 0.35 if c.mint_revoked is False else (0.10 if c.mint_revoked is None else 0.0)
    score += 0.35 if c.freeze_revoked is False else (0.10 if c.freeze_revoked is None else 0.0)
    if c.lp_burned_pct is not None and c.lp_burned_pct < g.min_lp_burned_pct:
        score += 0.20
    # R2-b (study): top-holder concentration is graded — >30% is the manipulation tell
    # (~half of manipulated tokens), >90% is extreme. (top5 excludes the pool vault.)
    conc = c.top5_concentration_pct
    if conc is not None:
        if conc > g.max_top5_concentration_pct:
            score += 0.20
        elif conc > g.concentration_warn_pct:
            score += 0.10
    # W5 (behavioral): a creator wallet that has spam-launched many tokens is a serial-spam/rug
    # factory. SOFT/graded (the brain weighs it; not a hard veto) — live data showed launch count
    # is a real but noisy tell (worst rugs from 65/67-launch creators, a winner from 20).
    launches = c.features.get("creator_launches")
    if launches is not None and launches > g.creator_spam_launches:
        score += 0.15
    # N12: the creator's FUNDER spawned a plausible-operator BAND of distinct creators -> a serial rug
    # factory hiding behind per-token wallet rotation (the launch count above misses this). The band
    # auto-excludes infrastructure (a funder above serial_max funds everyone, not one operation).
    fcc = c.features.get("funder_creator_count")
    if fcc is not None and g.funder_serial_min <= fcc <= g.funder_serial_max:
        score += 0.15
    # HOLDER funding-cluster: several "independent" top holders funded from ONE wallet = concealed
    # single-entity concentration (the #1 missing-signal's free-data form). Graded scam penalty,
    # advisory (the vault/CEX false-positive risk + noise keep it off the hard veto); calibrate from data.
    hfc = c.features.get("holder_funder_cluster")
    if hfc is not None and hfc >= g.holder_cluster_warn:
        score += 0.20
    # DUPLICATION (user insight): the SAME name+ticker reused across many mints = a scam-factory reusing
    # branding (a fresh creator/funder can't hide the reused identity). SOFT + smaller weight (popular
    # memes are reused legitimately, so it's noisier than the wallet tells); advisory, calibrate from data.
    nrc = c.features.get("name_reuse_count")
    if nrc is not None and nrc > g.name_reuse_warn:
        score += 0.10
    # P8 (behavioral, LEARNED): a creator whose OWN past tokens rugged often (over enough classified
    # samples) is a graded danger — our realized memory of who burned us, not just launch count.
    rug_rate = c.features.get("creator_rug_rate")
    rep_n = c.features.get("creator_rep_n", 0)
    if rug_rate is not None and rep_n >= g.creator_rep_min_tokens and rug_rate >= g.creator_rep_rug_rate:
        score += 0.15
    # P8: a pool DRAINING before we even buy is a rug-in-progress — the static liquidity LEVEL doesn't
    # separate winners from rugs (study), but the DIRECTION is a stronger pre-tell. Graded, advisory.
    lt = c.features.get("liq_trend") or {}
    if lt.get("dir") == "falling" and lt.get("chg_pct", 0.0) <= -g.liq_drain_warn_pct:
        score += 0.15
    # The CREATOR has DUMPED its allocation (holds ~0% of supply). This is a rug tell ONLY when the
    # coin is otherwise weak — but (user insight) a creator-sold token that is STILL THRIVING is the
    # GOLD setup, not a scam: the dev overhang is GONE and the demand is organic, so you only compete
    # with the other holders. So penalize the dump ONLY when the price isn't already proving strength;
    # a rising / positive-momentum token with the dev fully out earns NO penalty (and the brain gets a
    # bullish note). Aligns the scoring with the constitution's "creator garsan ch ... trendline" nuance.
    chp = c.features.get("creator_holding_pct")
    if chp is not None and chp < g.creator_sold_pct:
        # "thriving" = a confirmed uptrend, OR a positive 1-HOUR change WHILE the live tape isn't rolling
        # over. h1 lags (a launch pump keeps h1 green for ~an hour even as it dumps NOW), so a stale-green
        # h1 alone must NOT excuse the creator dump — require the short tape not be falling (trend!='falling'
        # and m5>=0). Defaults to the rug penalty on unknown/flat. (m5 defaults 0.0 when unpriced -> safe.)
        trend = c.features.get("trend") or {}
        thriving = (trend.get("dir") == "rising"
                    or (c.price_change_h1 > 0.0 and trend.get("dir") != "falling" and c.price_change_m5 >= 0.0))
        if not thriving:
            score += 0.15
    # P8 (G3 tape): snipers dominate the early buys -> they'll dump on whoever buys next.
    ss = c.features.get("sniper_share")
    if ss is not None and ss >= g.sniper_share_warn:
        score += 0.15
    # P8 (G3 tape): the CREATOR is SELLING on the live tape — a real-time dev dump, the strongest
    # rug tell of all (it's happening right now, not a stale snapshot).
    cd = c.features.get("creator_dump_ratio")
    if cd is not None and cd >= g.creator_dump_warn:
        score += 0.20
    # P8 (G3 tape, research's #1 missing signal): coordinated same-block BUNDLES dominate the buys ->
    # the true ownership is concealed/concentrated and will dump. Graded, advisory.
    bs = c.features.get("bundle_share")
    if bs is not None and bs >= g.bundle_share_warn:
        score += 0.15
    return min(1.0, score)


def holder_quality(c: Candidate, s: Settings) -> str:
    """Coarse holder-base health driving the hold-vs-scalp lean (user doctrine: good
    holders -> HOLD long; bad/concentrated -> take a small profit and exit fast).
    'healthy' favors HOLD, 'concentrated' favors SCALP-and-exit, else neutral/unknown."""
    conc = c.top5_concentration_pct
    if conc is None:
        return "unknown"
    if conc >= s.holder_cut_pct:
        return "concentrated"          # whales dominate -> scalp & bail
    if conc <= s.holder_good_pct:
        return "healthy"               # broad distribution -> can hold for a larger move
    return "mixed"


def evaluate(c: Candidate, s: Settings) -> Candidate:
    reasons: list[str] = []
    g, m, r = s.safety, s.momentum, s.risk

    # ── market gates ──────────────────────────────────────────────────────────
    if c.liquidity_usd > 0 and c.liquidity_usd < r.min_liquidity_usd:
        reasons.append(f"liquidity<{r.min_liquidity_usd:g}")
    if c.vol_to_mcap_pct > 0 and c.vol_to_mcap_pct < m.min_vol_to_mcap_pct:
        reasons.append("vol/mcap_low")
    # Guard breadth gates like the liquidity/vol gates above: only veto when the
    # data is actually present. Missing breadth (no DexScreener snapshot yet, e.g.
    # pre-index SCALP tokens) is a skip, not a hard veto via the 0 default. The
    # scorer + entry_threshold still keep us from trading on empty data.
    if c.buy_sell_ratio > 0 and c.buy_sell_ratio < m.min_buy_sell_ratio:
        reasons.append("buy/sell_low")
    if c.unique_buyers > 0 and c.unique_buyers < m.min_unique_buyers:
        reasons.append("buyers_low")

    # ── safety gates (only when data is known) ──────────────────────────────────
    if g.require_mint_revoked and c.mint_revoked is False:
        reasons.append("mint_not_revoked")
    if g.require_freeze_revoked and c.freeze_revoked is False:
        reasons.append("freeze_not_revoked")
    # A1: a dangerous Token-2022 extension (transfer hook / permanent delegate / default-frozen /
    # high transfer fee) is a stealth rug vector beyond classic mint/freeze -> HARD veto. None = none
    # detected / not Token-2022 / unknown, so this only ever fires on a positively-detected danger.
    if g.require_token2022_safe and c.token2022_risk:
        reasons.append(f"token2022:{c.token2022_risk}")
    if c.lp_burned_pct is not None and c.lp_burned_pct < g.min_lp_burned_pct:
        reasons.append("lp_not_burned")
    # NOTE: top5_concentration_pct is ADVISORY, not a hard veto. getTokenLargestAccounts
    # can't reliably separate the pool/curve vault from a dev whale, so a rank-based gate
    # risks excusing the very whale it should catch. It's fed to the brain as a signal;
    # a reliable hard gate needs the pool-vault address / owner-dedup (Phase 4, Bitquery/DAS).

    if c.mint_revoked is None and c.freeze_revoked is None:
        c.features["safety_unknown"] = True

    c.rule_reasons = reasons
    c.rule_passed = not reasons
    return c
