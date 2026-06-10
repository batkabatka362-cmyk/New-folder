"""Central configuration, loaded once from environment (.env).

All tunable thresholds live here as *config*, never hardcoded in logic — they
are community heuristics, not validated edges, and must be backtested and
calibrated against real labeled outcomes (see backtest/).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

from .portfolio.portfolio import ExitParams   # exit rules live with Position; no import cycle (portfolio.portfolio imports only ..constants and ..execution.base, neither of which imports config — keep it that way)

try:
    from dotenv import load_dotenv
    from pathlib import Path as _Path

    # BUGFIX: load the repo-root .env by ABSOLUTE path, not the cwd. `load_dotenv()` with no args walks up
    # from the current working directory — and when the supervisor spawns `python -m memebot`, the child's
    # cwd is NOT guaranteed to be the repo, so the .env (and every override in it: BUYER_INTEL/IMAGE_SCAM/
    # the banded-funnel knobs) was silently NOT loaded and all those features ran at their OFF defaults.
    # Anchoring to this file's location (repo/memebot/config.py -> repo/.env) makes it cwd-independent.
    _ENV = _Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(_ENV if _ENV.exists() else None)
except ImportError:  # dotenv is optional at runtime
    pass


# ── env helpers ──────────────────────────────────────────────────────────────
def _str(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return v if v not in (None, "") else default


def _float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v in (None, ""):
        return default
    try:
        return float(v)
    except ValueError:
        raise SystemExit(f"Config error: {name}={v!r} is not a valid number (expected float).")


def _int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v in (None, ""):
        return default
    try:
        return int(v)
    except ValueError:
        raise SystemExit(f"Config error: {name}={v!r} is not a valid integer.")


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v in (None, ""):
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# ── threshold groups ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SafetyGates:
    """Hard binary vetoes. Reject a token if ANY trips. Both trade modes."""

    require_mint_revoked: bool = True       # mint authority must be null
    require_freeze_revoked: bool = True     # freeze authority must be null (honeypot)
    require_token2022_safe: bool = True     # A1: HARD-FAIL a Token-2022 mint carrying a dangerous extension (transfer hook / permanent delegate / default-frozen / high transfer fee) — stealth rug vectors beyond classic mint/freeze
    token2022_max_transfer_fee_pct: float = 5.0   # A1: a Token-2022 transfer-fee above this % is a stealth tax that eats the round-trip -> veto
    min_lp_burned_pct: float = 90.0         # else must be locked (check expiry, not existence)
    max_bundled_supply_pct: float = 50.0
    max_sniper_volume_pct: float = 50.0
    reject_creator_in_bundle: bool = True
    # W4 LIVE RECALIBRATION: fresh pump.fun graduated tokens are UNIFORMLY high-concentration —
    # live top-5 (non-vault) ran 82-96% (median ~89), so the old 30/90 thresholds (tuned for a
    # "normal" 0-90 range) sat at/below the median and would penalize/veto basically every token,
    # incl. winners. Re-centered onto the actual distribution; n thin -> re-tune as labeled data grows.
    max_top5_concentration_pct: float = 96.0   # extreme/near-total dev control (the entry whale-rug VETO + R2-b +0.20). Only the top tail.
    concentration_warn_pct: float = 90.0        # R2-b graded +0.10 above the memecoin baseline (was 30 = everything)
    creator_spam_launches: int = 40             # W5: a creator wallet that has launched > this many tokens is a serial-spam/rug factory -> graded scam penalty (live data: worst rugs came from 65/67-launch creators; a +237% winner from 20, so 40 catches the spammers without blocking that winner). SOFT signal, re-tune from labeled data.
    # N12 dev-wallet FUNDING-SOURCE clustering: operators rotate to a fresh creator wallet PER token but
    # fund them from ONE treasury, defeating creator_spam_launches. Re-link rotated creators by common
    # FUNDER; a funder that spawned a plausible-operator BAND of distinct creators is a serial rug
    # factory. The band AUTO-EXCLUDES infrastructure (CEX / pump.fun rails / faucets fund thousands, so
    # > serial_max = not a single operator) — no hand-maintained address denylist needed. SOFT/advisory.
    funder_serial_min: int = 3                  # >= this many distinct creators share the funder -> a real cluster (below = noise)
    funder_serial_max: int = 200               # ...but ABOVE this it's INFRASTRUCTURE funding everyone -> exclude (no penalty)
    # HOLDER funding-cluster (free-data concealed-concentration tell, RESEARCH.md's #1 missing signal via
    # FUNDING rather than the G3 same-block tape): resolve the top non-vault holders' owner wallets, trace
    # each owner's funder, and flag when several "independent" top holders share ONE funder = one entity
    # hiding behind many wallets. OFF by default — it costs several extra Helius reads per survivor
    # (getAccountInfo per holder + a funder trace each); enable when the RPC has headroom. ADVISORY only.
    holder_cluster_check: bool = False          # master toggle (cost-gated)
    holder_cluster_top_n: int = 4               # how many top non-vault holders to cluster
    holder_cluster_warn: int = 2                # >= this many top holders sharing one funder -> graded scam penalty
    # DUPLICATION (user insight): a name+ticker REUSED across many mints is a scam-factory hallmark (a
    # fresh creator/funder but the SAME branding still ties the spam together). SOFT/advisory + noisy
    # (popular memes are reused legitimately), so a small graded penalty, never a veto; calibrate from data.
    name_reuse_warn: int = 6                    # name+symbol seen on > this many mints -> graded +0.10 scam penalty
    creator_rep_min_tokens: int = 3             # P8: min classified tokens before a creator's REALIZED rug-rate is weighed (below this the rate is too noisy)
    creator_rep_rug_rate: float = 0.5           # P8: a creator whose own classified tokens rugged >= this often -> graded scam penalty (learned from OUR outcomes, not just launch count)
    creator_rep_half_life_s: float = 604800.0   # P10b CAL3: half-life (default 7d) for the ADVISORY recency-weighted creator rug rate fed to the brain (a long-ago rug decays; 0 = no decay). NEVER touches the deterministic count-based penalty.
    liq_drain_warn_pct: float = 20.0            # P8: a pool whose polled liquidity is FALLING by >= this % before we buy is a rug-in-progress -> graded scam penalty (the static liquidity LEVEL doesn't separate winners from rugs; the DIRECTION is a stronger pre-tell)
    creator_sold_pct: float = 1.0               # P8: a creator wallet holding < this % of supply has DUMPED its allocation (user's #1 setup tell) -> graded scam penalty. Advisory, not a veto (the brain can still enter if holders stay + the trendline holds).
    sniper_share_warn: float = 0.6              # P8 (G3 tape): first-N wallets owning >= this share of buy volume = sniper-dominated (they dump on the crowd) -> graded scam penalty
    creator_dump_warn: float = 0.3             # P8 (G3 tape): creator has SOLD >= this fraction of what it bought, on the live tape (real-time dev dump) -> graded scam penalty
    bundle_share_warn: float = 0.5             # P8 (G3 tape, research #1): >= this share of buy volume from coordinated same-block BUNDLES = concealed concentration -> graded scam penalty
    conc_rise_cut_pct: float = 8.0              # W6: cut a HELD position when its top-5 concentration RISES >= this many percentage-points above ENTRY (dev/insiders consolidating = distribution prep = rug imminent). The TREND, not the static level (static didn't separate). 0 disables.
    max_single_wallet_pct: float = 35.0        # excludes LP
    live_dev_dump_pct: float = 20.0            # creator selling >= this now -> abort


@dataclass(frozen=True)
class MomentumThresholds:
    """Healthy / buy-side thresholds for the weighted momentum score."""

    volume_spike_mult: float = 2.5          # current vol > N x trailing avg
    min_vol_to_mcap_pct: float = 10.0       # < this == dead
    max_vol_to_mcap_pct: float = 0.0        # WL12 band UPPER (0 = off). Velocity/volume-intensity sweet-spot: our data shows vol/mcap 15-40% wins 35% (vs 26%), while >40% is wash/dump territory (40-100% wins 25% / rugs 50%). The pro "healthy volume" band. Off by default (small upper-cohort n); set e.g. 50 to skip the manipulation zone.
    min_buy_sell_ratio: float = 1.0        # WL1 (winner_loss): was 1.5 — that hard gate lost 17 winners / dodged 16 rugs (48% precision = noise; buy/sell is non-predictive, AUC~0.5). 1.0 vetoes only net-SELLING (buys<sells) flow, keeps the [1.0,1.5) winners. Advisory-calibrated, not auto-tuned.
    min_liq_to_mcap_pct: float = 10.0
    min_vol_h1: float = 1500.0              # R2-a: 1h USD volume — the #1 winner signal (study + AI5); _norm full_at = 2x this
    min_unique_buyers: int = 20             # WL11 (band analysis, user's "ranges" insight): 15->20 — our own data shows the <20-holder cohort wins 19% / rugs 36% (n=94) vs the 20-40 band's 28% / 17%. breadth defeats wash bots + the <20 cohort is the dead/rug zone.
    max_unique_buyers: int = 0              # WL11 band UPPER bound (0 = off). Data hints >150 holders = late/dump (win 0%, n=7) but n is too small to default-on; configurable for the user's banded funnel.


@dataclass(frozen=True)
class RiskLimits:
    # DEFENSIVE profile (multi-agent designed, sim-grounded): for memecoins, capital
    # preservation comes from SIZE / EXPOSURE / SELECTIVITY — not tighter stops.
    initial_sol: float = 10.0
    max_positions: int = 5                  # W2: 3->5 concurrent HOLDs for volume (paired w/ exposure 0.20; per-trade rug still 5%-capped). Was 3.
    max_position_sol: float = 1.0           # ABSOLUTE per-trade ceiling = 10% of equity (was 2.0)
    daily_loss_cap_sol: float = 1.5         # circuit breaker ~15% of equity (was 2.0)
    per_token_cooldown_s: float = 300.0     # after a loss on a token
    min_liquidity_usd: float = 3_000.0
    min_market_cap_usd: float = 10_000.0    # WL10 (Axiom/DexScreener pro-trader filter, data-confirmed): skip sub-$10K launches — our own classified data shows the <$10K cohort wins 14% vs 26% (the "dead-on-arrival" rugs). 0 disables. (Only ~7% of our traded set is sub-$10K, so it's a mild, safe funnel tightening, not a regime change.)
    max_market_cap_usd: float = 0.0         # WL11 band UPPER bound (0 = off). The data's safest zone is mcap $70-150K (rug 9% vs 31%); the pro band is $50K-$1M. Off by default (capping risks overfitting our small high-mcap sample) but configurable for the user's banded funnel — e.g. set 1_000_000 to skip already-mooned late entries.
    max_modeled_slippage_pct: float = 15.0  # skip fill if impact worse
    risk_per_trade_frac: float = 0.04       # equity fraction per trade at full size -> compounds (was 0.05)
    max_position_equity_frac: float = 0.05  # D5 HARD per-trade ceiling: one position's SOL (= its FULL rug loss) can never exceed this fraction of equity, independent of size_pct/risk_per_trade_frac. A robust survival invariant set just ABOVE risk_per_trade_frac (a backstop, not a normal-path constraint) — guarantees the bound even if sizing assumptions change or equity is drawn down.
    max_liquidity_frac: float = 0.02        # cap a buy at this fraction of pool liquidity (slippage)
    max_total_exposure_frac: float = 0.20   # W2: 0.15->0.20 — the TRUE concurrency governor (funds ~5 full-size HOLDs); whole-book rug 20% of equity, per-trade still 5%-capped. daily_loss_cap KEPT at 1.5 (survival-first: don't raise loss tolerance while the anti-bleed fixes prove out). Was 0.15.
    max_consecutive_losses: int = 5         # halt new entries after N losses in a row (0 = off); clears on a win or day rollover
    # N3 ROLLING-PnL HALT: the consecutive-loss breaker above RESETS on any non-negative close, so an
    # alternating {small win, big loss} bleed (the NORMAL shape at ~24% win-rate) never trips it. This
    # breaker instead sums the realized PnL of the last `rolling_loss_window` CLOSES and halts new
    # entries when that sum is a loss this deep — robust to alternation. Halts ENTRIES only; clears on
    # a UTC-day rollover (never on a single win — that is the whole point). Calibrate from trade_outcomes.
    max_rolling_loss_sol: float = 1.0       # halt when the last-N realized PnLs net to a loss >= this (0 = off)
    rolling_loss_window: int = 8            # how many recent closes the rolling sum spans
    # N8 DARK-CONCENTRATION SIZING: top-5 holder concentration is the #1 free rug tell, but it is DARK
    # on the public Solana RPC (getTokenLargestAccounts unsupported). Trading blind at full size is
    # exactly when to shrink — size/selectivity is the survival lever. When concentration is dark this
    # multiplies the per-trade size (and is surfaced in the startup risk banner). 1.0 = no shrink.
    conc_dark_size_mult: float = 0.5        # per-trade size multiplier while holder concentration is dark
    # N9 CORRELATED-COHORT CAP: position_size/exposure treats every mint as INDEPENDENT, but RESEARCH
    # says catastrophic losses are correlated flushes — N concurrent positions from ONE creator are
    # really ONE bet. Cap concurrent OPEN positions sharing a creator wallet (the key is ~100% captured).
    max_creator_cohort: int = 2             # max concurrent open positions from the SAME creator (0 = off)
    # N10 GIVEBACK halt (REALIZED): daily_loss_cap measures only START-of-day equity, so a day that
    # banks gains then gives them back via later losing closes stays silent (the worst shape — losing a
    # winning day). Tracks cumulative REALIZED day-PnL + its intraday peak; halts new entries when this
    # FRACTION of the peak is given back, but only after the peak cleared `giveback_arm_frac` of INITIAL
    # equity (so a tiny gain can't strangle the thin flow). REALIZED basis -> a single open position's
    # unrealized pump-and-fade can't arm/trip it. Complements N3 (per-close window) + daily_loss_cap.
    # LOOSE prior — calibrate from the logged trade book before tightening. 0 = off.
    daily_giveback_frac: float = 0.7        # halt after giving back this fraction of the day's PEAK realized PnL
    giveback_arm_frac: float = 0.05         # ...only once that peak exceeded this fraction of INITIAL equity
    max_fill_return_mult: float = 20.0      # fill-sanity guard: clamp a single round-trip's proceeds to this multiple of its cost (a >20x paper fill is a pricing glitch — curve-buy vs DexScreener-sell SOL-scale mismatch — not alpha; left unclamped one glitch can be ~100% of reported PnL + poison the go-live gate). 0 disables.


@dataclass(frozen=True)
class Fees:
    """pump.fun reality: fees stack. Undermodeling makes paper PnL look fake-good."""

    pumpportal_pct: float = 0.005           # Local Tx API
    pumpfun_curve_pct: float = 0.0125       # on-curve trade fee
    priority_fee_sol: float = 0.0005        # per tx network/priority estimate
    migration_fee_sol: float = 0.015        # one-time at graduation
    base_slippage_pct: float = 0.03         # thin liquidity default


@dataclass(frozen=True)
class Settings:
    mode: str = "paper"                     # paper | live
    log_level: str = "INFO"

    # loop cadence / candidate selection
    entry_threshold: float = 0.50           # on the post-D6 rescaled 0..1 scale. Live re-score: ~375 of 4.5k mints clear 0.50 (selective but active). Defense is in SIZE/exposure, not a starved entry.
    min_expected_return: float = 0.03       # require cost-aware expected return >= this (thin honest margin; estimator uncalibrated)
    use_kelly_sizing: bool = False          # W6: size by fractional Kelly (edge-proportional) instead of fixed risk_per_trade_frac. OFF until P4 supplies a CALIBRATED win-prob — Kelly on a miscalibrated p amplifies losses. The skeleton is ready; flip on only with a proven edge.
    kelly_fraction_mult: float = 0.5        # half-Kelly (safety): bet kelly_fraction_mult * f* of the budget
    require_scalp_momentum: bool = True      # AI8: a SCALP buy needs a real momentum/trend signal (else it just bleeds fees)
    trade_scalp: bool = False                # W2: SCALP buy lane OFF by default — full-history reconstruction shows SCALP is 0/39 wins, net ~-0.7..-0.9 SOL (round-trip 9.5% never cleared). Scalp candidates are still SCORED + OBSERVED (P4 data kept); only the BUY is vetoed. Set TRADE_SCALP=true to re-enable.
    require_data_backed_setup: bool = True   # AI9: only trade tokens with REAL market data (DexScreener liquidity>0) — skip blind curve-only bets; survive via good setups, not early guesses
    # P1 GOOD-SETUP classifier (survival-first): refuse falling-knife / ghost-pool late entries. Heuristics — re-derive from the live distribution.
    require_good_setup: bool = True          # skip a 'bad'-graded setup regardless of score
    setup_min_liquidity_usd: float = 8000.0  # sub-8k indexed pools are the bleed cohort
    setup_min_liq_mcap_ratio: float = 0.03   # liq/mcap < 3% = uninvestable ghost
    setup_max_off_high_pct: float = 35.0     # already this far off the recent high = catching a knife
    setup_max_h1_pump_for_fade: float = 150.0  # +150% h1 with m5<=0 => post-pump fade
    setup_min_buyers_good: int = 20
    setup_min_buyers_marginal: int = 12
    setup_bsr_lo: float = 1.2
    setup_bsr_hi: float = 4.0                # above this with <5 buyers = wash spike
    setup_min_sup_dist_pct: float = 3.0
    setup_risky_scam: float = 0.3            # scam-likelihood at/above this (but below the 0.5 hard veto) -> the 'risky' setup TYPE (handle with caution / smaller / faster) even if not vetoed
    setup_risky_size_mult: float = 0.5       # P10b SO1/SO2: DETERMINISTIC size multiplier for a 'risky' setup_type (rule + LLM path); only ever shrinks, never relaxes a gate
    # P10b SO4: rule-scorer ranking penalties (orchestration) — rank DOWN the off-high / falling survivors the setup gate already distrusts. Multiplicative, applied before the affine rescale; only ever reduce the score.
    rule_offhigh_penalty_at: float = 20.0    # off_high_pct beyond which the penalty RAMPS up toward the setup gate's veto (setup_max_off_high_pct=35); must be < that gate or the ramp is degenerate
    rule_offhigh_penalty_max: float = 0.25   # max fractional score cut as off_high deepens (0..1)
    rule_downtrend_penalty: float = 0.15     # fractional score cut when the polled trend is 'falling' (0..1)

    # R13 dynamic holder-driven position re-evaluation (open positions)
    reeval_interval_s: float = 60.0
    holder_cut_pct: float = 96.0            # W4: R13 cuts a held position only at EXTREME concentration (was 85 = below the ~89 memecoin median -> would dump most winners)
    holder_good_pct: float = 85.0           # W4: <= this = healthier end of the memecoin range -> extend (was 40 = unreachable, nothing is that low post-graduation)
    eval_interval_s: float = 3.0            # how often to scan + score candidates
    manage_interval_s: float = 2.0          # how often to check exits
    report_interval_s: float = 30.0         # equity/stats heartbeat
    feed_stale_warn_s: float = 120.0        # P8: warn + alert if the discovery WS goes silent this long (a dead feed, since pump.fun launches are constant — not a lull). 0 disables.
    watch_max_age_s: float = 2700.0         # W2: 1800->2700 — keep freshly-graduated tokens in scope long enough to catch a liquidity>0 snapshot + cross the 5m label horizon
    watch_limit: int = 240                  # W2: 120->240 — widen indexed-mint INTAKE (the true volume + P4-data ceiling; supply-bound, not gate-bound)
    max_safety_checks_per_cycle: int = 20   # W2: 10->20 — resolve more market-gate survivors' Helius safety same-cycle (survivors are few on free tier, so rate-limit risk is low)
    max_price_misses: int = 20              # W2: consecutive no-price polls before a force-close "no_price" (~40s at manage_interval 2s; faster blackout cut for rugs). Was a hardcoded 60.

    # autonomous brain (Claude). Exact model IDs — no date suffix.
    haiku_model: str = "claude-haiku-4-5"   # fast default decision model
    sonnet_model: str = "claude-sonnet-4-6" # escalation / reflection model
    high_conf_threshold: float = 0.8        # >= this = slam-dunk, act locally (no LLM)
    llm_per_min: int = 5                     # max LLM decision calls per minute
    llm_max_tokens: int = 512                # a verdict, not prose
    reflect_enabled: bool = True             # learn lessons after each closed trade
    meta_reflect_interval_s: float = 1800.0  # AI3: distill aggregate performance into a strategy lesson (0 = off)
    exit_use_trend: bool = True              # P3: exit a (non-losing) position when the live trend BREAKS (fade), don't wait for the stop
    fade_min_polls: int = 5                   # P8: a fade needs >= this many polled-trend points (was a hardcoded 3 ~= 6s of noise -> fade was 0/14, all losers)
    fade_min_drop_pct: float = 10.0           # P8: ...AND a falling trend of at least this magnitude (else it's jitter, not a break)

    # P8 MISS-LEARNING (regret loop): replay observed-but-not-bought mints, re-price, and LEARN from
    # the ones that subsequently pumped ("why did we skip this?"). OFF the hot path; self-degrades
    # without DexScreener (no-op pass) or without an LLM (record-only, no lesson).
    miss_learn_enabled: bool = True          # master switch for the regret loop
    miss_learn_interval_s: float = 600.0     # how often to replay (10 min; well off the hot path). 0 disables.
    miss_win_threshold_pct: float = 50.0     # forward return >= this (+50%) = a "missed winner"
    miss_min_age_s: float = 300.0            # an obs must be >= this old to judge (matches the 5m label horizon)
    miss_max_age_s: float = 3600.0           # ...and <= this old (don't chase fetches on stale/dead tokens)
    miss_batch_size: int = 30                # observations re-priced per pass (<=30 = ONE DexScreener request)
    miss_reflect_enabled: bool = True        # queue a brain miss-reflection (needs llm + reflect_enabled too)
    miss_reflect_max_per_pass: int = 3       # cap reflections/pass (LLM budget + lesson-spam guard)
    miss_recall_frac: float = 0.34           # P10b: max FRACTION of recall slots 'miss'-origin regret lessons may fill (survival lessons keep the rest)

    # N1 FORWARD-TRACKING (label completion): re-price observed-but-not-bought INDEXED mints from the
    # DB — independent of registry membership — so their forward price path reaches the label horizon.
    # _evaluate_once logs a mint only while it's in the active top-N; under a flood of new tokens an
    # indexed mint is pushed out (median track span ~511s) BEFORE the 5m horizon, so its label never
    # forms. This off-hot-path loop keeps re-pricing it until track_window_s, completing the label.
    # The binding constraint on GBM/separation/AUC is labelable-mint COUNT — this is what lifts it.
    track_enabled: bool = True               # master switch for the forward-tracking loop. 0/false disables.
    track_interval_s: float = 60.0           # how often to re-price the tracking set (well off the hot path)
    track_window_s: float = 1200.0           # keep re-pricing a mint until its FIRST obs is this old (20 min
                                             # — covers the 5m label horizon + the 900s tol with margin)
    track_revisit_s: float = 45.0            # only re-price a mint whose LATEST obs is at least this old (so a
                                             # still-active mint, logged every cycle by eval, is NOT double-logged)
    track_batch_size: int = 30               # mints re-priced per pass (<=30 = ONE DexScreener request)

    # N12 FUNDER CLUSTERING: resolve each observed creator's funding wallet (Helius getSignaturesForAddress
    # + getTransaction), off the hot path + cached/persisted (a wallet's funder is immutable), to re-link
    # rotated creators by common treasury. Self-degrades to a no-op without an RPC. 0/false disables.
    funder_clustering_enabled: bool = True
    funder_interval_s: float = 120.0         # how often to resolve a batch of unresolved creators
    funder_batch_size: int = 10              # creators resolved per pass (2 RPC calls each; keep light)

    # READINESS MONITOR: periodically snapshot the go-live GATE metrics (CLEAN-book net/pf/win/n +
    # labelable count) to a durable `readiness_log` table, so the TRAJECTORY is queryable ("is the book
    # trending toward net-positive over days?"). Cheap (no GBM), off the hot path. 0 disables.
    readiness_interval_s: float = 3600.0     # snapshot the readiness gate this often (hourly)

    # P8 SELF-CALIBRATION (advisory): periodically classify our own observations, derive threshold
    # SUGGESTIONS from what actually separates winners from rugs, + learn per-creator reputation. The
    # suggestions are LOGGED + stored as a recalled lesson; they are NEVER auto-applied to live risk.
    calibrate_enabled: bool = True
    calibrate_interval_s: float = 3600.0     # how often to self-calibrate (1h; fully off the hot path). 0 disables.
    calibrate_min_mints: int = 40            # don't calibrate on fewer classified mints than this (too noisy)
    calibrate_horizon_s: float = 300.0       # forward horizon for the outcome classifier (5m)
    calibrate_tol_s: float = 900.0           # tolerance window around the horizon

    # LLM backend: auto (local Ollama -> cloud Claude -> rule), local, cloud, off
    llm_backend: str = "auto"
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "gemma3:4b"          # FAST screener (every decide) — qwen3:8b = smarter/slower
    # P-dual: a SECOND, deeper local model used ONLY to confirm low-conviction buys (mirrors the cloud
    # Haiku->Sonnet cascade with two local models). "" => single-model local brain (no escalation).
    ollama_confirm_model: str = "qwen3:8b"   # deeper local confirm model; must differ from ollama_model
    llm_parallel: int = 3                    # bounded concurrency for brain.decide() per eval cycle (1 = serial, today's behavior). Speeds up catching fresh coins by removing head-of-line blocking; set to your real Ollama OLLAMA_NUM_PARALLEL.
    llm_timeout_s: float = 60.0              # local models are seconds/call

    # WL6 — IMAGE scam-scorer (the user's visual edge: spot a scam by its IMAGE). A vision model scores a
    # GATE-PASSED candidate's image 0..1; LOG-ONLY (rides entry_features as `image_scam_score`, no veto).
    # OFF by default: needs a pulled vision model (`ollama pull llava`) or a cloud vision endpoint. Cost-
    # gated to candidates we're about to buy (a few/min), so even a paid backend stays cheap.
    image_scam_enabled: bool = False
    image_scam_host: str = ""                # "" => reuse ollama_host; or a cloud vision base URL
    image_scam_model: str = "llava"          # Ollama vision model (llava / llama3.2-vision / bakllava)
    image_scam_timeout_s: float = 30.0       # a vision call is slow; keep it off the critical path via caching
    image_ipfs_gateway: str = "https://ipfs.io/ipfs/"   # resolve ipfs:// metadata/image refs
    image_scam_max_bytes: int = 5_000_000    # skip absurdly large images (defensive)

    # WL9 — BUYER INTELLIGENCE (the #1 trader winner-signal: smart-money confluence) on FREE data. For the
    # top-N ranked candidates each cycle, reconstruct recent BUYER wallets from the Helius RPC (no SOL, vs
    # the metered PumpPortal tape), learn each wallet's reputation from our forward outcomes, and log the
    # smart-vs-dumper early-buyer counts. OFF by default + EXPENSIVE (~1+max_sigs RPC calls/token), so the
    # top-N is small; reputations accrue SLOWLY on free data (the honest free-vs-metered trade-off). LOG-
    # first: the confluence counts are logged for validation, never a veto until they are shown to separate.
    buyer_intel_enabled: bool = False
    buyer_intel_top_n: int = 1                # how many top-ranked candidates/cycle get the (costly) buyer read
    buyer_intel_max_sigs: int = 20            # recent signatures scanned per token (each => 1 getTransaction)
    buyer_intel_min_tokens: int = 3           # a wallet needs this many resolved tokens before it earns a label
    buyer_intel_smart_winrate: float = 0.55   # >= this win-rate over resolved tokens = SMART money
    buyer_intel_dumper_rugrate: float = 0.6   # >= this rug-rate = DUMPER (exit-liquidity magnet)

    # Phase 2 — GBM scorer (rule scorer is used until a model is trained)
    gbm_model_path: str = "gbm_model.txt"
    gbm_entry_threshold: float = 0.40       # GBM prob scale. Val sweep: 0.40 => precision 0.40 (2.1x the 0.19 base), 0.5 starves (4/81). Re-tune as data grows.
    # The 5m-forward target is rare-positive (~18%), so without a class-prior correction the GBM's
    # probabilities cluster near the base rate and it predicts ~ZERO positives at gbm_entry_threshold
    # (an AUC that ranks fine but offers no usable entry point — exactly the P4 deploy-gate failure).
    # scale_pos_weight = n_neg/n_pos shifts the minority class up. A class-prior fix, NOT data-snooping
    # (correct regardless of sample size); the precision-over-base deploy gate still guards what ships.
    gbm_balance_classes: bool = True        # apply scale_pos_weight = n_neg/n_pos when training the GBM
    # GO-LIVE GATE (advisory — `python -m memebot.readiness`): the explicit, auditable criteria that must
    # ALL hold on the HONEST (CLEAN, glitch-excluded) realized book before real money is even considered.
    # A single lucky AUC can't satisfy this; it's the durable-PnL gate. NEVER auto-flips live mode.
    go_live_min_trades: int = 100           # min closed round-trips for the realized stats to be meaningful
    go_live_min_profit_factor: float = 1.3  # gross-win / gross-loss must clear this (a real margin over break-even)
    # P4 SHADOW MODE: a candidate model that SCORES + LOGS on live data but NEVER sizes a trade.
    # Empty = off (default). When set, the bot loads this model alongside the live (rule) scorer and
    # records its probability into each observation's `shadow_score` feature — so a freshly-trained
    # GBM can be validated against real forward outcomes BEFORE it is promoted to drive entries.
    gbm_shadow_model_path: str = ""
    # rule-scorer calibration: the raw rule sum is a heuristic rank, not a probability.
    # score_candidate affine-rescales [ref_lo, ref_hi] -> [0,1]. Measured raw range (social-free
    # scorer, 49k snapshots) is ~[0, 0.86] and bimodal — a mass at ~0.42-0.51 then a thin tail to
    # 0.86 — so 0.62 maps the top ~0.5% to 1.0 (the rare slam-dunk). Re-derive from a rolling live
    # distribution as the market shifts (E3); do NOT freeze these. (True ranking power needs AI6/GBM.)
    rule_score_ref_lo: float = 0.30
    rule_score_ref_hi: float = 0.70         # re-derived after R2-a volume reweight (129k candidates): entry 0.50 = top ~12%, slam 0.85 stays rare (~53 mints)
    rule_high_conf_threshold: float = 0.85  # rule-scale slam-dunk (act-local) tier; high on purpose so it stays rare (no proven edge yet)

    # Parquet archival (skipped if pyarrow missing)
    archive_dir: str = "archive"
    archive_interval_s: float = 1800.0      # 0 = disabled

    # Metered PumpPortal trade stream (tick features). OFF by default — it SPENDS SOL.
    trade_stream_enabled: bool = False      # also requires PUMPPORTAL_API_KEY
    watchlist_size: int = 20
    watchlist_interval_s: float = 15.0

    # endpoints / keys
    pumpportal_ws_url: str = "wss://pumpportal.fun/api/data"
    pumpportal_api_key: str = ""
    helius_rpc_url: str = ""
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    dexscreener_base_url: str = "https://api.dexscreener.com"
    # A2: Jupiter quote-based honeypot check (QUOTE-only, no real money, no key). Runs right before a
    # paper buy: buy-quote -> sell-quote; no sell route or extreme round-trip tax -> veto. Degrades to
    # a no-op when Jupiter is unreachable, so it never blocks a buy on its own failure.
    honeypot_check_enabled: bool = True
    jupiter_base_url: str = "https://quote-api.jup.ag"
    honeypot_sol_amount: float = 0.05            # the quote-test buy size (SOL); only a routing probe
    honeypot_min_roundtrip_keep: float = 0.5     # veto if an instant buy->sell recovers < this fraction of SOL in (stealth tax)
    # B1: RugCheck.xyz cross-check (free, no key). A last-line pre-buy SECOND OPINION; veto on a RugCheck
    # 'danger' verdict. Read-only; degrades to a no-op when unreachable (an external service must never
    # silently block buys on its own failure).
    rugcheck_enabled: bool = True
    rugcheck_base_url: str = "https://api.rugcheck.xyz"
    rugcheck_veto_on_danger: bool = True         # treat a RugCheck 'danger'-level risk as a buy veto
    # WL14 — Solana Tracker risk/holder cross-check: the Axiom-style FILTER DATA (bundle/sniper/insider %,
    # top-10 concentration, a 1-10 risk score, a rugged flag) we cannot compute on free on-chain data.
    # Free tier ~2,500 req/mo, so cost-gated to buy candidates + cached. OFF without a key.
    solanatracker_enabled: bool = False
    solanatracker_api_key: str = ""              # free x-api-key from solanatracker.io (NEVER commit it)
    solanatracker_base_url: str = "https://data.solanatracker.io"
    # `rugged` ALWAYS vetoes (clean/rare). danger is too broad (Top-10/Bundlers fire on ~80% of tokens) ->
    # default OFF, log-first; calibrate the score/top10 cuts from our outcomes before gating on them.
    solanatracker_veto_on_danger: bool = False   # also veto on any danger-level risk (noisy — calibrate first)
    solanatracker_max_risk_score: float = 0.0    # also veto if risk score (1-10) >= this (0 = off; e.g. 8)
    bitquery_api_key: str = ""
    moralis_api_key: str = ""
    anthropic_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    db_path: str = "memebot.db"

    safety: SafetyGates = field(default_factory=SafetyGates)
    momentum: MomentumThresholds = field(default_factory=MomentumThresholds)
    risk: RiskLimits = field(default_factory=RiskLimits)
    fees: Fees = field(default_factory=Fees)
    exit: ExitParams = field(default_factory=ExitParams)   # per-mode TP/SL/breakeven/trail (env-overridable)

    @property
    def is_live(self) -> bool:
        return self.mode.strip().lower() == "live"

    @property
    def helius_rpc(self) -> str:
        """Prefer Helius (authority/holder reads), fall back to public RPC."""
        return self.helius_rpc_url or self.solana_rpc_url

    def validate(self) -> list[str]:
        """Non-fatal sanity warnings (empty list == all good). Logged at startup."""
        w: list[str] = []
        if not 0.0 <= self.entry_threshold <= 1.0:
            w.append(f"entry_threshold {self.entry_threshold} is outside [0,1]")
        if self.high_conf_threshold <= self.entry_threshold:
            w.append(f"high_conf_threshold ({self.high_conf_threshold}) <= entry_threshold "
                     f"({self.entry_threshold}) — the LLM brain will never be consulted")
        if self.risk.initial_sol <= 0:
            w.append("initial_sol must be > 0")
        if self.risk.max_positions <= 0:
            w.append("max_positions must be > 0")
        if not 0.0 <= self.miss_recall_frac <= 1.0:
            w.append(f"miss_recall_frac ({self.miss_recall_frac}) is outside [0,1] (clamped at runtime)")
        if not 0.0 < self.setup_risky_size_mult <= 1.0:
            w.append(f"setup_risky_size_mult ({self.setup_risky_size_mult}) must be in (0,1]")
        if not 0.0 <= self.rule_offhigh_penalty_max <= 1.0:
            w.append(f"rule_offhigh_penalty_max ({self.rule_offhigh_penalty_max}) must be in [0,1]")
        if not 0.0 <= self.rule_downtrend_penalty <= 1.0:
            w.append(f"rule_downtrend_penalty ({self.rule_downtrend_penalty}) must be in [0,1]")
        if self.rule_offhigh_penalty_at >= self.setup_max_off_high_pct:
            w.append(f"rule_offhigh_penalty_at ({self.rule_offhigh_penalty_at}) >= setup_max_off_high_pct "
                     f"({self.setup_max_off_high_pct}) -> the off-high scorer penalty ramp is degenerate")
        if self.risk.max_position_sol <= 0:
            w.append("max_position_sol must be > 0")
        if self.risk.risk_per_trade_frac <= 0:
            w.append("risk_per_trade_frac must be > 0")
        if not 0.0 < self.risk.max_position_equity_frac <= 1.0:
            w.append(f"max_position_equity_frac {self.risk.max_position_equity_frac} should be in (0,1]")
        if self.risk.risk_per_trade_frac > self.risk.max_position_equity_frac:
            w.append(f"risk_per_trade_frac {self.risk.risk_per_trade_frac} > max_position_equity_frac "
                     f"{self.risk.max_position_equity_frac}: the hard ceiling would clip normal sizing "
                     "(it should sit ABOVE the per-trade frac as a backstop)")
        if not 0.0 < self.risk.max_total_exposure_frac <= 1.0:
            w.append(f"max_total_exposure_frac {self.risk.max_total_exposure_frac} should be in (0,1]")
        if self.risk.max_rolling_loss_sol > 0 and self.risk.rolling_loss_window < 1:
            w.append("rolling_loss_window must be >= 1 when max_rolling_loss_sol > 0 "
                     "(the N3 rolling-PnL bleed halt needs a window to sum over)")
        if not 0.0 < self.risk.conc_dark_size_mult <= 1.0:
            w.append(f"conc_dark_size_mult {self.risk.conc_dark_size_mult} should be in (0,1] "
                     "(it SHRINKS size when concentration is dark; >1 would grow risk on a blind session)")
        if self.risk.max_creator_cohort < 0:
            w.append("max_creator_cohort must be >= 0 (0 disables the same-creator cohort cap)")
        if not 0.0 <= self.risk.daily_giveback_frac <= 1.0:
            w.append(f"daily_giveback_frac {self.risk.daily_giveback_frac} should be in [0,1] "
                     "(fraction of the day's peak REALIZED PnL that, once given back, halts entries; 0 = off)")
        if self.risk.daily_giveback_frac > 0 and self.risk.giveback_arm_frac < 0:
            w.append("giveback_arm_frac must be >= 0 (the min peak realized PnL, as a fraction of INITIAL "
                     "equity, before the N10 giveback halt can arm)")
        if self.exit.breakeven_arm_pct <= self.exit.breakeven_floor_pct:
            w.append("breakeven_arm_pct should be > breakeven_floor_pct (else breakeven exits at a loss)")
        _rt = 2.0 * (self.fees.pumpportal_pct + self.fees.pumpfun_curve_pct + self.fees.base_slippage_pct)
        if self.exit.partial_tp_pct <= _rt:
            w.append(f"partial_tp_pct {self.exit.partial_tp_pct} <= round-trip cost {_rt:.3f} — the partial leg loses money")
        if self.exit.trail_after_arm_pct >= self.exit.breakeven_arm_pct:
            w.append("trail_after_arm_pct should be < breakeven_arm_pct")
        if not 0.0 < self.exit.liq_collapse_frac < 1.0:
            w.append(f"liq_collapse_frac {self.exit.liq_collapse_frac} should be in (0,1) — fraction of entry liquidity that triggers a rug exit")
        if not 0.0 < self.exit.derisk_max_frac < 1.0:
            w.append(f"derisk_max_frac {self.exit.derisk_max_frac} should be in (0,1) — a free-roll tail must always remain")
        if self.exit.derisk_tp_pct <= 0.0:
            w.append("derisk_tp_pct should be > 0 (the profit at which a risky position recovers principal)")
        if self.exit.sell_pressure_bsr < 0.0:
            w.append(f"sell_pressure_bsr {self.exit.sell_pressure_bsr} should be >= 0 (buy/sell ratio floor; 0 disables)")
        if self.max_price_misses <= 0:
            w.append("max_price_misses must be > 0")
        if self.rule_score_ref_hi <= self.rule_score_ref_lo:
            w.append(f"rule_score_ref_hi ({self.rule_score_ref_hi}) must be > rule_score_ref_lo ({self.rule_score_ref_lo})")
        if self.rule_high_conf_threshold >= 1.0:
            w.append("rule_high_conf_threshold >= 1.0 is unreachable — the rule slam-dunk tier is dead")
        if min(self.eval_interval_s, self.manage_interval_s, self.report_interval_s) <= 0:
            w.append("eval/manage/report intervals must be > 0")
        if not 0.0 <= self.fees.base_slippage_pct <= 1.0:
            w.append(f"base_slippage_pct {self.fees.base_slippage_pct} should be in [0,1]")
        if self.trade_stream_enabled and not self.pumpportal_api_key:
            w.append("TRADE_STREAM_ENABLED is set but PUMPPORTAL_API_KEY is empty — trade stream stays off")
        if self.llm_parallel < 1:
            w.append(f"llm_parallel {self.llm_parallel} must be >= 1 (1 = serial decisions)")
        if self.miss_learn_enabled:
            if self.miss_min_age_s >= self.miss_max_age_s:
                w.append(f"miss_min_age_s ({self.miss_min_age_s}) must be < miss_max_age_s "
                         f"({self.miss_max_age_s}) — the replay window is empty")
            if self.miss_win_threshold_pct <= 0:
                w.append("miss_win_threshold_pct must be > 0 (the forward-return bar for a 'missed winner')")
            if self.miss_batch_size < 1:
                w.append("miss_batch_size must be >= 1 (0 silently disables the regret loop; "
                         "negative becomes SQLite LIMIT -1 = unbounded, breaking the 1-request batch)")
            if self.miss_batch_size > 30:
                w.append(f"miss_batch_size {self.miss_batch_size} > 30 — DexScreener batches 30 mints/call; "
                         "a larger batch costs extra requests per pass")
        if self.track_enabled:
            if self.track_window_s <= self.track_revisit_s:
                w.append(f"track_window_s ({self.track_window_s}) must be > track_revisit_s "
                         f"({self.track_revisit_s}) — else the tracking set is always empty")
            if self.track_batch_size < 1:
                w.append("track_batch_size must be >= 1 (0/negative breaks the bounded 1-request batch)")
            if self.track_batch_size > 30:
                w.append(f"track_batch_size {self.track_batch_size} > 30 — DexScreener batches 30 mints/call; "
                         "a larger batch costs extra requests per pass")
        if self.funder_clustering_enabled:
            if self.safety.funder_serial_min < 2:
                w.append("funder_serial_min should be >= 2 (a 1-creator funder is not a cluster)")
            if self.safety.funder_serial_min > self.safety.funder_serial_max:
                w.append(f"funder_serial_min ({self.safety.funder_serial_min}) must be <= funder_serial_max "
                         f"({self.safety.funder_serial_max}) — else the serial-operator band is empty")
            if self.funder_batch_size < 1:
                w.append("funder_batch_size must be >= 1 (0 disables resolution via an empty batch; NEGATIVE "
                         "would become SQLite LIMIT -1 = UNBOUNDED — now clamped in unresolved_creators)")
        if self.ollama_confirm_model and self.ollama_confirm_model == self.ollama_model:
            w.append("ollama_confirm_model == ollama_model — local escalation is a no-op; set "
                     "OLLAMA_CONFIRM_MODEL to a deeper model (e.g. qwen3:8b) or leave it empty")
        if self.is_live:
            w.append("MODE=live is not implemented (Phase 5) — paper only")
        return w

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            mode=_str("MODE", "paper"),
            log_level=_str("LOG_LEVEL", "INFO"),
            entry_threshold=_float("ENTRY_THRESHOLD", 0.50),  # keep in sync with the dataclass default above
            min_expected_return=_float("MIN_EXPECTED_RETURN", 0.03),
            use_kelly_sizing=_bool("USE_KELLY_SIZING", False),
            kelly_fraction_mult=_float("KELLY_FRACTION_MULT", 0.5),
            require_scalp_momentum=_bool("REQUIRE_SCALP_MOMENTUM", True),
            trade_scalp=_bool("TRADE_SCALP", False),
            require_data_backed_setup=_bool("REQUIRE_DATA_BACKED_SETUP", True),
            require_good_setup=_bool("REQUIRE_GOOD_SETUP", True),
            setup_min_liquidity_usd=_float("SETUP_MIN_LIQUIDITY_USD", 8000.0),
            setup_min_liq_mcap_ratio=_float("SETUP_MIN_LIQ_MCAP_RATIO", 0.03),
            setup_max_off_high_pct=_float("SETUP_MAX_OFF_HIGH_PCT", 35.0),
            setup_max_h1_pump_for_fade=_float("SETUP_MAX_H1_PUMP_FOR_FADE", 150.0),
            setup_min_buyers_good=_int("SETUP_MIN_BUYERS_GOOD", 20),
            setup_min_buyers_marginal=_int("SETUP_MIN_BUYERS_MARGINAL", 12),
            setup_bsr_lo=_float("SETUP_BSR_LO", 1.2),
            setup_bsr_hi=_float("SETUP_BSR_HI", 4.0),
            setup_risky_scam=_float("SETUP_RISKY_SCAM", 0.3),
            setup_risky_size_mult=_float("SETUP_RISKY_SIZE_MULT", 0.5),
            rule_offhigh_penalty_at=_float("RULE_OFFHIGH_PENALTY_AT", 20.0),
            rule_offhigh_penalty_max=_float("RULE_OFFHIGH_PENALTY_MAX", 0.25),
            rule_downtrend_penalty=_float("RULE_DOWNTREND_PENALTY", 0.15),
            setup_min_sup_dist_pct=_float("SETUP_MIN_SUP_DIST_PCT", 3.0),
            reeval_interval_s=_float("REEVAL_INTERVAL_S", 60.0),
            holder_cut_pct=_float("HOLDER_CUT_PCT", 96.0),
            holder_good_pct=_float("HOLDER_GOOD_PCT", 85.0),
            eval_interval_s=_float("EVAL_INTERVAL_S", 3.0),
            manage_interval_s=_float("MANAGE_INTERVAL_S", 2.0),
            report_interval_s=_float("REPORT_INTERVAL_S", 30.0),
            feed_stale_warn_s=_float("FEED_STALE_WARN_S", 120.0),
            watch_max_age_s=_float("WATCH_MAX_AGE_S", 2700.0),
            watch_limit=_int("WATCH_LIMIT", 240),
            max_safety_checks_per_cycle=_int("MAX_SAFETY_CHECKS_PER_CYCLE", 20),
            max_price_misses=_int("MAX_PRICE_MISSES", 20),
            haiku_model=_str("HAIKU_MODEL", "claude-haiku-4-5"),
            sonnet_model=_str("SONNET_MODEL", "claude-sonnet-4-6"),
            high_conf_threshold=_float("HIGH_CONF_THRESHOLD", 0.8),
            llm_per_min=_int("LLM_PER_MIN", 5),
            llm_max_tokens=_int("LLM_MAX_TOKENS", 512),
            reflect_enabled=_bool("REFLECT_ENABLED", True),
            meta_reflect_interval_s=_float("META_REFLECT_INTERVAL_S", 1800.0),
            exit_use_trend=_bool("EXIT_USE_TREND", True),
            fade_min_polls=_int("FADE_MIN_POLLS", 5),
            fade_min_drop_pct=_float("FADE_MIN_DROP_PCT", 10.0),
            miss_learn_enabled=_bool("MISS_LEARN_ENABLED", True),
            miss_learn_interval_s=_float("MISS_LEARN_INTERVAL_S", 600.0),
            miss_win_threshold_pct=_float("MISS_WIN_THRESHOLD_PCT", 50.0),
            miss_min_age_s=_float("MISS_MIN_AGE_S", 300.0),
            miss_max_age_s=_float("MISS_MAX_AGE_S", 3600.0),
            miss_batch_size=_int("MISS_BATCH_SIZE", 30),
            miss_reflect_enabled=_bool("MISS_REFLECT_ENABLED", True),
            miss_reflect_max_per_pass=_int("MISS_REFLECT_MAX_PER_PASS", 3),
            miss_recall_frac=_float("MISS_RECALL_FRAC", 0.34),
            track_enabled=_bool("TRACK_ENABLED", True),
            track_interval_s=_float("TRACK_INTERVAL_S", 60.0),
            track_window_s=_float("TRACK_WINDOW_S", 1200.0),
            track_revisit_s=_float("TRACK_REVISIT_S", 45.0),
            track_batch_size=_int("TRACK_BATCH_SIZE", 30),
            funder_clustering_enabled=_bool("FUNDER_CLUSTERING_ENABLED", True),
            funder_interval_s=_float("FUNDER_INTERVAL_S", 120.0),
            funder_batch_size=_int("FUNDER_BATCH_SIZE", 10),
            readiness_interval_s=_float("READINESS_INTERVAL_S", 3600.0),
            calibrate_enabled=_bool("CALIBRATE_ENABLED", True),
            calibrate_interval_s=_float("CALIBRATE_INTERVAL_S", 3600.0),
            calibrate_min_mints=_int("CALIBRATE_MIN_MINTS", 40),
            calibrate_horizon_s=_float("CALIBRATE_HORIZON_S", 300.0),
            calibrate_tol_s=_float("CALIBRATE_TOL_S", 900.0),
            llm_backend=_str("LLM_BACKEND", "auto"),
            ollama_host=_str("OLLAMA_HOST", "http://localhost:11434"),
            ollama_model=_str("OLLAMA_MODEL", "gemma3:4b"),
            ollama_confirm_model=_str("OLLAMA_CONFIRM_MODEL", "qwen3:8b"),
            llm_parallel=_int("LLM_PARALLEL", 3),
            llm_timeout_s=_float("LLM_TIMEOUT_S", 60.0),
            image_scam_enabled=_bool("IMAGE_SCAM_ENABLED", False),     # WL6: vision image scam-scorer (LOG-only)
            image_scam_host=_str("IMAGE_SCAM_HOST", ""),               # "" => reuse OLLAMA_HOST
            image_scam_model=_str("IMAGE_SCAM_MODEL", "llava"),
            image_scam_timeout_s=_float("IMAGE_SCAM_TIMEOUT_S", 30.0),
            image_ipfs_gateway=_str("IMAGE_IPFS_GATEWAY", "https://ipfs.io/ipfs/"),
            image_scam_max_bytes=_int("IMAGE_SCAM_MAX_BYTES", 5_000_000),
            buyer_intel_enabled=_bool("BUYER_INTEL_ENABLED", False),    # WL9: free-data smart-money confluence (LOG-only)
            buyer_intel_top_n=_int("BUYER_INTEL_TOP_N", 1),
            buyer_intel_max_sigs=_int("BUYER_INTEL_MAX_SIGS", 20),
            buyer_intel_min_tokens=_int("BUYER_INTEL_MIN_TOKENS", 3),
            buyer_intel_smart_winrate=_float("BUYER_INTEL_SMART_WINRATE", 0.55),
            buyer_intel_dumper_rugrate=_float("BUYER_INTEL_DUMPER_RUGRATE", 0.6),
            gbm_model_path=_str("GBM_MODEL_PATH", "gbm_model.txt"),
            gbm_entry_threshold=_float("GBM_ENTRY_THRESHOLD", 0.40),
            gbm_balance_classes=_bool("GBM_BALANCE_CLASSES", True),
            go_live_min_trades=_int("GO_LIVE_MIN_TRADES", 100),
            go_live_min_profit_factor=_float("GO_LIVE_MIN_PROFIT_FACTOR", 1.3),
            gbm_shadow_model_path=_str("GBM_SHADOW_MODEL_PATH", ""),
            rule_score_ref_lo=_float("RULE_SCORE_REF_LO", 0.30),
            rule_score_ref_hi=_float("RULE_SCORE_REF_HI", 0.70),
            rule_high_conf_threshold=_float("RULE_HIGH_CONF_THRESHOLD", 0.85),
            archive_dir=_str("ARCHIVE_DIR", "archive"),
            archive_interval_s=_float("ARCHIVE_INTERVAL_S", 1800.0),
            trade_stream_enabled=_bool("TRADE_STREAM_ENABLED", False),
            watchlist_size=_int("WATCHLIST_SIZE", 20),
            watchlist_interval_s=_float("WATCHLIST_INTERVAL_S", 15.0),
            pumpportal_ws_url=_str("PUMPPORTAL_WS_URL", "wss://pumpportal.fun/api/data"),
            pumpportal_api_key=_str("PUMPPORTAL_API_KEY"),
            helius_rpc_url=_str("HELIUS_RPC_URL"),
            solana_rpc_url=_str("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),
            dexscreener_base_url=_str("DEXSCREENER_BASE_URL", "https://api.dexscreener.com"),
            honeypot_check_enabled=_bool("HONEYPOT_CHECK_ENABLED", True),
            jupiter_base_url=_str("JUPITER_BASE_URL", "https://quote-api.jup.ag"),
            honeypot_sol_amount=_float("HONEYPOT_SOL_AMOUNT", 0.05),
            honeypot_min_roundtrip_keep=_float("HONEYPOT_MIN_ROUNDTRIP_KEEP", 0.5),
            rugcheck_enabled=_bool("RUGCHECK_ENABLED", True),
            rugcheck_base_url=_str("RUGCHECK_BASE_URL", "https://api.rugcheck.xyz"),
            rugcheck_veto_on_danger=_bool("RUGCHECK_VETO_ON_DANGER", True),
            solanatracker_enabled=_bool("SOLANATRACKER_ENABLED", False),       # WL14 Axiom-style risk data
            solanatracker_api_key=_str("SOLANATRACKER_API_KEY", ""),
            solanatracker_base_url=_str("SOLANATRACKER_BASE_URL", "https://data.solanatracker.io"),
            solanatracker_veto_on_danger=_bool("SOLANATRACKER_VETO_ON_DANGER", False),
            solanatracker_max_risk_score=_float("SOLANATRACKER_MAX_RISK_SCORE", 0.0),
            bitquery_api_key=_str("BITQUERY_API_KEY"),
            moralis_api_key=_str("MORALIS_API_KEY"),
            anthropic_api_key=_str("ANTHROPIC_API_KEY"),
            telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_str("TELEGRAM_CHAT_ID"),
            db_path=_str("DB_PATH", "memebot.db"),
            # P9-harden: SafetyGates + MomentumThresholds were the only nested groups NOT
            # built from env in load(), so every safety/momentum knob silently ignored its
            # env override despite .env.example + CLAUDE.md promising they take effect. These
            # drive the hard rug-avoidance vetoes (concentration, mint/freeze revoked, min
            # liquidity, breadth), so making them overridable matters most. Each default is
            # kept in sync with the dataclass field default above.
            safety=SafetyGates(
                require_mint_revoked=_bool("REQUIRE_MINT_REVOKED", True),
                require_freeze_revoked=_bool("REQUIRE_FREEZE_REVOKED", True),
                require_token2022_safe=_bool("REQUIRE_TOKEN2022_SAFE", True),
                token2022_max_transfer_fee_pct=_float("TOKEN2022_MAX_TRANSFER_FEE_PCT", 5.0),
                min_lp_burned_pct=_float("MIN_LP_BURNED_PCT", 90.0),
                max_bundled_supply_pct=_float("MAX_BUNDLED_SUPPLY_PCT", 50.0),
                max_sniper_volume_pct=_float("MAX_SNIPER_VOLUME_PCT", 50.0),
                reject_creator_in_bundle=_bool("REJECT_CREATOR_IN_BUNDLE", True),
                max_top5_concentration_pct=_float("MAX_TOP5_CONCENTRATION_PCT", 96.0),
                concentration_warn_pct=_float("CONCENTRATION_WARN_PCT", 90.0),
                creator_spam_launches=_int("CREATOR_SPAM_LAUNCHES", 40),
                funder_serial_min=_int("FUNDER_SERIAL_MIN", 3),
                funder_serial_max=_int("FUNDER_SERIAL_MAX", 200),
                holder_cluster_check=_bool("HOLDER_CLUSTER_CHECK", False),
                holder_cluster_top_n=_int("HOLDER_CLUSTER_TOP_N", 4),
                holder_cluster_warn=_int("HOLDER_CLUSTER_WARN", 2),
                name_reuse_warn=_int("NAME_REUSE_WARN", 6),
                creator_rep_min_tokens=_int("CREATOR_REP_MIN_TOKENS", 3),
                creator_rep_rug_rate=_float("CREATOR_REP_RUG_RATE", 0.5),
                creator_rep_half_life_s=_float("CREATOR_REP_HALF_LIFE_S", 604800.0),
                liq_drain_warn_pct=_float("LIQ_DRAIN_WARN_PCT", 20.0),
                creator_sold_pct=_float("CREATOR_SOLD_PCT", 1.0),
                sniper_share_warn=_float("SNIPER_SHARE_WARN", 0.6),
                creator_dump_warn=_float("CREATOR_DUMP_WARN", 0.3),
                bundle_share_warn=_float("BUNDLE_SHARE_WARN", 0.5),
                conc_rise_cut_pct=_float("CONC_RISE_CUT_PCT", 8.0),
                max_single_wallet_pct=_float("MAX_SINGLE_WALLET_PCT", 35.0),
                live_dev_dump_pct=_float("LIVE_DEV_DUMP_PCT", 20.0),
            ),
            momentum=MomentumThresholds(
                volume_spike_mult=_float("VOLUME_SPIKE_MULT", 2.5),
                min_vol_to_mcap_pct=_float("MIN_VOL_TO_MCAP_PCT", 10.0),
                min_buy_sell_ratio=_float("MIN_BUY_SELL_RATIO", 1.0),   # WL1: keep load() default in sync with the dataclass (winner_loss-calibrated 1.5->1.0)
                min_liq_to_mcap_pct=_float("MIN_LIQ_TO_MCAP_PCT", 10.0),
                min_vol_h1=_float("MIN_VOL_H1", 1500.0),
                min_unique_buyers=_int("MIN_UNIQUE_BUYERS", 20),   # WL11: 15->20 (band-validated)
                max_unique_buyers=_int("MAX_UNIQUE_BUYERS", 0),     # WL11 band upper (0=off)
                max_vol_to_mcap_pct=_float("MAX_VOL_TO_MCAP_PCT", 0.0),   # WL12 velocity band upper (0=off)
            ),
            risk=RiskLimits(
                initial_sol=_float("INITIAL_SOL", 10.0),
                max_positions=_int("MAX_POSITIONS", 5),
                max_position_sol=_float("MAX_POSITION_SOL", 1.0),
                daily_loss_cap_sol=_float("DAILY_LOSS_CAP_SOL", 1.5),
                # P9-harden: these three were declared on RiskLimits but never read from env,
                # so PER_TOKEN_COOLDOWN_S / MIN_LIQUIDITY_USD / MAX_MODELED_SLIPPAGE_PCT in .env
                # silently did nothing despite the docs promising they were overridable.
                per_token_cooldown_s=_float("PER_TOKEN_COOLDOWN_S", 300.0),
                min_liquidity_usd=_float("MIN_LIQUIDITY_USD", 3_000.0),
                min_market_cap_usd=_float("MIN_MARKET_CAP_USD", 10_000.0),   # WL10: Axiom pro-filter, data-confirmed
                max_market_cap_usd=_float("MAX_MARKET_CAP_USD", 0.0),         # WL11 band upper (0=off)

                max_modeled_slippage_pct=_float("MAX_MODELED_SLIPPAGE_PCT", 15.0),
                risk_per_trade_frac=_float("RISK_PER_TRADE_FRAC", 0.04),
                max_position_equity_frac=_float("MAX_POSITION_EQUITY_FRAC", 0.05),
                max_liquidity_frac=_float("MAX_LIQUIDITY_FRAC", 0.02),
                max_total_exposure_frac=_float("MAX_TOTAL_EXPOSURE_FRAC", 0.20),
                max_consecutive_losses=_int("MAX_CONSECUTIVE_LOSSES", 5),
                max_rolling_loss_sol=_float("MAX_ROLLING_LOSS_SOL", 1.0),
                rolling_loss_window=_int("ROLLING_LOSS_WINDOW", 8),
                conc_dark_size_mult=_float("CONC_DARK_SIZE_MULT", 0.5),
                max_creator_cohort=_int("MAX_CREATOR_COHORT", 2),
                daily_giveback_frac=_float("DAILY_GIVEBACK_FRAC", 0.7),
                giveback_arm_frac=_float("GIVEBACK_ARM_FRAC", 0.05),
                max_fill_return_mult=_float("MAX_FILL_RETURN_MULT", 20.0),
            ),
            fees=Fees(
                pumpportal_pct=_float("PUMPPORTAL_FEE_PCT", 0.005),
                pumpfun_curve_pct=_float("PUMPFUN_CURVE_FEE_PCT", 0.0125),
                priority_fee_sol=_float("PRIORITY_FEE_SOL", 0.0005),
                migration_fee_sol=_float("MIGRATION_FEE_SOL", 0.015),
                base_slippage_pct=_float("BASE_SLIPPAGE_PCT", 0.03),
            ),
            exit=ExitParams(
                scalp_tp_pct=_float("SCALP_TP_PCT", 0.40),
                scalp_sl_pct=_float("SCALP_SL_PCT", 0.18),
                scalp_max_hold_s=_float("SCALP_MAX_HOLD_S", 180.0),
                hold_tp_pct=_float("HOLD_TP_PCT", 3.0),
                hold_sl_pct=_float("HOLD_SL_PCT", 0.35),
                hold_trail_pct=_float("HOLD_TRAIL_PCT", 0.30),
                hold_max_hold_s=_float("HOLD_MAX_HOLD_S", 86_400.0),
                breakeven_arm_pct=_float("BREAKEVEN_ARM_PCT", 0.12),
                breakeven_floor_pct=_float("BREAKEVEN_FLOOR_PCT", 0.10),
                trail_after_arm_pct=_float("TRAIL_AFTER_ARM_PCT", 0.04),
                scalp_stall_s=_float("SCALP_STALL_S", 75.0),
                partial_tp_pct=_float("PARTIAL_TP_PCT", 0.15),
                partial_tp_frac=_float("PARTIAL_TP_FRAC", 0.7),   # WL2: keep load() default in sync with the dataclass (exitlab-calibrated 0.5->0.7)
                liq_collapse_frac=_float("LIQ_COLLAPSE_FRAC", 0.5),
                derisk_tp_pct=_float("DERISK_TP_PCT", 0.08),
                derisk_max_frac=_float("DERISK_MAX_FRAC", 0.9),
                derisk_proactive_pct=_float("DERISK_PROACTIVE_PCT", 0.0),
                take_initial_pct=_float("TAKE_INITIAL_PCT", 0.3),   # WL7: recover principal at +30% (1.3x) — was 2x; user "bank principal at 1-2x" + addressable-fader decomposition. 0 disables.
                sell_pressure_bsr=_float("SELL_PRESSURE_BSR", 0.7),
                sell_pressure_cut=_bool("SELL_PRESSURE_CUT", False),
            ),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton."""
    return Settings.load()
