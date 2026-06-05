"""memebot entrypoint — Phase 1 paper-trading loop.

Wires the free PumpPortal discovery feed + DexScreener enrichment + Helius
safety checks into a rule-gated, scored paper executor that logs to SQLite.

Run:  python -m memebot      (reads .env; MODE=paper)

Data reality for Phase 1 (free tier): PumpPortal streams give us new-token and
migration events but NOT per-token trades (that stream is metered). So market
metrics (price/volume/liquidity/txns) come from DexScreener, polled per cycle.
Tick-level volume-spike / unique-buyer breadth arrives in Phase 4.
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import AsyncExitStack

from .agent.backends import make_llm
from .agent.brain import TradingBrain
from .agent.memory import AgentMemory
from .agent.schema import Verdict
from .alerts.commands import TelegramCommands
from .alerts.telegram import TelegramAlerter
from .backtest.calibrate import creator_reputation, readiness, suggest_thresholds
from .backtest.dataset import build_dataset, summarize
from .backtest.label import _load_all_obs
from .backtest.separation import separation_report, verdict as sep_verdict
from .config import Settings, get_settings
from .constants import MODE_HOLD, MODE_SCALP, SIDE_BUY, SIDE_SELL
from .data.creator_history import CreatorHistory
from .data.funder_history import FunderRegistry
from .data.metadata_history import MetadataRegistry
from .data.smart_money import SmartMoney
from .data.token_state import TokenRegistry
from .execution.paper import PaperBackend
from .execution.pricing import PriceSource
from .feed.dexscreener import DexScreenerClient
from .feed.helius_rpc import HeliusRPC, largest_funder_cluster
from .feed.jupiter import JupiterClient
from .feed.rugcheck import RugCheckClient
from .feed.pumpportal_ws import PumpPortalFeed
from .feed.watchlist import WatchlistManager
from .filter import rules
from .filter.features import build_features
from .filter.gbm import GBMScorer
from .models import Candidate, MigrationEvent, NewTokenEvent, TradeEvent
from .portfolio.pnl import compute_stats
from .portfolio.portfolio import Portfolio
from .risk.limits import RiskManager, expected_return, kelly_fraction, reeval_action
from .signals.scoring import Scorer, select_mode
from .signals.setup import classify_setup, setup_type
from .storage.archive import ParquetArchiver
from .storage.db import Storage
from .utils.logging import get_logger, setup_logging
from .utils.money import round_trip_cost_pct, sell_cost_pct
from .utils.singleton import InstanceLock, SingleInstanceError

log = get_logger("main")


class Bot:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.registry = TokenRegistry()
        self.creator_history = CreatorHistory()   # serial-spam / rug-factory tell (launch count per creator)
        self.funder_history = FunderRegistry()    # N12: re-link rotated creators by common funder (cluster tell)
        self._holder_funder_cache: dict[str, str | None] = {}   # holder-cluster: wallet -> funder (immutable; cache to bound cost)
        self.metadata_history = MetadataRegistry()  # branding-duplication (name/ticker reuse = scam-factory tell)
        self.smart_money = SmartMoney()            # P8: per-wallet cross-token PnL -> copy-trade signal (G3 tape)
        self.storage = Storage(settings.db_path)
        self.events: asyncio.Queue = asyncio.Queue(maxsize=20_000)
        self.feed = PumpPortalFeed(
            settings.pumpportal_ws_url, self.events,
            # the api-key is ONLY attached when the metered trade stream is enabled (it spends SOL)
            api_key=(settings.pumpportal_api_key if settings.trade_stream_enabled else ""),
        )
        self.dex = DexScreenerClient(settings.dexscreener_base_url)
        self.helius = HeliusRPC(settings.helius_rpc) if settings.helius_rpc else None
        # A2: quote-only Jupiter honeypot check (no key, no real money); None = disabled.
        self.jupiter = JupiterClient(settings.jupiter_base_url) if settings.honeypot_check_enabled else None
        # B1: RugCheck.xyz risk cross-check (free, no key); None = disabled.
        self.rugcheck = RugCheckClient(settings.rugcheck_base_url) if settings.rugcheck_enabled else None
        self.portfolio = Portfolio(settings.risk.initial_sol)
        self.exit_params = settings.exit       # env-overridable defensive profile
        self.risk = RiskManager(settings)
        self.alerter = TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id)
        self.prices: PriceSource | None = None
        self.executor: PaperBackend | None = None
        self._price_cache: dict[str, float] = {}
        self._price_misses: dict[str, int] = {}     # consecutive no-price polls per open position
        self._max_price_misses = settings.max_price_misses   # W2: config-driven blackout cut (~40s at default 20)
        self._conc_dark = False                     # N8: set in run() — True when the RPC can't serve holder concentration (shrink size)

        # autonomous brain (Claude advisory; risk layer still vetoes). Degrades
        # to a rule-based fallback when no API key / anthropic package.
        self.llm = make_llm(settings)
        self.memory = AgentMemory(self.storage, miss_recall_frac=self.s.miss_recall_frac)
        # Phase 2: GBM scorer if a trained model exists, else the rule scorer.
        self.scorer = Scorer(settings, GBMScorer.load(settings.gbm_model_path))
        # P4 shadow mode (off unless gbm_shadow_model_path is set): a candidate model that SCORES +
        # logs (into each observation's `shadow_score`) but NEVER sizes a trade — live validation
        # of a fresh model against real forward outcomes before it is promoted to drive entries.
        self.shadow_scorer = (GBMScorer.load(settings.gbm_shadow_model_path)
                              if settings.gbm_shadow_model_path else None)
        # GBM probabilities are on a different scale than the rule score, so the
        # entry gate is threshold-by-kind (shared with the brain).
        self.entry = settings.gbm_entry_threshold if self.scorer.kind == "gbm" else settings.entry_threshold
        # high-conf (slam-dunk) tier is scale-specific too: GBM is a true probability (0.80),
        # the rescaled rule score uses its own higher bar (0.85, kept rare — no proven edge).
        self.high_conf = settings.high_conf_threshold if self.scorer.kind == "gbm" else settings.rule_high_conf_threshold
        self.brain = TradingBrain(settings, self.memory, self.llm,
                                  entry_threshold=self.entry, high_conf_threshold=self.high_conf)
        self.commands = TelegramCommands(settings, self.alerter, self)   # /pnl /positions /stop ...
        self.archiver = ParquetArchiver(settings.db_path, settings.archive_dir)
        self.watchlist = WatchlistManager(self.feed, self.registry, settings)  # metered, off by default
        self._reflect_q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._miss_q: asyncio.Queue = asyncio.Queue(maxsize=200)   # P8: missed-winner reflections (off hot path)
        self._patience = {"observed": 0, "ranked": 0, "bought": 0}  # P8: process-life selectivity counters
        self._instance_lock: InstanceLock | None = None   # P8: single-instance guard (no double-write)
        self._last_event_ts = time.time()                 # P8: feed-liveness watchdog (set in ingest)
        self._trade_events = 0                             # G3: count metered trade-stream events ingested
        self._g3_zero_reports = 0                          # G3: consecutive report cycles with 0 events (dead-stream watchdog)
        self._creator_rep: dict[str, dict] = {}  # P8: learned per-creator realized rug/win rate (calibrate loop)
        self._last_calib_lesson: str | None = None  # dedupe the advisory calibrate lesson across cycles
        self._last_sep_lesson: str | None = None  # P10 #6: dedupe the AUC-separation lesson across cycles
        self._outcome_base: dict | None = None    # P10 #8: the bot's own measured rug/fade/winner base rate (advisory prior to the brain)
        self._closing: set[str] = set()          # guards against double-close (manage vs reeval)

    # ── helpers ────────────────────────────────────────────────────────────────
    def _price_map(self) -> dict[str, float]:
        return {
            m: self._price_cache.get(m, p.avg_price_sol)
            for m, p in self.portfolio.positions.items()
        }

    def _account_state(self) -> dict:
        """AI2: a compact snapshot of the bot's OWN book for the brain to reason on."""
        pm = self._price_map()
        equity = self.portfolio.equity(pm)
        recent = self.portfolio.closed[-20:]
        rwins = sum(1 for t in recent if t.pnl_sol > 0)
        return {
            "equity_pct": (equity / self.portfolio.initial_sol - 1.0) * 100.0 if self.portfolio.initial_sol > 0 else 0.0,
            "open": self.portfolio.open_count(),
            "max_positions": self.s.risk.max_positions,
            "daily_loss": self.portfolio.daily_loss(pm),
            "daily_cap": self.s.risk.daily_loss_cap_sol,
            "streak": self.risk.consecutive_losses,
            "recent_n": len(recent),
            "recent_win": (rwins / len(recent)) if recent else 0.0,
            "outcome_base": getattr(self, "_outcome_base", None),  # P10 #8: own measured rug/fade/winner base rate (advisory prior)
        }

    # ── tasks ────────────────────────────────────────────────────────────────
    async def _ingest_loop(self) -> None:
        while True:
            ev = await self.events.get()
            self._last_event_ts = time.time()         # P8: feed-liveness watchdog heartbeat
            try:
                if isinstance(ev, NewTokenEvent):
                    st = self.registry.on_new_token(ev)
                    self.creator_history.record(st.creator)   # tally the creator's launches (spam/rug tell)
                    self.metadata_history.record(st.name, st.symbol)   # tally name/ticker reuse (factory tell)
                    await self.storage.upsert_token(st)
                elif isinstance(ev, TradeEvent):
                    self._trade_events += 1           # G3: metered trade-stream heartbeat
                    self.registry.on_trade(ev)        # only present if watching trades (Phase 4)
                    self.smart_money.on_trade(ev.trader, ev.mint, ev.side, ev.sol_amount, ev.token_amount)
                elif isinstance(ev, MigrationEvent):
                    st = self.registry.on_migration(ev)
                    if st is not None:
                        await self.storage.upsert_token(st)
            except Exception:  # noqa: BLE001
                log.exception("ingest error")
            finally:
                self.events.task_done()

    async def _eval_loop(self) -> None:
        while True:
            await asyncio.sleep(self.s.eval_interval_s)
            try:
                await self._evaluate_once()
            except Exception:  # noqa: BLE001
                log.exception("eval cycle error")

    def _features_json(self, c: Candidate) -> str:
        """Feature JSON for logging. In P4 shadow mode (gbm_shadow_model_path set) it also records the
        candidate model's probability as `shadow_score` — a non-FEATURE_NAMES key, ignored by training
        and scoring — so a not-yet-promoted model is validated against real forward outcomes, never
        affecting a live decision."""
        feats = build_features(c)
        if self.shadow_scorer is not None:
            feats["shadow_score"] = self.shadow_scorer.score(c)
        # W5: log creator launch count (non-FEATURE_NAMES key, ignored by training/scoring) so the
        # creator-history vs outcome separation can be studied + a threshold calibrated from labels.
        if "creator_launches" in c.features:
            feats["creator_launches"] = c.features["creator_launches"]
        # P7: log the live TRADE-FLOW signal (sells-dominating-on-the-tape) as non-FEATURE_NAMES keys
        # alongside every observation. This is the user's "experienced trader senses the rug from the
        # trades" tell turned into DATA: accumulated against forward outcomes so P4 can LEARN the
        # rug-from-tradeflow separation (and a threshold be calibrated) before it's promoted to a model
        # feature. Ignored by the current rule scorer + training column order — pure dataset enrichment.
        feats["bsr_h1"] = float(c.buy_sell_ratio)
        feats["sell_pressure"] = 1.0 if self._sell_pressure(
            c.buy_sell_ratio, c.price_change_m5, self.exit_params.sell_pressure_bsr) else 0.0
        return json.dumps(feats)

    def _candidate_from_snapshot(self, st, snap) -> Candidate:
        """Build a Candidate from a token state + DexScreener snapshot — shared by the ranking path
        and the W2 held-observation path. Sets market fields + trend/tech features; runs NO gates."""
        c = Candidate(mint=st.mint, symbol=st.symbol or (snap.symbol if snap else ""))
        # A non-pumpfun DEX pool on DexScreener means the token has graduated, even if we missed its
        # MigrationEvent. Fixes scalp/hold misclassification.
        if snap and snap.dex_id not in ("", "pumpfun"):
            st.migrated = True
            # LP-LOCK (researched 2026-06): since 2025-03 pump.fun graduates onto its own AMM (PumpSwap),
            # whose `migrate` instruction BURNS the pool LP by protocol design — so a pump-AMM graduate has
            # locked liquidity GUARANTEED, with NO RPC call. Populate the otherwise-DEAD lp_burned_pct as
            # ADVISORY/confirmatory (it revives the gate + removes false comfort). A NON-pump pool (e.g.
            # raydium / a dev-made pool) stays None=unknown — the rare 'pullable' case would need an LP-mint
            # read we deliberately don't spend (LOW value: the dominant rug is a TOKEN dump = concentration,
            # not an LP pull; only ~0.6% graduate at all). NOT a hard veto; a 100 here just stops the no-op.
            if c.lp_burned_pct is None and snap.dex_id.startswith("pump"):   # pump* AMM, pumpfun already excluded
                c.lp_burned_pct = 100.0
        c.mode = select_mode(st)
        if snap:
            c.price_sol = snap.price_native
            c.price_usd = snap.price_usd
            c.liquidity_usd = snap.liquidity_usd
            c.market_cap_usd = snap.market_cap
            c.vol_to_mcap_pct = snap.vol_to_mcap_pct
            c.buy_sell_ratio = snap.buy_sell_ratio_h1
            c.unique_buyers = snap.buys_h1            # Phase 1 proxy (txn count, not wallets)
            c.price_change_m5 = snap.price_change_m5  # trend signals -> the AI reasons on trajectory
            c.price_change_h1 = snap.price_change_h1
            c.features["vol_h1"] = snap.volume_h1
            # P10b SR2/SR3/SR4: advisory DATASET-ONLY keys (NOT in FEATURE_NAMES — accrue against forward
            # outcomes for separation study before any model promotion, matching the bsr_h1 discipline).
            c.features["sells_h1"] = snap.sells_h1
            if snap.volume_h24 > 0:
                c.features["vol_conc_now"] = min(1.0, snap.volume_h1 / snap.volume_h24)  # freshness: 1h share of 24h vol
            if (snap.buys_m5 + snap.sells_m5) > 0:
                c.features["bsr_m5"] = snap.buy_sell_ratio_m5
                c.features["txns_m5"] = snap.buys_m5 + snap.sells_m5
            st.record_price(snap.price_usd)           # build the short-term trendline
            st.record_liquidity(snap.liquidity_usd)   # build the liquidity trend (drain = rug pre-tell)
            st.record_buyers(snap.buys_h1)            # N13: build the buyer-breadth velocity (free DexScreener)
            st.record_sells(snap.sells_h1)           # P10b SR5: sell-count velocity (mirror of buyers)
            bt = st.buyer_trend()                     # accelerating vs fading breadth (weak GBM contributor)
            if bt["n"] >= 3:
                c.features["buyer_growth"] = bt["growth"]
            stt = st.sell_trend()                     # P10b SR5: accelerating selling = fade/rug cue (dataset-only)
            if stt["n"] >= 3:
                c.features["sell_growth"] = stt["growth"]
        else:
            c.price_sol = st.last_price_sol           # pre-index: curve price only
        c.features["vol_spike"] = st.volume_spike()
        c.features["age_s"] = st.age_s()              # P8: token age -> age-normalized velocity feature
        c.features["trend"] = st.price_trend()        # direction / % change / off-high for the AI prompt
        c.features["tech"] = st.technicals()          # EMA-cross / breakout / support-resistance (AI4)
        c.features["liq_trend"] = st.liquidity_trend()  # P8: is the pool filling or DRAINING (rug pre-tell)
        c.creator = st.creator                        # N9: carry the creator wallet -> cohort cap + Position
        c.features["creator_launches"] = self.creator_history.launch_count(st.creator)  # serial-spam/rug tell
        mh = getattr(self, "metadata_history", None)  # branding-duplication tell (name/ticker reuse); stub-safe
        if mh is not None:
            c.features["name_reuse_count"] = mh.reuse_count(st.name, st.symbol)
        # N12: distinct creators sharing this creator's FUNDER (re-links rotated wallets). None until
        # resolved -> omit (no penalty on missing data). getattr keeps the unbound-stub tests working.
        fh = getattr(self, "funder_history", None)
        if fh is not None:
            fcc = fh.funder_creator_count(st.creator)
            if fcc is not None:
                c.features["funder_creator_count"] = fcc
        # P8: this creator's REALIZED track record learned from our own classified outcomes (advisory).
        # getattr keeps the unbound-stub test working (the cache is a Bot instance attribute).
        rep = getattr(self, "_creator_rep", {}).get(st.creator)
        if rep:
            c.features["creator_rug_rate"] = rep["rug_rate"]
            c.features["creator_rep_n"] = rep["n"]
            # P10 #7: also surface the POSITIVE track record (win_rate) so a proven repeat-legit dev
            # earns advisory credit, not only blame. Advisory-only (NOT in FEATURE_NAMES, NOT in any
            # deterministic gate — rug_rate's rules.py penalty stays; positivity never relaxes a gate).
            c.features["creator_win_rate"] = rep["win_rate"]
            c.features["creator_rug_rate_recent"] = rep.get("rug_rate_recent", rep["rug_rate"])  # P10b CAL3 (advisory, recency-weighted)
        if st.trades:   # real tick data when the (metered) trade stream is on (G3)
            ub = st.unique_buyers(60.0)
            if ub:
                # distinct wallets/60s — a DIFFERENT metric than the h1-txn-count gate, so keep it
                # as a separate feature rather than overwriting the gated field.
                c.features["unique_buyers_tick"] = ub
            bsr = st.buy_sell_ratio(60.0)
            if bsr > 0:
                c.buy_sell_ratio = bsr   # a ratio is dimensionless -> safe to refine
            # G3 tape signals (the user's asks): sniper concentration + real-time creator dump
            ss = st.sniper_share()
            if ss is not None:
                c.features["sniper_share"] = ss
            cd = st.creator_dump_ratio(st.creator)
            if cd is not None:
                c.features["creator_dump_ratio"] = cd
            bs = st.bundle_share()      # the research's #1 missing signal: coordinated same-block buys
            if bs is not None:
                c.features["bundle_share"] = bs
            # P8 copy-trade: are PROFITABLE wallets buying this token? (the positive axis vs bundle/sniper).
            # getattr keeps the unbound-stub test working (smart_money is a Bot instance attribute).
            sm = getattr(self, "smart_money", None)
            if sm is not None:
                sm_buys = [t for t in st.trades if t.side == SIDE_BUY]
                sm_tot = sum(t.sol for t in sm_buys)
                if sm_tot > 0 and len(sm_buys) >= 5:
                    c.features["smart_money_share"] = sum(t.sol for t in sm_buys if sm.is_smart(t.trader)) / sm_tot
        return c

    async def _observe_held(self, held: list, snaps: dict) -> None:
        """W2: HELD positions are excluded from the buy path, so they used to stop being observed
        exactly when their 5m P4 label forms (avg hold ~183s < 300s horizon). Snapshot them too:
        (1) log an in-distribution observation so the label completes, and (2) cut a RUG fast when
        the pool drains below liq_collapse_frac of entry liquidity — the real rug tell the -35%
        price SL gaps past (rugs realized -90% with only ~3% slippage). Never opens/sizes."""
        for st in held:
            snap = snaps.get(st.mint)
            if snap is None or snap.liquidity_usd <= 0:
                continue
            pos = self.portfolio.positions.get(st.mint)
            if pos is None:
                continue
            c = self._candidate_from_snapshot(st, snap)
            entry_liq = float(pos.entry_features.get("liquidity_usd", 0.0))
            if self._liq_collapsed(snap.liquidity_usd, entry_liq,
                                   self.s.risk.min_liquidity_usd, self.exit_params.liq_collapse_frac):
                await self._close_position(st.mint, "liq_collapse")
                continue
            # P7: watch the TRADE FLOW behind the position (user: "buy hiisnii daraa mash sain
            # hynah") — sells dominating on the tape + price rolling over = a rug being worked. If
            # we're in profit, bank the principal and free-roll the rest (zero new downside); if not
            # in profit, only an opt-in cut fires (the SL/liq_collapse own the deep downside).
            if self._sell_pressure(snap.buy_sell_ratio_h1, snap.price_change_m5,
                                   self.exit_params.sell_pressure_bsr):
                await self._derisk_if_profitable(st.mint, snap.price_native, "derisk_flow")
                pos = self.portfolio.positions.get(st.mint)
                if self.exit_params.sell_pressure_cut and pos is not None and not pos.partial_taken:
                    await self._close_position(st.mint, "sell_pressure")
                    continue
            self.scorer.score(c)
            await self.storage.log_observation(c, self._features_json(c))

    @staticmethod
    def _liq_collapsed(current_liq: float, entry_liq: float, min_liq: float, frac: float) -> bool:
        """W2: a HOLD's pool has RUGGED if its liquidity fell below the absolute floor OR below
        `frac` of its entry liquidity — the rug tell the -35% price SL gaps past. Only meaningful
        for an indexed snapshot (current_liq>0); a missing snapshot is the no_price blackout's job."""
        if current_liq <= 0:
            return False
        return current_liq < min_liq or (entry_liq > 0 and current_liq < entry_liq * frac)

    @staticmethod
    def _concentration_rose(current: float | None, entry: float | None, threshold: float) -> bool:
        """W6: True when a held position's top-5 concentration has RISEN >= threshold percentage-points
        above its ENTRY level (dev/insiders consolidating = distribution prep = rug imminent). Both
        must be known (entry >= 0); threshold <= 0 disables. The TREND, not the static level."""
        if current is None or entry is None or entry < 0 or threshold <= 0:
            return False
        return current - entry >= threshold

    @staticmethod
    def _concentration_vetoed(conc: float | None, threshold: float) -> bool:
        """W3: veto a BUY when top-5 (non-vault) holder concentration is known AND exceeds the extreme
        threshold (whale-rug tell). Unknown concentration (None / public-RPC dark) -> no veto (never
        block on missing data — the dataset stays honest, the scorer keeps concentration advisory)."""
        return conc is not None and conc > threshold

    @staticmethod
    def _dark_conc_size_frac(size_frac: float, dark: bool, mult: float) -> float:
        """N8: shrink the per-trade size fraction when holder concentration is DARK (the #1 free rug
        tell is unavailable on the public RPC) — size/selectivity is the survival lever. No-op when
        concentration is live (dark=False) or the multiplier is 1.0."""
        return size_frac * mult if dark else size_frac

    @staticmethod
    def _cohort_full(positions: dict, creator: str, cap: int) -> bool:
        """N9: True when the number of OPEN positions sharing this creator wallet has reached the cap.
        cap<=0 disables; an empty creator (unknown) is never capped (don't penalize missing data)."""
        if cap <= 0 or not creator:
            return False
        return sum(1 for p in positions.values() if p.creator == creator) >= cap

    @staticmethod
    def _sell_pressure(bsr_h1: float, price_change_m5: float, threshold: float) -> bool:
        """P7: the live 'rug-on-the-tape' tell on a HELD position — sells dominating (buy/sell ratio
        below `threshold`) AND the price rolling over (m5 < 0). DexScreener's h1 ratio is a LAGGING
        hourly aggregate, so we require the m5-price corroboration to cut noise. threshold <= 0
        disables; bsr_h1 <= 0 (unknown) -> no signal. This is what an experienced trader FEELS as
        'about to get dumped' — made into data so the AI can act on it AND learn it."""
        return threshold > 0.0 and 0.0 < bsr_h1 < threshold and price_change_m5 < 0.0

    async def _evaluate_once(self) -> None:
        assert self.executor is not None
        now = time.time()
        self.registry.evict_stale(now)

        active = self.registry.active(self.s.watch_max_age_s, self.s.watch_limit)
        if not active:
            return
        held = [st for st in active if self.portfolio.has_position(st.mint)]
        states = [st for st in active if not self.portfolio.has_position(st.mint)]

        snaps, _ = await self.dex.snapshots([st.mint for st in active])   # live: missing = retry next cycle
        ranked: list[Candidate] = []
        safety_budget = self.s.max_safety_checks_per_cycle

        for st in states:
            self._patience["observed"] += 1               # P8: a token we evaluated (the selectivity denominator)
            snap = snaps.get(st.mint)
            c = self._candidate_from_snapshot(st, snap)

            rules.evaluate(c, self.s)                     # market gates first (cheap)
            if not c.rule_passed:
                # P2: a market-gate FAILER is still an in-distribution OBSERVATION. `candidates`
                # only ever kept gate-survivors (winners) — the survivorship bias that sank the
                # offline-trained GBM. Logging the full indexed population (incl. failers and
                # tokens decaying toward death) is the structural cure. Pre-safety features here
                # match what the live scorer sees for a failer (it never gets the safety fetch).
                if c.liquidity_usd > 0:
                    self.scorer.score(c)
                    c.features["skip_reason"] = "market_gate"   # P10b #4: per-filter regret attribution
                    c.features["rule_reasons"] = list(c.rule_reasons)   # spec-S7: per-gate-reason rug-dodge attribution
                    await self.storage.log_observation(c, self._features_json(c))
                continue

            # safety check only for market-gate survivors, and only within budget
            if self.helius is not None and safety_budget > 0:
                safety_budget -= 1
                c.mint_revoked, c.freeze_revoked = await self.helius.get_authorities(st.mint)
                # A1: inspect Token-2022 extensions for stealth rug vectors (transfer hook / permanent
                # delegate / default-frozen / high transfer fee) that classic mint/freeze checks miss.
                if self.s.safety.require_token2022_safe:
                    c.token2022_risk = await self.helius.get_token2022_risk(
                        st.mint, max_transfer_fee_pct=self.s.safety.token2022_max_transfer_fee_pct)
                    if c.token2022_risk:
                        c.features["token2022_risk"] = c.token2022_risk   # dataset-only: study the lift later
                # W3: fetch concentration for any INDEXED token (liquidity>0 == a real DEX pool == off
                # the curve), not just st.migrated — broadens coverage of the entry whale-rug veto to
                # every tradeable HOLD (DexScreener may tag a graduated pool without flipping migrated).
                if st.migrated or c.liquidity_usd > 0:
                    c.top5_concentration_pct = await self.helius.top5_concentration(st.mint)
                    # P8: has the CREATOR sold their allocation? (user's #1 setup tell). 0% = dumped
                    # (strong rug signal); a real holding = skin in the game. Free Helius read.
                    if st.creator:
                        chp = await self.helius.creator_holding_pct(st.mint, st.creator)
                        if chp is not None:
                            c.features["creator_holding_pct"] = chp
                    # HOLDER funding-cluster (free-data concealed-concentration): top non-vault holders'
                    # owners -> funders (cached, immutable) -> flag when several share ONE funder = one
                    # entity behind many wallets. Cost-gated (OFF by default); bounded + cached.
                    if self.s.safety.holder_cluster_check:
                        funders = []
                        for o in await self.helius.get_holder_owners(st.mint, top_n=self.s.safety.holder_cluster_top_n):
                            if o not in self._holder_funder_cache:
                                self._holder_funder_cache[o] = await self.helius.get_funder(o)
                            if self._holder_funder_cache[o]:
                                funders.append(self._holder_funder_cache[o])
                        cluster = largest_funder_cluster(funders)
                        if cluster >= 2:
                            c.features["holder_funder_cluster"] = cluster
                rules.evaluate(c, self.s)                 # re-gate with safety data
                if not c.rule_passed:
                    if c.liquidity_usd > 0:               # P2: safety-failer observation (resolved safety)
                        self.scorer.score(c)
                        c.features["skip_reason"] = "safety_gate"   # P10b #4: per-filter regret attribution
                        c.features["rule_reasons"] = list(c.rule_reasons)   # spec-S7: per-gate-reason rug-dodge attribution
                        await self.storage.log_observation(c, self._features_json(c))
                    continue

            self.scorer.score(c)
            grade, why, quality = classify_setup(c, self.s)   # P1: survival-first setup gate
            c.features["setup"] = {"grade": grade, "quality": quality, "reasons": why}
            c.features["setup_type"] = setup_type(c, self.s)   # the SITUATION/playbook classification (orchestration layer)
            c.features["skip_reason"] = "passed_gates"          # P10b #4: cleared the hard gates (a not-bought one was a decision-layer skip)
            feats_json = self._features_json(c)
            # P2: log the SURVIVOR observation AFTER resolved safety + the final score, so these
            # in-distribution rows match what the live model scores at inference (no train/serve
            # skew). Each indexed token is logged exactly ONCE, at its most-resolved point.
            if c.liquidity_usd > 0:
                await self.storage.log_observation(c, feats_json)
            await self.storage.log_candidate(c, feats_json)
            if self.s.require_good_setup and grade == "bad":
                continue                                       # drop falling-knife / ghost-pool late entries
            self._patience["ranked"] += 1                      # P8: cleared every gate -> a real buy candidate
            ranked.append(c)

        ranked.sort(key=lambda x: x.score, reverse=True)
        self_state = self._account_state()             # AI2: the brain reasons on its own book
        await self._decide_and_open(ranked, self_state)

        # W2: observe HELD positions (P4 label coverage) + cut rugs on pool collapse. After the buy
        # loop so a liq_collapse close frees a slot only once this cycle's entries are placed.
        if held:
            await self._observe_held(held, snaps)

    async def _decide_and_open(self, ranked: list[Candidate], self_state: dict) -> None:
        """Decide the top candidates CONCURRENTLY (bounded by llm_parallel), then apply buys
        SEQUENTIALLY under the live risk gate. The parallel decide is the throughput win — a fresh
        launch no longer waits behind earlier candidates' slow LLM round-trips — while the sequential
        apply keeps max_positions/exposure honest. `ranked` must be score-descending."""
        # ranked is score-desc, so the sub-entry tail is contiguous -> filtering is equivalent to the
        # old `break`, and keeps the highest-scored candidates first for the scarce position slots.
        eligible = [c for c in ranked if c.score >= self.entry]
        free = max(0, self.s.risk.max_positions - self.portfolio.open_count())
        if not eligible or free <= 0:
            return
        # PHASE 1 — decide concurrently. No buys here: the verdict is advisory and the risk layer
        # re-checks at open, so deciding over a one-time self_state snapshot can't over-allocate. We
        # decide ALL eligible (not just `free`) on purpose: the highest-scored are first (score-desc),
        # the per-minute LLM budget hard-caps actual model calls (the rest are instant rule verdicts),
        # and keeping fallbacks decided means a vetoed top candidate doesn't waste the cycle.
        sem = asyncio.Semaphore(max(1, self.s.llm_parallel))

        async def _decide(cand: Candidate):
            async with sem:
                try:
                    return cand, await self.brain.decide(cand, self_state)
                except Exception as e:  # noqa: BLE001 — fault-isolate: one bad verdict must not drop the batch
                    log.warning("decide failed for %s, treating as skip: %s", cand.mint[:8], e)
                    return cand, Verdict(action="skip", mode=cand.mode, reasoning=f"decide error: {e}")

        decided = await asyncio.gather(*(_decide(c) for c in eligible))   # preserves eligible (score-desc) order
        # PHASE 2 — apply buys SEQUENTIALLY under the live risk gate. Re-check open_count()/
        # has_position per buy as state mutates; this single-task sequential apply is what prevents
        # any double-open / over-max_positions. Do NOT parallelize _try_open.
        for c, verdict in decided:
            # P10b #2: log EVERY decided verdict, including SKIPs (the most common loss-min action,
            # previously dropped), with the candidate score — so 'good skips' (avoided losses) become
            # measurable vs missed winners. These are already the eligible/decided set, so it stays off
            # the per-token hot path (gate-failers are covered by observations, not re-logged here).
            await self.storage.log_verdict(mint=c.mint, symbol=c.symbol, v=verdict, score=c.score)
            if verdict.action != "buy":
                continue
            if self.portfolio.open_count() >= self.s.risk.max_positions:
                break
            if self.portfolio.has_position(c.mint):
                continue
            await self._try_open(c, verdict)

    @staticmethod
    def _has_momentum(c: Candidate) -> bool:
        """A real, visible upward signal — so we don't SCALP blind (AI8)."""
        if c.price_change_m5 > 0 or c.price_change_h1 > 0:
            return True
        if (c.features.get("trend") or {}).get("dir") == "rising":
            return True
        return float(c.features.get("vol_spike", 0.0)) >= 1.5

    @staticmethod
    def _trend_broke(trend: dict, tech: dict, *, min_polls: int = 5, min_drop_pct: float = 10.0) -> bool:
        """P8 (was P3): a CONFIRMED live trend break, not 6s of noise. The old rule (n>=3 polls ~= 6s
        AND ANY single tech flag) fired the fade exit 0/14 — all losers, the biggest real PnL leak —
        because a young token's polled series is jittery. Now require EITHER a sustained falling trend
        WITH real magnitude (>= min_polls polls AND chg_pct <= -min_drop_pct), OR a CORROBORATED
        technical breakdown (breakdown AND EMA bear together, not either alone)."""
        falling = (trend.get("n", 0) >= min_polls and trend.get("dir") == "falling"
                   and trend.get("chg_pct", 0.0) <= -abs(min_drop_pct))
        breakdown = tech.get("breakout") == "down" and tech.get("ema_signal") == "bear"
        return falling or breakdown

    @staticmethod
    def _data_backed_setup(c: Candidate) -> bool:
        """A trade needs REAL market data to assess a setup. A curve-only token with no
        DexScreener pool (liquidity_usd<=0) can't be classified — trading it blind just
        bleeds fees (live finding). Survive via good DATA-backed setups, not early guesses (AI9)."""
        return c.liquidity_usd > 0

    async def _try_open(self, c: Candidate, verdict: Verdict) -> None:
        assert self.executor is not None
        mode = verdict.mode or c.mode
        # W2: SCALP buy lane is OFF by default — full-history reconstruction shows SCALP is 0/39 wins,
        # pure round-trip bleed. Gate on the INTRINSIC c.mode (set by select_mode from migration
        # status: non-migrated curve token -> scalp), NOT the LLM-overridable verdict.mode — else the
        # brain could relabel a curve-only scalp as "hold" and bypass the veto (review HIGH). The
        # candidate was still scored + observed upstream (P4 data kept); only the BUY is vetoed.
        if c.mode == MODE_SCALP and not self.s.trade_scalp:
            log.debug("skip scalp %s: SCALP lane disabled (trade_scalp=False)", c.symbol or c.mint[:6])
            return
        # AI9: don't trade tokens we have no real data for (blind curve-only) — they only bleed.
        if self.s.require_data_backed_setup and not self._data_backed_setup(c):
            log.debug("skip %s: no data-backed setup (liquidity_usd<=0)", c.symbol or c.mint[:6])
            return
        # W3: veto a HOLD whose top-5 (non-vault) holders own an EXTREME share — the whale-rug tell,
        # now that holder concentration is live (free Helius RPC). The catastrophic losses are all
        # whale dumps that no entry score/liquidity separated; concentration is the one tell that
        # does. Conservative (only near-total dominance, the existing 'extreme' line); concentration
        # stays advisory in the scorer/dataset — only the BUY is vetoed, so the token is still
        # scored/observed/labeled. Skipped silently when concentration is unknown (no false veto).
        if self._concentration_vetoed(c.top5_concentration_pct, self.s.safety.max_top5_concentration_pct):
            log.debug("skip %s: top5 concentration %.0f%% > %.0f%% (whale-rug risk)",
                      c.symbol or c.mint[:6], c.top5_concentration_pct, self.s.safety.max_top5_concentration_pct)
            return
        # AI8: a scalp with no visible momentum just bleeds the round-trip fee (live stress test).
        if mode == MODE_SCALP and self.s.require_scalp_momentum and not self._has_momentum(c):
            log.debug("skip blind scalp %s: no momentum signal", c.symbol or c.mint[:6])
            return
        price_map = self._price_map()
        ok, reason = self.risk.can_open(self.portfolio, price_map, c.mint)
        if not ok:
            return
        # N9: correlated-cohort cap — N open positions from the SAME creator are really ONE bet (research:
        # catastrophic losses are correlated flushes). Block a buy once the creator's open cohort is full.
        if self._cohort_full(self.portfolio.positions, c.creator, self.s.risk.max_creator_cohort):
            log.debug("skip %s: creator cohort full (max %d concurrent from one creator)",
                      c.symbol or c.mint[:6], self.s.risk.max_creator_cohort)
            return
        # cost-aware expected-return gate: don't open trades whose edge can't clear fees+slippage
        tp = verdict.tp_pct or (self.exit_params.scalp_tp_pct if mode == MODE_SCALP else self.exit_params.hold_tp_pct)
        sl = verdict.sl_pct or (self.exit_params.scalp_sl_pct if mode == MODE_SCALP else self.exit_params.hold_sl_pct)
        # EV must use a realistic expected win — not the +300% hold ceiling, which the
        # trailing stop never realizes; cap the hold win at the trail width for the gate.
        tp_for_ev = tp if mode == MODE_SCALP else min(tp, self.exit_params.hold_trail_pct)
        er = expected_return(verdict.conviction, tp_for_ev, sl, round_trip_cost_pct(self.s.fees))
        if er < self.s.min_expected_return:
            log.debug("skip %s: expected_return %.3f < min %.3f", c.symbol or c.mint[:6], er, self.s.min_expected_return)
            return
        equity = self.portfolio.equity(price_map)        # size scales with equity -> compounds
        liq_sol = (c.liquidity_usd * c.price_sol / c.price_usd) if (c.price_usd > 0 and c.price_sol > 0) else 0.0
        if self.s.use_kelly_sizing:
            # W6: edge-proportional (fractional-Kelly) sizing — conviction as the win-prob p, the
            # trail-capped tp_for_ev/sl as the reward:risk odds. OFF by default (needs a calibrated
            # p from P4). The per-trade (5%) + exposure caps in position_size_sol still bind on top.
            size_frac = kelly_fraction(verdict.conviction, tp_for_ev, sl, self.s.kelly_fraction_mult)
        else:
            size_frac = verdict.size_pct if verdict.size_pct > 0 else 0.5
        # P1: scale size by setup quality — marginal setups risk half, clean setups full.
        quality = float((c.features.get("setup") or {}).get("quality", 1.0))
        size_frac *= (0.5 + 0.5 * quality)
        # P10b SO2: a 'risky' setup TYPE sizes down DETERMINISTICALLY — regardless of the LLM/rule path
        # or the quality score — composing with the other multipliers. Only ever shrinks; the
        # position_size_sol caps + concentration/EV/cohort gates below still bind on top.
        if c.features.get("setup_type", "") == "risky":
            size_frac *= self.s.setup_risky_size_mult
        # N8: holder concentration is the #1 free rug tell but DARK on the public RPC (no
        # getTokenLargestAccounts) — trading blind at full size is exactly when to shrink. Survival is
        # in size/selectivity, not tighter stops. getattr keeps the unbound-stub tests working.
        size_frac = self._dark_conc_size_frac(size_frac, getattr(self, "_conc_dark", False),
                                              self.s.risk.conc_dark_size_mult)
        size = self.risk.position_size_sol(self.portfolio, equity, frac=size_frac,
                                           liquidity_sol=liq_sol, price_map=price_map)
        if size <= 0:
            return
        # A2: LAST-LINE honeypot check right before the (paper) buy — quote-only sell-route test, no
        # real money. Only on actual buy candidates (cheap: a few/min), degrades to no-op on any error.
        if self.jupiter is not None:
            hp = await self.jupiter.honeypot_check(
                c.mint, sol_amount=self.s.honeypot_sol_amount,
                min_roundtrip_keep=self.s.honeypot_min_roundtrip_keep)
            if hp:
                log.warning("HONEYPOT veto %s: %s — skipping buy", c.symbol or c.mint[:6], hp)
                self.alerter.send(f"🍯 HONEYPOT *{c.symbol or c.mint[:6]}* — {hp} (buy vetoed)")
                return
        # B1: RugCheck cross-check — an independent second opinion right before the (paper) buy. Veto on
        # a 'danger' verdict (a risk RugCheck's DB flags that our own gate may have missed). No-op if
        # RugCheck is unreachable (an external service must never block buys on its own failure).
        if self.rugcheck is not None and self.s.rugcheck_veto_on_danger:
            rc = await self.rugcheck.report(c.mint)
            if rc and rc.get("danger"):
                top = ", ".join(rc.get("risks", [])[:3]) or "danger"
                log.warning("RUGCHECK veto %s: %s — skipping buy", c.symbol or c.mint[:6], top)
                self.alerter.send(f"🛑 RUGCHECK *{c.symbol or c.mint[:6]}* danger ({top}) — buy vetoed")
                return
        fill = await self.executor.buy(c.mint, size)
        if not fill.ok:
            log.debug("buy rejected %s: %s", c.symbol, fill.reason)
            return
        if not self.portfolio.apply_buy(
            fill, symbol=c.symbol, mode=mode,
            tp_override=(verdict.tp_pct or None), sl_override=(verdict.sl_pct or None),
            note=verdict.reasoning, features=build_features(c), score=c.score,  # P2: trade_outcomes
            creator=c.creator,                            # N9: cohort key for the correlated-flush cap
            setup_type=c.features.get("setup_type", ""),  # situation class -> setup-specific lessons
            risk_flags=verdict.risk_flags,                # P10 #4: carry the brain's at-entry concerns -> reflection
            verdict_source=verdict.source,                # P10b #1: brain-vs-rule A/B key on the CLEAN book
            verdict_conviction=verdict.conviction,
        ):
            return
        self._price_cache[c.mint] = fill.price_sol
        self._patience["bought"] += 1                     # P8: an actual entry (the selectivity numerator)
        await self.storage.log_trade(
            mint=c.mint, symbol=c.symbol, side=SIDE_BUY, mode=mode, sol=fill.sol,
            tokens=fill.tokens, price_sol=fill.price_sol, fee_sol=fill.fee_sol,
            slippage_pct=fill.slippage_pct, reason=f"{verdict.source} conv={verdict.conviction:.2f}",
        )
        self.alerter.send(
            f"🟢 BUY *{c.symbol or c.mint[:6]}* [{mode}] {size:.3f} SOL @ {fill.price_sol:.8f} "
            f"(conv {verdict.conviction:.2f}, {verdict.source})"
        )

    async def _close_position(self, mint: str, reason: str) -> None:
        """Sell the whole position, book it, log + alert, queue a reflection.
        Guarded so _manage_loop and _reeval_loop can't double-close one mint."""
        assert self.executor is not None
        if mint in self._closing or mint not in self.portfolio.positions:
            return
        self._closing.add(mint)
        try:
            pos = self.portfolio.positions.get(mint)
            if pos is None:
                return
            fill = await self.executor.sell(mint, pos.qty)
            if not fill.ok:
                return
            symbol, mode = pos.symbol, pos.mode
            before = len(self.portfolio.closed)
            pnl = self.portfolio.apply_sell(fill, reason=reason,
                                            max_return_mult=self.s.risk.max_fill_return_mult)
            if pnl is None:                                # a concurrent pass already booked it
                return
            await self.storage.log_trade(
                mint=mint, symbol=symbol, side=SIDE_SELL, mode=mode, sol=fill.sol,
                tokens=fill.tokens, price_sol=fill.price_sol, fee_sol=fill.fee_sol,
                slippage_pct=fill.slippage_pct, reason=reason,
            )
            self._price_cache.pop(mint, None)
            self._price_misses.pop(mint, None)
            if len(self.portfolio.closed) > before:        # fully closed -> learn from it
                ct = self.portfolio.closed[-1]
                # N3: feed the halt the TRADE-LEVEL realized PnL (full round-trip, incl. any earlier
                # partial banked at arm=True), NOT this slice's return — else a net-WINNING scaled-out
                # trade (partial +15% then remainder -5%) trips the bleed/streak halt on a positive book.
                if ct.pnl_sol < 0:
                    self.risk.on_loss(mint, ct.pnl_sol)
                else:
                    self.risk.on_win(ct.pnl_sol)
                await self.storage.log_trade_outcome(ct)   # P2: entry features -> realized PnL
                self._enqueue_reflection(ct)
            emoji = "✅" if pnl >= 0 else "❌"
            self.alerter.send(f"🔴 SELL *{symbol or mint[:6]}* [{mode}] {reason} {emoji} pnl {pnl:+.4f} SOL")
        finally:
            self._closing.discard(mint)

    async def _take_partial(self, mint: str, *, frac: float | None = None,
                            tag: str = "partial_tp", arm: bool = True, latch: str = "partial") -> None:
        """P3/P7: sell a fraction of a position to bank SOL, then keep the remainder. Two modes:
        the clean PARTIAL (frac=None -> partial_tp_frac, arm=True: bank some + protect the rest with
        the tight breakeven-trail), and the P7 DE-RISK (frac=derisk_fraction, arm=False: recover the
        PRINCIPAL so the rest is a house-money runner free to RUN — the wide hold-trail + hard SL +
        liq_collapse still protect it against a rug). One-time per position; guarded by `_closing`."""
        assert self.executor is not None
        if mint in self._closing or mint not in self.portfolio.positions:
            return
        self._closing.add(mint)
        try:
            pos = self.portfolio.positions.get(mint)
            if pos is None or getattr(pos, f"{latch}_taken", False):
                return
            f = self.exit_params.partial_tp_frac if frac is None else frac
            qty = pos.qty * f
            if qty <= 0:
                return
            symbol, mode = pos.symbol, pos.mode
            before = len(self.portfolio.closed)
            fill = await self.executor.sell(mint, qty)
            if not fill.ok:
                return
            pnl = self.portfolio.apply_sell(fill, reason=tag,
                                            max_return_mult=self.s.risk.max_fill_return_mult)
            if pnl is None:
                return
            await self.storage.log_trade(
                mint=mint, symbol=symbol, side=SIDE_SELL, mode=mode, sol=fill.sol,
                tokens=fill.tokens, price_sol=fill.price_sol, fee_sol=fill.fee_sol,
                slippage_pct=fill.slippage_pct, reason=tag,
            )
            remaining = self.portfolio.positions.get(mint)
            if remaining is not None:
                setattr(remaining, f"{latch}_taken", True)   # one-time (partial OR initial latch)
                if arm:
                    remaining.breakeven_armed = True  # clean partial: protect the rest with the breakeven-trail
                label = "PARTIAL" if arm else "DE-RISK"
                tail = "protecting the rest" if arm else "principal recovered, free-rolling the rest"
                self.alerter.send(
                    f"🟡 {label} *{symbol or mint[:6]}* [{mode}] sold "
                    f"{f:.0%}, pnl {pnl:+.4f} SOL ({tail})"
                )
            elif len(self.portfolio.closed) > before:  # partial consumed a dust remainder -> full close
                ct = self.portfolio.closed[-1]
                await self.storage.log_trade_outcome(ct)
                if ct.pnl_sol < 0:                         # N3: trade-level round-trip PnL, not the slice
                    self.risk.on_loss(mint, ct.pnl_sol)
                else:
                    self.risk.on_win(ct.pnl_sol)
                self._price_cache.pop(mint, None)
                self._price_misses.pop(mint, None)
                self._enqueue_reflection(ct)
                emoji = "✅" if pnl >= 0 else "❌"
                self.alerter.send(f"🔴 SELL *{symbol or mint[:6]}* [{mode}] {tag} {emoji} pnl {pnl:+.4f} SOL")
        finally:
            self._closing.discard(mint)

    async def _derisk_if_profitable(self, mint: str, price: float | None, tag: str) -> None:
        """P7: when a HELD position is flagged RISKY (sell-pressure on the tape, or holder
        concentration creeping up) AND it is in profit, bank a PRINCIPAL-RECOVERY partial so the
        remainder rides risk-free — the user's "ersdeltei baiwal orson dvngeer ashig hiigeed vldsen
        hesgiig tsaash ywuulj aldagdalgvi" doctrine. No-op if not yet up >= derisk_tp_pct (let the
        normal partial/SL own it) or already partialled. Selling into profit never creates a loss."""
        pos = self.portfolio.positions.get(mint)
        # P7 fix: gate on the OWN derisk latch, not partial_taken. The old `pos.partial_taken` check
        # meant a clean P3 partial permanently blocked this risk-triggered principal-recovery — so a
        # position that banked a partial and THEN saw sell-pressure / a concentration rise could never
        # recover its principal (the exact survival case this exists for). Own latch -> composes with
        # the partial/initial latches (mirrors take_initial's latch="initial").
        if pos is None or pos.derisk_taken or price is None or price <= 0:
            return
        if pos.pnl_pct(price) < self.exit_params.derisk_tp_pct:
            return
        frac = pos.derisk_fraction(price, sell_cost_pct(self.s.fees), self.exit_params.derisk_max_frac)
        if frac <= 0:
            return
        await self._take_partial(mint, frac=frac, tag=tag, arm=False, latch="derisk")

    async def _manage_loop(self) -> None:
        assert self.executor is not None
        while True:
            await asyncio.sleep(self.s.manage_interval_s)
            for mint, pos in list(self.portfolio.positions.items()):
                try:
                    price = await self.executor.get_price_sol(mint)
                    if price is None or price <= 0:
                        # No fresh price. A dead/dark token must NOT sit valued at its stale
                        # entry price forever (that hides the loss from the kill-switch and
                        # blocks every exit). Force-close after a sustained blackout; until
                        # then keep evaluating the TIME-based exit against the last-known price
                        # so timeouts/stops still fire.
                        self._price_misses[mint] = self._price_misses.get(mint, 0) + 1
                        if self._price_misses[mint] >= self._max_price_misses:
                            await self._close_position(mint, "no_price")
                            continue
                        price = self._price_cache.get(mint) or pos.avg_price_sol
                        if price <= 0:
                            continue
                    else:
                        self._price_misses.pop(mint, None)
                        self._price_cache[mint] = price
                    do_exit, reason = pos.should_exit(price, self.exit_params)
                    if not do_exit and self.s.exit_use_trend:
                        # P3 fade exit: we entered late, so bail when the live trend BREAKS
                        # while we're NOT in a real loss (> -breakeven_floor) — don't wait for
                        # the stop. The hard SL still owns the real downside; we never fade a
                        # fresh dip below the floor (that would just lock the fee loss).
                        st = self.registry.get(mint)
                        if st is not None and pos.pnl_pct(price) > -self.exit_params.breakeven_floor_pct \
                                and self._trend_broke(st.price_trend(), st.technicals(),
                                                      min_polls=self.s.fade_min_polls,
                                                      min_drop_pct=self.s.fade_min_drop_pct):
                            do_exit, reason = True, "fade"
                    if do_exit:
                        await self._close_position(mint, reason)
                    elif pos.should_take_initial(price, self.exit_params):
                        # A3 (the spec's #1 survival rule): at 2x recover the PRINCIPAL (sell enough to
                        # bank the original cost basis), ride the rest as risk-free house money so a later
                        # rug can never zero this winner. Own latch -> composes with the P3 partial; arm=True
                        # protects the free-roll tail with the breakeven-trail.
                        f = pos.derisk_fraction(price, sell_cost_pct(self.s.fees), self.exit_params.derisk_max_frac)
                        if f > 0.0:
                            await self._take_partial(mint, frac=f, tag="take_initial", arm=True, latch="initial")
                    elif pos.should_take_partial(price, self.exit_params):
                        # P3: not a full exit, but up enough to bank a partial and free-roll the rest
                        await self._take_partial(mint)
                except Exception:  # noqa: BLE001
                    log.exception("manage error for %s", mint)

    async def _reeval_loop(self) -> None:
        """R13: periodically re-check OPEN positions' holder health and adapt —
        broadening distribution -> extend to hold; worsening -> cut fast."""
        while True:
            await asyncio.sleep(self.s.reeval_interval_s)
            if self.helius is None:
                continue
            budget = self.s.max_safety_checks_per_cycle
            for mint, pos in list(self.portfolio.positions.items()):
                if budget <= 0:
                    break
                st = self.registry.get(mint)
                if st is None or not st.migrated:          # concentration only meaningful post-migration
                    continue
                budget -= 1
                try:
                    conc = await self.helius.top5_concentration(mint)
                except Exception:  # noqa: BLE001
                    continue
                # W6 concentration-TREND rug-cut: static concentration is uniformly high on fresh
                # memecoins (didn't separate), but a RISE while held = dev/insiders consolidating =
                # distribution prep = rug imminent. Cut when it climbs >= conc_rise_cut_pct above ENTRY.
                entry_conc = float(pos.entry_features.get("top5_concentration_pct") or -1.0)
                if self._concentration_rose(conc, entry_conc, self.s.safety.conc_rise_cut_pct):
                    log.info("concentration ROSE on %s (%.0f%% -> %.0f%%, +%.0fpp) -> rug-prep cut",
                             pos.symbol or mint[:6], entry_conc, conc, conc - entry_conc)
                    await self._close_position(mint, "conc_rise")
                    continue
                action = reeval_action(conc, self.s.holder_cut_pct, self.s.holder_good_pct)
                if action == "cut":
                    log.info("holders worsened on %s (conc=%.0f%%) -> cutting", pos.symbol or mint[:6], conc or -1)
                    await self._close_position(mint, "holders_cut")
                elif action == "extend" and pos.mode == MODE_SCALP:
                    pos.mode = MODE_HOLD
                    log.info("holders broadened on %s (conc=%.0f%%) -> extended to hold",
                             pos.symbol or mint[:6], conc or -1)
                elif conc is not None and conc >= self.s.safety.concentration_warn_pct:
                    # P7: holders RISKY (warn-zone — concentrated but not yet the extreme cut line).
                    # Rather than ride a whale-heavy book full-size, bank the principal and free-roll
                    # the rest IF we're in profit (the user's "tom holder = ersdeltei -> orson dvngee
                    # gargana" doctrine). No-op when not in profit; the conc_rise/cut paths own the
                    # hard exits. Uses the manage-loop's cached price (no extra fetch).
                    await self._derisk_if_profitable(mint, self._price_cache.get(mint), "derisk_conc")

    def _enqueue_reflection(self, ct) -> None:
        if not (self.brain.llm_enabled and self.s.reflect_enabled):
            return
        try:
            self._reflect_q.put_nowait(ct)
        except asyncio.QueueFull:
            pass

    async def _reflect_loop(self) -> None:
        """Off the hot path: turn each closed trade into a lesson in memory."""
        while True:
            ct = await self._reflect_q.get()
            try:
                await self.brain.reflect(ct)
            except Exception:  # noqa: BLE001
                log.exception("reflect error")
            finally:
                self._reflect_q.task_done()

    async def _meta_reflect_loop(self) -> None:
        """AI3: periodically distill AGGREGATE recent performance into a strategy-level
        lesson (advisory; recalled in future decisions). Never edits config."""
        while True:
            await asyncio.sleep(self.s.meta_reflect_interval_s)
            recent = self.portfolio.closed[-30:]
            if len(recent) < 5:                  # need a few trades to see a pattern
                continue
            try:
                # P10b #1/#3: close the regret loop — feed the recent missed-winner profile (incl. the
                # winner/fade/flat/glitch mix) into meta-reflection so it can judge whether selectivity
                # is over-strict, anchored to the true base rate (most misses are fades, not edge).
                traits = await self.storage.winners_traits()
                if traits.get("n", 0):
                    traits["verdict_mix"] = await self.storage.miss_verdict_counts()
                await self.brain.meta_reflect(compute_stats(recent), recent, miss_traits=traits)
            except Exception:  # noqa: BLE001
                log.exception("meta-reflect error")

    # ── P8 miss-learning (regret) + patience ─────────────────────────────────────
    async def _miss_learn_loop(self) -> None:
        """P8 REGRET loop — the complement to _reflect_loop (which learns from LOSSES we took).
        Periodically replays recently-OBSERVED-but-NOT-bought mints, re-prices them via DexScreener,
        and when a skipped token's forward return cleared the winner bar it records a 'missed winner'
        and (optionally) queues a brain reflection asking which filter wrongly rejected it. One
        DexScreener batch + at most miss_reflect_max_per_pass LLM calls per pass; fully off the hot
        path; self-degrades to a no-op (DexScreener down) or record-only (no LLM)."""
        while True:
            await asyncio.sleep(self.s.miss_learn_interval_s)
            try:
                await self._miss_learn_once()
            except Exception:  # noqa: BLE001
                log.exception("miss-learn cycle error")

    async def _miss_learn_once(self) -> None:
        now = time.time()
        cands = await self.storage.miss_candidates(
            min_age_s=self.s.miss_min_age_s, max_age_s=self.s.miss_max_age_s,
            limit=self.s.miss_batch_size, now=now,
        )
        if not cands:
            return
        mints = list({c["mint"] for c in cands})           # de-dup for ONE batch fetch (<=30 = 1 request)
        snaps, covered = await self.dex.snapshots(mints)
        bar = 1.0 + self.s.miss_win_threshold_pct / 100.0
        reflected = 0
        # seed `seen` with mints already recorded so a token re-judged across passes records ONE row
        # (token-level winners_traits, not per-observation) — still judges each new obs_id below.
        seen: set[str] = await self.storage.missed_winner_mints()
        for c in cands:
            mint = c["mint"]
            if mint not in covered:
                continue                                    # fetch didn't cover it -> NOT judged; retry next pass
            snap = snaps.get(mint)
            entry_price = float(c["price_usd"] or 0.0)
            if snap is None or snap.price_usd <= 0 or entry_price <= 0:
                # covered but unpriced now = the token is dead/rugged -> the skip was CORRECT (a free
                # true-negative). Mark judged so we never re-fetch it.
                await self.storage.mark_miss_judged(c["obs_id"], "flat")
                continue
            fwd_ret = snap.price_usd / entry_price - 1.0
            # P10 #9: a >=20x forward move is the same DexScreener/curve SOL-scale pricing glitch the
            # honest book excludes (risk.max_fill_return_mult) — a phantom "missed winner" that would
            # poison the regret base rate. Mark it a glitch and skip recording/reflecting.
            if self.s.risk.max_fill_return_mult > 0 and fwd_ret + 1.0 >= self.s.risk.max_fill_return_mult:
                await self.storage.mark_miss_judged(c["obs_id"], "glitch")
                continue
            cleared = snap.price_usd >= entry_price * bar
            # P10 #12: require the move to be SUSTAINED, not a single-point spike already rolling over.
            # A token past the bar but with m5 momentum already negative is a pump-and-give-back FADE,
            # not a missed winner — recording it teaches the brain to chase wash spikes, the opposite
            # of survival-first selectivity. Count fades (don't silently drop them as 'flat').
            won = cleared and snap.price_change_m5 >= 0.0
            verdict = "winner" if won else ("fade" if cleared else "flat")
            await self.storage.mark_miss_judged(c["obs_id"], verdict)
            if not won or mint in seen:
                continue
            seen.add(mint)
            row_id = await self.storage.log_missed_winner(
                obs_id=c["obs_id"], mint=mint, symbol=c["symbol"] or snap.symbol,
                mode=c["mode"], entry_ts=c["ts"], entry_price=entry_price,
                detect_price=snap.price_usd, fwd_return=fwd_ret,
                horizon_s=now - c["ts"], rule_passed=c["rule_passed"],
                score=c["score"] or 0.0, features_json=c["features"] or "{}",
            )
            if row_id is None:
                continue                                    # already recorded (prior pass / race)
            log.info("MISSED WINNER %s +%.0f%% (rule_passed=%s, score=%.2f) — learning from the regret",
                     c["symbol"] or mint[:6], fwd_ret * 100, bool(c["rule_passed"]), c["score"] or 0.0)
            self.alerter.send(
                f"🟣 MISS *{c['symbol'] or mint[:6]}* skipped, then +{fwd_ret * 100:.0f}% "
                f"(gate {'passed' if c['rule_passed'] else 'failed'})"
            )
            if reflected < self.s.miss_reflect_max_per_pass and self._enqueue_miss_reflection(c, fwd_ret):
                reflected += 1

    async def _track_loop(self) -> None:
        """N1 — forward-tracking (label completion). The eval loop logs an indexed mint only while it
        sits in the active top-N; under a flood of new tokens an indexed mint is pushed out (median
        track span ~511s) BEFORE the 5m label horizon, so its forward path never reaches the horizon
        and it is never labelable. This loop re-prices observed-but-not-bought INDEXED mints straight
        from the DB — independent of registry membership — until track_window_s, completing the label.
        The binding constraint on the GBM/separation/AUC is labelable-mint COUNT; this lifts it. One
        DexScreener batch/pass, fully off the hot path; self-degrades to a no-op (no candidates / no
        DexScreener). Touches NO safety gate, risk layer, or buy path."""
        while True:
            await asyncio.sleep(self.s.track_interval_s)
            try:
                await self._track_once()
            except Exception:  # noqa: BLE001
                log.exception("forward-tracking cycle error")

    async def _track_once(self) -> None:
        now = time.time()
        cands = await self.storage.tracking_candidates(
            window_s=self.s.track_window_s, revisit_s=self.s.track_revisit_s,
            limit=self.s.track_batch_size, now=now,
        )
        if not cands:
            return
        mints = [c["mint"] for c in cands]                  # already distinct (GROUP BY mint)
        snaps, covered = await self.dex.snapshots(mints)
        logged = 0
        for cand in cands:
            mint = cand["mint"]
            snap = snaps.get(mint)
            if mint not in covered or snap is None or snap.price_usd <= 0 or snap.liquidity_usd <= 0:
                continue        # dead / unpriced / de-indexed now -> log nothing (never fabricate a point)
            tc = Candidate(mint=mint, symbol=cand["symbol"] or snap.symbol)
            tc.ts = now
            tc.mode = cand["mode"] or MODE_HOLD
            tc.price_usd = snap.price_usd
            tc.price_sol = snap.price_native
            tc.liquidity_usd = snap.liquidity_usd
            tc.market_cap_usd = snap.market_cap
            tc.features["vol_h1"] = snap.volume_h1
            # N1: a tracking row contributes ONLY its later PRICE to the forward label — the labelers
            # use each mint's EARLIEST obs for features, so this is never an entry/training row (it is
            # always later than the mint's first eval obs). A NON-empty features json is required: the
            # labelers drop rows where features == '{}', and indexed_only reads liquidity_usd from it.
            feats_json = json.dumps({
                "liquidity_usd": snap.liquidity_usd,
                "market_cap_usd": snap.market_cap,
                "vol_h1": snap.volume_h1,
            })
            await self.storage.log_observation(tc, feats_json)
            logged += 1
        if logged:
            log.debug("forward-tracking | re-priced %d/%d indexed mints (completing labels)", logged, len(cands))

    async def _funder_loop(self) -> None:
        """N12 — funder clustering. Off the hot path, resolves the funding wallet of recently-observed
        creators (Helius getSignaturesForAddress + getTransaction), caches + persists them, and lets
        the cluster graph (rotated creators sharing one treasury) accrue. Bounded per pass; self-
        degrades to a no-op without an RPC or when every recent creator is already resolved."""
        while True:
            await asyncio.sleep(self.s.funder_interval_s)
            try:
                await self._funder_once()
            except Exception:  # noqa: BLE001
                log.exception("funder-clustering cycle error")

    async def _funder_once(self) -> None:
        if self.helius is None:
            return
        creators = await asyncio.to_thread(self.storage.unresolved_creators, self.s.funder_batch_size)
        if not creators:
            return
        resolved: list[tuple] = []
        for creator in creators:
            funder = await self.helius.get_funder(creator)
            if funder:
                self.funder_history.record(creator, funder)
                resolved.append((creator, funder))
        if resolved:
            await asyncio.to_thread(self.storage.save_creator_funders, resolved)
            log.debug("funder-clustering | resolved %d/%d creators (%d distinct funders tracked)",
                      len(resolved), len(creators), len({f for _, f in self.funder_history.snapshot()}))

    async def _readiness_loop(self) -> None:
        """Periodically snapshot the go-live GATE (CLEAN-book net/pf/win/n + labelable count) to the
        durable readiness_log, so the trajectory is queryable. Cheap (no GBM), off the hot path."""
        while True:
            await asyncio.sleep(self.s.readiness_interval_s)
            try:
                book = (await asyncio.to_thread(self._honest_book) or {}).get("clean") or {}
                stats = await self.storage.tracking_stats(horizon_s=self.s.calibrate_horizon_s)
                labelable = int(stats.get("labelable", 0))
                await asyncio.to_thread(self.storage.save_readiness, book, labelable)
                pf = book.get("profit_factor", 0.0)
                log.info("readiness | net %+.3f SOL | win %.0f%% | pf %s | %d closed | labelable %d "
                         "(go-live gate: %s)",
                         book.get("net", 0.0), book.get("win_rate", 0.0) * 100,
                         "inf" if pf == float("inf") else f"{pf:.2f}", int(book.get("n", 0)), labelable,
                         "PASS" if (int(book.get("n", 0)) >= self.s.go_live_min_trades
                                    and book.get("net", 0.0) > 0
                                    and pf >= self.s.go_live_min_profit_factor) else "NO-GO")
            except Exception:  # noqa: BLE001
                log.exception("readiness cycle error")

    def _miss_reflect_ready(self) -> bool:
        return bool(self.brain.llm_enabled and self.s.reflect_enabled and self.s.miss_reflect_enabled)

    def _enqueue_miss_reflection(self, cand: dict, fwd_ret: float) -> bool:
        if not self._miss_reflect_ready():
            return False
        try:
            self._miss_q.put_nowait((cand, fwd_ret))
            return True
        except asyncio.QueueFull:
            return False

    async def _miss_reflect_loop(self) -> None:
        """Off the hot path: turn each missed winner into a 'why did we skip this?' lesson."""
        while True:
            cand, fwd_ret = await self._miss_q.get()
            try:
                await self.brain.reflect_miss(cand, fwd_ret)
            except Exception:  # noqa: BLE001
                log.exception("miss-reflect error")
            finally:
                self._miss_q.task_done()

    def _patience_line(self) -> str:
        """P8: a measured-patience line for the report. skip_rate near 100% is EXPECTED + good
        (Default-to-SKIP); the value is its TREND paired with winners_traits' gate_failed_frac."""
        p = self._patience
        seen = max(1, p["observed"])
        skip_rate = 1.0 - p["bought"] / seen
        return (f"patience | observed={p['observed']} ranked={p['ranked']} bought={p['bought']} | "
                f"skip_rate={skip_rate * 100:.1f}%")

    def _build_calibration(self):
        """Sync (runs in a worker thread): classify our OWN observations into outcomes, derive the
        threshold suggestions + per-creator reputation. Returns (summary, suggestions, rep, ready) or
        None when there's too little data. Reads via a separate sqlite connection (WAL-safe)."""
        rows = _load_all_obs(self.s.db_path)
        if not rows:
            return None
        recs = build_dataset(rows, self.s.calibrate_horizon_s, self.s.calibrate_tol_s)
        if len(recs) < self.s.calibrate_min_mints:
            return None
        summary = summarize(recs)
        rep = creator_reputation(recs, self.storage.mint_creators(),
                                 min_tokens=self.s.safety.creator_rep_min_tokens,
                                 now_ts=time.time(), half_life_s=self.s.safety.creator_rep_half_life_s)  # P10b CAL3
        return summary, suggest_thresholds(summary), rep, readiness(summary), separation_report(recs)

    async def _calibrate_loop(self) -> None:
        """P8 self-improvement: periodically (off the hot path) classify our own outcomes, learn what
        SEPARATES winners from rugs + each creator's track record, and surface it. ADVISORY only —
        suggestions are logged + stored as a recalled lesson; they NEVER auto-edit the live risk
        config (deterministic risk must not be silently re-tuned by a model)."""
        while True:
            await asyncio.sleep(self.s.calibrate_interval_s)
            try:
                data = await asyncio.to_thread(self._build_calibration)
                if data is None:
                    continue
                summary, suggestions, rep, ready, sep = data
                self._creator_rep = rep                         # feed the brain (advisory)
                # P8 RIGOROUS separation: the proper single-feature AUC verdict on winner-vs-rug —
                # the honest "does any signal separate?" read that watches the velocity/liq-trend
                # features cross into SEPARATES as forward data accrues (replaces eyeballing medians).
                if sep.get("ready") and sep.get("features"):
                    top = sep["features"][0]
                    sv = sep_verdict(top["strength"], top["nw"], top["nr"])
                    log.info("separation | %d winners / %d rugs | best=%s AUC=%.2f (%s)",
                             sep["winners"], sep["rugs"], top["feature"], top["auc"], sv)
                    # P10 #6: when the RIGOROUS AUC actually SEPARATES, store it as a recalled lesson —
                    # the strong distribution-free finding was logged-then-dropped while only the weak
                    # median-suggest reached the brain. Dedup on the feature so it isn't re-stored
                    # hourly. Advisory only; never edits config. (Honest null result -> store nothing.)
                    if sv == "SEPARATES" and top["feature"] != self._last_sep_lesson and self.brain.llm_enabled:
                        await self.memory.store(
                            f"From your own classified outcomes: {top['feature']} SEPARATES winners "
                            f"from rugs (AUC {top['auc']:.2f}, n {sep['winners']}/{sep['rugs']}) — weigh it.",
                            "global", 0.6)
                        self._last_sep_lesson = top["feature"]
                cls = summary.get("classes", {})
                # P10 #8: cache the bot's OWN leakage-free outcome base rate so the brain can anchor its
                # prior to the measured rug-dominated reality instead of treating each token as a coin flip.
                self._outcome_base = {
                    "n": summary.get("n", 0),
                    "rug": cls.get("rug", {}).get("frac", 0.0),
                    "fade": cls.get("fade", {}).get("frac", 0.0),
                    "winner": cls.get("winner", {}).get("frac", 0.0),
                }
                log.info("calibrate | %d mints | winners=%d rugs=%d losers=%d flat=%d | creators_rated=%d | "
                         "data_ready=%s", summary.get("n", 0), cls.get("winner", {}).get("n", 0),
                         cls.get("rug", {}).get("n", 0), cls.get("loser", {}).get("n", 0),
                         cls.get("flat", {}).get("n", 0), len(rep), ready["ready"])
                for sug in suggestions[:5]:
                    log.info("  calibrate-suggest: %s", sug["note"])
                if not suggestions:
                    log.info("  calibrate: no feature cleanly separates winners from rugs yet (entry "
                             "signals overlap) — keep accumulating the new P7/P8 signals")
                # P10b CAL1: append this cycle's advisory findings to a queryable audit table (median
                # suggestions + the rigorous AUC), so calibration is reviewable OVER TIME, not just one
                # transient log line + one deduped lesson. Advisory audit only — never auto-applied.
                win_n = cls.get("winner", {}).get("n", 0)
                rug_n = cls.get("rug", {}).get("n", 0)
                cal_rows = [("median", s["feature"], s["winner"], s["rug"], s["separation"],
                             win_n, rug_n, "separates") for s in suggestions]
                if sep.get("ready"):
                    cal_rows += [("auc", f["feature"], f.get("win_median", 0.0), f.get("rug_median", 0.0),
                                  f.get("auc", 0.0), f.get("nw", 0), f.get("nr", 0),
                                  sep_verdict(f.get("strength", 0.0), f.get("nw", 0), f.get("nr", 0)))
                                 for f in sep.get("features", [])]
                if cal_rows:
                    await self.storage.log_calibration_suggestions(cal_rows)
                # store the STRONGEST suggestion as an advisory lesson the brain recalls — deduped on
                # the FEATURE (stable), not the note (which embeds live medians that drift hourly), so a
                # stable finding isn't re-stored every cycle (lesson-spam guard). Never edits config.
                top_feat = suggestions[0]["feature"] if suggestions else None
                if top_feat and top_feat != self._last_calib_lesson and self.brain.llm_enabled:
                    await self.memory.store("From your own classified outcomes: " + suggestions[0]["note"],
                                            "global", 0.6)
                    self._last_calib_lesson = top_feat
            except Exception:  # noqa: BLE001
                log.exception("calibrate cycle error")

    async def _archive_loop(self) -> None:
        """Periodically snapshot the SQLite tables to Parquet (off the event loop).

        P9 NOTE (observations retention, deliberately NOT pruned): the observations table grows
        unbounded over a multi-day run, but a retention prune is UNSAFE with the current archive
        design and is intentionally deferred. (1) The offline GBM/separation/calibration pipeline
        reads the FULL observation price history from SQLite to compute forward-return labels, and
        that dataset is the project's binding constraint (data-starved) — deleting old rows starves
        it. (2) ParquetArchiver.export() is a snapshot OVERWRITE, so it cannot durably preserve
        pruned rows: the next archive would rewrite the parquet from the already-pruned table and
        lose them for good. A safe retention requires append/date-partitioned parquet first (so
        history survives the prune); idx_obs_ts is already in place for when that lands. Until then
        SQLite holds the full set (millions of rows is well within its comfort zone)."""
        while True:
            await asyncio.sleep(self.s.archive_interval_s)
            try:
                res = await asyncio.to_thread(self.archiver.export)
                if res:
                    log.info("parquet archive: %s", res)
            except Exception:  # noqa: BLE001
                log.exception("archive error")

    async def _report_loop(self) -> None:
        while True:
            await asyncio.sleep(self.s.report_interval_s)
            try:
                price_map = self._price_map()
                eq = self.portfolio.equity(price_map)
                stats = compute_stats(self.portfolio.closed)
                await self.storage.log_equity(
                    equity_sol=eq, sol_balance=self.portfolio.sol_balance,
                    realized_pnl=self.portfolio.realized_pnl,
                    open_positions=self.portfolio.open_count(),
                )
                log.info(
                    "equity=%.4f SOL | bal=%.4f | open=%d | tracked=%d | %s",
                    eq, self.portfolio.sol_balance, self.portfolio.open_count(),
                    len(self.registry), stats.as_line(),
                )
                # P8: measured patience + what we MISSED (the regret base rate). gate_failed_frac
                # high => the hard gates wrongly reject winners; low => the entry/brain is too strict.
                traits = await self.storage.winners_traits()
                log.info(
                    "%s | missed_winners n=%d median +%.0f%% gate_failed=%.0f%%",
                    self._patience_line(), traits.get("n", 0), traits.get("median_fwd_pct", 0.0),
                    traits.get("gate_failed_frac", 0.0) * 100.0,
                )
                # P8 feed-liveness watchdog: a free-tier lull STILL has constant new-token events, so
                # a long silence means a dead/stalled WS (not a quiet market) — warn loudly + alert.
                if self._feed_stale(self._last_event_ts, time.time(), self.s.feed_stale_warn_s):
                    stale_s = time.time() - self._last_event_ts
                    log.warning("FEED STALE: no PumpPortal events for %.0fs (>%.0fs) — the WS may be "
                                "dead/stalled, not just a quiet market", stale_s, self.s.feed_stale_warn_s)
                    self.alerter.send(f"⚠️ feed stale: no events for {stale_s:.0f}s — check the WS")
                # P8: the HONEST realized win-rate (CLEAN, glitch-excluded, from the DURABLE trades
                # table — survives restarts; a PEPTI-style pricing glitch never inflates it). THIS is
                # the number to watch for the go-live decision, not the in-memory equity.
                cb = (await asyncio.to_thread(self._honest_book)).get("clean", {})
                if cb.get("n", 0) > 0:
                    pf = "inf" if cb["profit_factor"] == float("inf") else f"{cb['profit_factor']:.2f}"
                    log.info("honest book | CLEAN net %+.3f SOL | win %.0f%% (%dW/%dL) | pf %s",
                             cb["net"], cb["win_rate"] * 100, cb["wins"], cb["losses"], pf)
                # G3: make the metered trade stream VISIBLE — tokens watched + tick events ingested.
                if self.watchlist.enabled:
                    log.info("G3 trade stream | watching %d tokens | %d trade events ingested so far",
                             len(self.feed.watched), self._trade_events)
                    # dead-stream watchdog: G3 is metered + opt-in, so a subscribed-but-silent stream is
                    # a real problem — it delivers NO tape signal (bundle/smart-money stay dark) and may
                    # be spending SOL for nothing. Warn ONCE after several silent cycles with a likely cause.
                    if self._trade_events == 0 and len(self.feed.watched) > 0:
                        self._g3_zero_reports += 1
                        if self._g3_zero_reports == 5:
                            log.warning("G3 ENABLED + subscribed to %d tokens but 0 trade events ingested — the "
                                        "metered stream is delivering NOTHING (the watched cohort has likely "
                                        "GRADUATED off the pump.fun curve that subscribeTokenTrade streams, or "
                                        "the api-key/subscription is being rejected). The G3 tape signals "
                                        "(bundle/smart-money/creator-dump) will stay DARK; set "
                                        "TRADE_STREAM_ENABLED=false until fixed to avoid any wasted SOL.",
                                        len(self.feed.watched))
                    else:
                        self._g3_zero_reports = 0          # events are flowing -> reset the watchdog
                    # P8 copy-trade: persist the cross-restart wallet PnL ledger + surface its size.
                    snap = self.smart_money.snapshot()
                    if snap:
                        await asyncio.to_thread(self.storage.save_wallets, snap)
                        log.info("smart-money | %d wallets tracked | %d profitable (copy-trade signal)",
                                 len(snap), self.smart_money.smart_count())
            except Exception:  # noqa: BLE001
                log.exception("report error")

    def _honest_book(self) -> dict:
        """P8: the HONEST realized book (CLEAN, glitch-excluded) reconstructed from the durable trades
        table — the win-rate to watch for the go-live decision. Sync (runs in a worker thread)."""
        from .backtest.pnl_reconstruct import _load_trades, reconstruct, summarize
        return summarize(reconstruct(_load_trades(self.s.db_path),
                                     glitch_mult=self.s.risk.max_fill_return_mult))

    @staticmethod
    def _feed_stale(last_event_ts: float, now: float, threshold: float) -> bool:
        """P8: True when the discovery feed has been silent longer than `threshold` (a dead WS, since
        pump.fun launches are constant). threshold <= 0 disables the watchdog."""
        return threshold > 0 and (now - last_event_ts) > threshold

    # ── lifecycle ──────────────────────────────────────────────────────────────
    async def run(self) -> None:
        setup_logging(self.s.log_level)
        if self.s.is_live:
            raise SystemExit("MODE=live is not implemented yet (Phase 5). Use MODE=paper.")
        # P8 single-instance guard: a second bot on the same DB silently double-writes the dataset.
        if self.s.db_path and self.s.db_path != ":memory:":
            self._instance_lock = InstanceLock(self.s.db_path + ".lock")
            try:
                self._instance_lock.acquire()
            except SingleInstanceError as e:
                self._instance_lock = None
                raise SystemExit(str(e))
        self.storage.connect()
        self.creator_history.load(self.storage.creator_launch_counts())   # seed the per-creator launch tally
        self.metadata_history.load(self.storage.name_symbol_counts())     # seed the branding-reuse tally
        self.smart_money.load(self.storage.load_wallets())                # seed cross-restart wallet reputation
        self.funder_history.load(self.storage.load_creator_funders())     # N12: seed the funder cluster graph
        await self.memory.load()
        log.info(
            "memebot starting | mode=%s | initial=%.3f SOL | brain=%s | scorer=%s | helius=%s | telegram=%s",
            self.s.mode, self.s.risk.initial_sol,
            (self.llm.name if self.llm is not None else "rule-fallback"),
            self.scorer.kind, bool(self.helius), self.alerter.enabled,
        )
        for warn in self.s.validate():
            log.warning("config: %s", warn)
        sar = self.risk.sol_at_risk(self.s.risk.initial_sol)   # D5: make the survival bound visible
        log.info(
            "risk posture | per-trade <= %.3f SOL (%.1f%% equity) | whole-book rug <= %.3f SOL "
            "(%.1f%%) | daily cap %.2f SOL | book_within_cap=%s",
            sar["per_trade_sol"], sar["per_trade_pct"] * 100, sar["max_book_rug_sol"],
            sar["max_book_rug_pct"] * 100, sar["daily_loss_cap_sol"], sar["book_within_daily_cap"],
        )
        if self.shadow_scorer is not None:
            log.info("P4 shadow mode ON: %s scores into observations.shadow_score (does NOT size trades)",
                     self.s.gbm_shadow_model_path)
        log.info("W2 trading lane | scalp_buys=%s | max_positions=%d | exposure_frac=%.2f | "
                 "liq_collapse<%.0f%% of entry | blackout=%d polls",
                 self.s.trade_scalp, self.s.risk.max_positions, self.s.risk.max_total_exposure_frac,
                 self.exit_params.liq_collapse_frac * 100, self.s.max_price_misses)

        async with AsyncExitStack() as stack:
            dex = await stack.enter_async_context(self.dex)
            if self.jupiter is not None:
                self.jupiter = await stack.enter_async_context(self.jupiter)   # A2 honeypot quote client
            if self.rugcheck is not None:
                self.rugcheck = await stack.enter_async_context(self.rugcheck)  # B1 RugCheck cross-check
            if self.helius is not None:
                self.helius = await stack.enter_async_context(self.helius)
                # W3: honestly report whether holder-concentration safety is actually LIVE. On the
                # public Solana RPC getTokenLargestAccounts is unsupported -> top5_concentration is
                # always unknown, so the R2-b scam penalty / holder-quality / R13 re-eval are all
                # DARK. Measured: the rug cohort that drives every catastrophic loss enters with
                # concentration UNKNOWN, so this is the #1 free win-rate lever.
                if self.helius.supports_largest_accounts():
                    log.info("holder-concentration safety ENABLED (enhanced RPC serves getTokenLargestAccounts)")
                else:
                    self._conc_dark = True           # N8: concentration dark -> shrink per-trade size
                    log.warning("holder-concentration safety DISABLED: this RPC does not serve "
                                "getTokenLargestAccounts (the public Solana RPC doesn't). It is the "
                                "#1 free scam/rug-avoidance tell — set HELIUS_RPC_URL to a FREE "
                                "Helius key to activate it.")
            else:
                self._conc_dark = True               # N8: no RPC at all -> concentration dark too
            if self._conc_dark:
                log.warning("N8: holder concentration DARK -> per-trade size shrunk to %.0f%% "
                            "(survival-first; the free fix is a Helius RPC)", self.s.risk.conc_dark_size_mult * 100)
            self.prices = PriceSource(self.registry, dex)
            self.executor = PaperBackend(self.prices, self.s.fees, self.s.risk.max_modeled_slippage_pct)

            tasks = [
                asyncio.create_task(self.feed.run(), name="feed"),
                asyncio.create_task(self._ingest_loop(), name="ingest"),
                asyncio.create_task(self._eval_loop(), name="eval"),
                asyncio.create_task(self._manage_loop(), name="manage"),
                asyncio.create_task(self._report_loop(), name="report"),
            ]
            if self.alerter.enabled:
                # P9 #1: gate the alerter task — a DISABLED alerter.run() returns immediately
                # (alerts go to console), which would trip the FIRST_COMPLETED teardown below on
                # every startup. When disabled the task did nothing anyway, so this is behaviour-neutral.
                tasks.append(asyncio.create_task(self.alerter.run(), name="alerter"))
            if self.brain.llm_enabled and self.s.reflect_enabled:
                tasks.append(asyncio.create_task(self._reflect_loop(), name="reflect"))
                if self.s.meta_reflect_interval_s > 0:
                    tasks.append(asyncio.create_task(self._meta_reflect_loop(), name="meta-reflect"))
            if self.s.miss_learn_enabled and self.s.miss_learn_interval_s > 0:
                # P8: regret loop (records misses even with no LLM); the reflector only when ready.
                tasks.append(asyncio.create_task(self._miss_learn_loop(), name="miss-learn"))
                if self._miss_reflect_ready():
                    tasks.append(asyncio.create_task(self._miss_reflect_loop(), name="miss-reflect"))
            if self.s.track_enabled and self.s.track_interval_s > 0:
                # N1: forward-tracking — re-price indexed mints past the active window so labels form.
                tasks.append(asyncio.create_task(self._track_loop(), name="track"))
            if self.s.funder_clustering_enabled and self.s.funder_interval_s > 0 and self.helius is not None:
                # N12: resolve creator funders off the hot path -> re-link rotated creators by treasury.
                tasks.append(asyncio.create_task(self._funder_loop(), name="funder"))
            if self.s.readiness_interval_s > 0:
                # snapshot the go-live gate trajectory (durable readiness_log) — cheap, off the hot path.
                tasks.append(asyncio.create_task(self._readiness_loop(), name="readiness"))
            if self.s.calibrate_enabled and self.s.calibrate_interval_s > 0:
                # P8: self-calibration (learn what separates winners from rugs + creator reputation).
                tasks.append(asyncio.create_task(self._calibrate_loop(), name="calibrate"))
            if self.commands.enabled:
                tasks.append(asyncio.create_task(self.commands.run(), name="commands"))
            if self.s.archive_interval_s > 0 and self.archiver.available():
                tasks.append(asyncio.create_task(self._archive_loop(), name="archive"))
            watch_task = None
            if self.watchlist.enabled:
                # P9 #1: tracked separately — watchlist.run() LEGITIMATELY returns when the gated G3
                # trade stream is unavailable, so it must be excluded from the FIRST_COMPLETED set
                # (its normal completion is not a bot failure and must not trigger a restart).
                watch_task = asyncio.create_task(self.watchlist.run(), name="watchlist")
                tasks.append(watch_task)
            if self.helius is not None:
                tasks.append(asyncio.create_task(self._reeval_loop(), name="reeval"))
            try:
                # P9 #1: wait on the FIRST task to finish/fail, NOT gather(return_exceptions=True).
                # Every sentinel here is an infinite loop, so ANY of them completing is abnormal (a
                # dead pipeline stage). gather(return_exceptions=True) would capture that task's
                # exception but keep blocking forever on the surviving infinite loops — leaving the
                # bot silently half-dead AND invisible to memebot.supervisor, which only restarts on
                # a process exit. Instead: detect the first completion, then raise SystemExit(1) so
                # the process exits NONZERO and the supervisor relaunches it. (watchlist is excluded —
                # it legitimately returns when the gated G3 stream is unavailable.)
                sentinels = [t for t in tasks if t is not watch_task]
                done, _pending = await asyncio.wait(sentinels, return_when=asyncio.FIRST_COMPLETED)
                t = next(iter(done))
                exc = t.exception() if not t.cancelled() else None
                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    log.error("task %s crashed: %r -> exiting nonzero for supervisor restart", t.get_name(), exc)
                else:
                    log.error("task %s exited unexpectedly -> exiting nonzero for supervisor restart", t.get_name())
                raise SystemExit(1)
            except asyncio.CancelledError:
                pass
            finally:
                for t in tasks:
                    t.cancel()
                # Drive cancellation to completion so in-flight DB writes / sockets
                # unwind cleanly BEFORE we close storage.
                await asyncio.gather(*tasks, return_exceptions=True)
                if self.llm is not None and hasattr(self.llm, "aclose"):
                    await self.llm.aclose()
                self._shutdown_report()
                self.storage.close()
                if self._instance_lock is not None:
                    self._instance_lock.release()

    def _shutdown_report(self) -> None:
        stats = compute_stats(self.portfolio.closed)
        log.info("── shutdown ── final bal=%.4f SOL realized=%.4f | %s",
                 self.portfolio.sol_balance, self.portfolio.realized_pnl, stats.as_line())


def main() -> None:
    settings = get_settings()
    bot = Bot(settings)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\nbye 👋")


if __name__ == "__main__":
    main()
