"""TradingBrain — the autonomous decision + reflection loop.

decide(candidate) -> Verdict:
  below entry floor          -> skip (no LLM)
  uncertain band + budget    -> Claude (Haiku; re-ask Sonnet on a hard buy)
  slam-dunk / LLM-unavailable -> rule-based Verdict (so the loop never blocks)

reflect(closed_trade) -> writes a distilled lesson to memory (off the hot path).

The Verdict is ADVISORY. main passes it through the deterministic risk layer,
which decides and can veto.
"""
from __future__ import annotations

from ..config import Settings
from ..filter.rules import holder_quality, scam_likelihood
from ..models import Candidate
from ..portfolio.portfolio import ClosedTrade
from ..utils.logging import get_logger
from .escalation import Escalator
from .memory import AgentMemory
from .policy import CONSTITUTION
from .schema import ReflectionNote, Verdict, _clamp, is_actionable_lesson, setup_tag

log = get_logger("agent.brain")

# A Haiku "buy" with conviction below this is a hard call -> get Sonnet's opinion.
_HARD_BUY_CONVICTION = 0.55


def _exit_token(reason: str) -> str:
    """P10b LM5: group a raw exit reason into a recall-able exit-condition token, so a lesson can be
    tagged by HOW the trade ended and surfaced for a live candidate showing that condition."""
    r = (reason or "").lower()
    if "trail" in r:
        return "trail"
    if "timeout" in r or "stall" in r or "no_price" in r:
        return "timeout"
    if "breakeven" in r or r == "be":
        return "breakeven"
    if "fade" in r:
        return "fade"
    return ""


class TradingBrain:
    def __init__(self, settings: Settings, memory: AgentMemory, llm=None,
                 entry_threshold: float | None = None,
                 high_conf_threshold: float | None = None) -> None:
        self.s = settings
        self.memory = memory
        self.llm = llm
        # effective thresholds — the GBM probability scale differs from the rescaled rule score,
        # so main resolves these by scorer kind and passes them in.
        self.entry = entry_threshold if entry_threshold is not None else settings.entry_threshold
        self.high_conf = high_conf_threshold if high_conf_threshold is not None else settings.high_conf_threshold
        self.escalator = Escalator(settings.llm_per_min, self.high_conf, self.entry)
        if self.high_conf <= self.entry:
            log.warning(
                "high_conf threshold (%.2f) <= entry threshold (%.2f): escalation band is empty; "
                "the LLM brain will never be consulted (rule-fallback only)",
                self.high_conf, self.entry,
            )

    @property
    def llm_enabled(self) -> bool:
        return self.llm is not None

    async def decide(self, c: Candidate, self_state: dict | None = None) -> Verdict:
        if c.score < self.entry:
            return Verdict(action="skip", mode=c.mode, conviction=c.score, reasoning="below entry threshold")

        if self.llm is not None and self.escalator.allow(c.score, c.mint):
            lessons = self.memory.recall(c)
            prompt = self._candidate_prompt(c, lessons, self_state)
            # label the source by the actual backend tier. Each backend names its own tiers
            # (AnthropicLLM: haiku/sonnet; DualLocalLLM: the two model names); the getattr
            # fallback preserves the legacy labels for any backend without the attrs.
            base_src = getattr(self.llm, "base_source", None) or \
                ("haiku" if self.llm.supports_escalation else self.llm.name)
            data = await self.llm.decide(CONSTITUTION, prompt, escalate=False)
            if data is not None:
                v = Verdict.from_dict(data, base_src)
                # P10 #2: escalate a BUY when being wrong is most expensive — not only low-conviction
                # (a confident-but-flagged buy, or one that contradicts a recalled rug lesson, costs the
                # most). Budget-gated by try_reserve() (counts vs llm_per_min), backend must support it.
                if (self._should_escalate(v, lessons)
                        and self.llm.supports_escalation and self.escalator.try_reserve()):
                    # P10 #1: hand the deep model the screener's verdict to ARBITRATE (biased skeptical),
                    # not a blind re-roll of the same prompt. Addendum rides in the USER message so the
                    # cached CONSTITUTION prefix stays byte-stable; no llm.decide signature change.
                    esc_prompt = prompt + "\n\n" + self._escalation_addendum(v)
                    data2 = await self.llm.decide(CONSTITUTION, esc_prompt, escalate=True)
                    if data2 is not None:
                        esc_src = getattr(self.llm, "escalated_source", "sonnet")
                        v = Verdict.from_dict(data2, esc_src)
                v.recalled_lessons = list(lessons)   # P10b #14: which lessons informed this call (advisory metadata)
                return v
            # LLM failed -> fall through to rule verdict

        return self._rule_verdict(c)

    def _should_escalate(self, v: Verdict, lessons: list[str]) -> bool:
        """P10 #2: which BUYs deserve the scarce deep-model second opinion. Skips are cheap and already
        safe -> never escalated. Escalate when wrong is most expensive: low conviction (the original
        case), the screener raised its OWN concern (non-empty risk_flags), or the buy contradicts a
        recalled rug lesson. Advisory routing only — the deterministic risk layer vetoes regardless."""
        if v.action != "buy":
            return False
        if v.conviction < _HARD_BUY_CONVICTION:
            return True
        if v.risk_flags:
            return True
        return any("rug" in str(x).lower() for x in (lessons or []))

    def _escalation_addendum(self, v: Verdict) -> str:
        """P10 #1: the second-opinion block appended to the escalated prompt — turns the deeper model
        into a skeptical reviewer of the screener's verdict rather than an independent re-roll."""
        flags = ", ".join(v.risk_flags) if v.risk_flags else "none"
        return (
            "SECOND-OPINION REVIEW — you are the SENIOR reviewer of a first-pass screener.\n"
            f"The screener returned: action={v.action}, conviction={v.conviction:.2f}, "
            f"primary_signal={v.primary_signal}, risk_flags=[{flags}].\n"
            f"Its reasoning: \"{v.reasoning}\"\n"
            "Confirm or OVERRULE it. Be MORE skeptical than the screener: the prior is hostile and a "
            "confident wrong BUY is the most expensive error. Only confirm a buy if the convergence "
            "genuinely holds up under scrutiny; when in doubt, overrule to SKIP — that veto stands.")

    def _rule_verdict(self, c: Candidate) -> Verdict:
        span = max(1e-6, 1.0 - self.entry)
        size = _clamp((c.score - self.entry) / span, 0.25, 1.0)
        reasoning = "rule-score fallback"
        # P10b LM2: consult memory even on the LLM-less fallback path. If a recalled RUG/avoid lesson
        # MATCHES this candidate's situation, DAMP toward caution. Advisory-CONSERVATIVE only — it can
        # make the rule path MORE selective (smaller size, or flip to watch), NEVER less; it never
        # upgrades a skip to a buy nor relaxes a deterministic gate (recall is in-memory, no I/O).
        sit = self.memory._situation_tokens(c)
        danger = 0
        for lz in self.memory.recall(c):
            low = lz.lower()
            if ("rug" in low or "avoid" in low or "skip" in low) and any(t and t in low for t in sit):
                danger += 1
        if danger >= 2:
            return Verdict(action="watch", mode=c.mode, conviction=c.score, size_pct=0.0,
                           reasoning="rule fallback overruled by matching rug/avoid lessons", source="rule")
        if danger >= 1:
            size = min(size, 0.35)
            reasoning = "rule-score fallback (size damped: a matching rug/avoid lesson)"
        # P10b SO1: the rule fallback honors the 'risky' setup lean DETERMINISTICALLY (only shrinks
        # size; the risk_flag flows to reflection so the loop learns WHY it sized down).
        flags = []
        if c.features.get("setup_type", "") == "risky":
            size *= self.s.setup_risky_size_mult
            flags.append("risky_setup")
        return Verdict(action="buy", mode=c.mode, conviction=c.score, size_pct=size,
                       tp_pct=0.0, sl_pct=0.0, reasoning=reasoning, risk_flags=flags, source="rule")

    async def reflect(self, ct: ClosedTrade) -> ReflectionNote | None:
        if self.llm is None:
            return None
        data = await self.llm.reflect(CONSTITUTION, self._reflect_prompt(ct))
        if data is None:
            return None
        note = ReflectionNote.from_dict(data)
        if note.lesson and is_actionable_lesson(note.lesson):   # P10b LM1: drop platitudes at the boundary
            # tag the lesson with the trade's SETUP TYPE + EXIT condition, so it's recalled for future
            # same-setup candidates AND for live positions showing that exit condition (LM5).
            stag = setup_tag(getattr(ct, "setup_type", ""))
            etag = _exit_token(ct.reason)
            extra = "-".join(t for t in (stag, etag) if t)
            tag = "-".join(sorted({t for t in (note.tag + "-" + extra).split("-") if t})) if extra else note.tag
            await self.memory.store(note.lesson, tag, note.confidence)
            log.info("lesson [%s, conf %.2f]: %s", tag, note.confidence, note.lesson[:140])
        elif note.lesson:
            log.debug("lesson rejected (not actionable): %s", note.lesson[:80])
        return note

    async def meta_reflect(self, stats, recent: list[ClosedTrade], miss_traits: dict | None = None) -> ReflectionNote | None:
        """AI3 — higher-level than per-trade reflect: review AGGREGATE recent performance
        and distill ONE strategy adjustment, stored as a broad lesson the brain recalls.
        Advisory only (it never edits config); it shapes future reasoning via memory.
        P10b #1: miss_traits (recent missed-winner profile) closes the regret loop — it lets the
        meta-reflection weigh whether the SKIPs were over-strict, without licensing 'buy everything'."""
        if self.llm is None or not recent:
            return None
        data = await self.llm.reflect(CONSTITUTION, self._meta_prompt(stats, recent, miss_traits))
        if data is None:
            return None
        note = ReflectionNote.from_dict(data)
        if note.lesson and is_actionable_lesson(note.lesson):   # P10b LM1
            # store as a slightly-boosted lesson; default the tag to 'global' so it is
            # recalled for every candidate (a strategy-level insight, not mode-specific).
            await self.memory.store(note.lesson, note.tag or "global", min(0.9, note.confidence + 0.1))
            log.info("META-lesson [%s]: %s", note.tag, note.lesson[:180])
        return note

    async def reflect_miss(self, cand: dict, fwd_ret: float) -> "ReflectionNote | None":
        """P8: distill a lesson from a MISSED winner — a token we observed, did NOT buy, that then
        pumped. The counterfactual complement to reflect() (which learns from LOSSES we took). Writes
        into the same memory recalled before future decisions, so the agent stops repeating the
        rejection — WITHOUT drifting into over-trading (the prompt re-affirms Default-to-SKIP and the
        confidence is damped: a miss is a weaker signal than a realized loss). Advisory only."""
        if self.llm is None:
            return None
        data = await self.llm.reflect(CONSTITUTION, self._miss_prompt(cand, fwd_ret))
        if data is None:
            return None
        note = ReflectionNote.from_dict(data)
        if note.lesson and is_actionable_lesson(note.lesson):   # P10b LM1
            # tag -> 'global' so the selectivity lesson is recalled for EVERY future candidate;
            # clamp confidence <= 0.7 (survivorship + no-true-peak make a miss a soft signal).
            # P10b #2: prefix the tag with a stable 'miss' marker so recall() can BOUND how many
            # lean-in regret lessons fill the slots (they must not crowd out survival/rug lessons).
            tag = "miss-" + (note.tag or "global")
            await self.memory.store(note.lesson, tag, min(0.7, note.confidence))
            log.info("MISS-lesson [%s, conf %.2f]: %s", tag, note.confidence, note.lesson[:160])
        return note

    def _miss_prompt(self, cand: dict, fwd_ret: float) -> str:
        import json as _json
        try:
            f = _json.loads(cand.get("features") or "{}")
        except (ValueError, TypeError):
            f = {}
        trend = f.get("trend") or {}
        tech = f.get("tech") or {}
        gate = ("PASSED the safety gate but we still skipped it (ranking / entry-threshold / brain skip)"
                if cand.get("rule_passed") else "was REJECTED by a hard safety/market gate")
        return "\n".join([
            "You SKIPPED a token and it subsequently PUMPED. This is a MISS — learn from the regret.",
            "Default-to-SKIP is CORRECT and survival comes first, so do NOT conclude 'buy everything'.",
            "Identify the SPECIFIC trait that, in hindsight, should have raised conviction — OR confirm",
            "the skip was still correct (a pump that later round-trips to zero is NOT a win we wanted).",
            "",
            f"  symbol: {cand.get('symbol') or cand.get('mint', '')[:8]}   mode: {cand.get('mode')}",
            f"  forward move since we saw it: +{fwd_ret * 100:.0f}%   entry_score: {cand.get('score') or 0.0:.2f}",
            f"  gate status: it {gate}",
            f"  liquidity_usd: {f.get('liquidity_usd', 0):.0f}   vol_h1: {f.get('vol_h1', 0):.0f}   "
            f"buy/sell ratio: {f.get('bsr_h1', 0):.2f}",
            f"  trend at skip: {trend.get('dir', 'unknown')} ({trend.get('chg_pct', 0.0):+.1f}% / "
            f"{trend.get('n', 0)} polls), {trend.get('off_high_pct', 0.0):.1f}% off high",
            f"  indicators at skip: EMA {tech.get('ema_signal', 'n/a')}, breakout {tech.get('breakout', 'n/a')}",
            "",
            "Write ONE concrete, full-sentence lesson (~12-28 words) naming the TRAIT we under-weighted and",
            "the conditional ACTION (e.g. \"When liquidity>15k AND buy/sell>2 with a rising 5m trend, the",
            "entry bar is too strict — lean toward a small starter, not skip.\"). Stay survival-first: if the",
            "pump looks like an unsustainable wash spike, say the skip was CORRECT.",
            "Tag it with one of: scalp, hold, rug, global.",
        ])

    def _meta_prompt(self, stats, recent: list[ClosedTrade], miss_traits: dict | None = None) -> str:
        from collections import Counter
        reasons = ", ".join(f"{k}:{v}" for k, v in Counter(t.reason for t in recent).most_common())
        lines = [
            "Review your RECENT TRADING PERFORMANCE AS A WHOLE (not a single trade) and distill the",
            "single most important pattern + the ONE strategy adjustment it implies going forward.",
            "",
            f"  trades: {stats.n_trades}   win-rate: {stats.win_rate * 100:.0f}%   "
            f"avg pnl: {stats.avg_pnl_sol:+.4f} SOL   total: {stats.total_pnl_sol:+.4f} SOL",
            f"  best: {stats.best_sol:+.4f}   worst: {stats.worst_sol:+.4f}   exit reasons: {reasons}",
        ]
        # P10b #1: the regret complement — tokens we SKIPPED that then pumped. gate_failed_frac is the
        # diagnostic: HIGH => the HARD SAFETY GATES are rejecting winners (a calibration signal, NOT a
        # license to relax a safety gate); LOW => winners cleared the gate but ranking/entry/brain
        # passed them up (the DECISION layer may be slightly over-strict). Survival-first still binds.
        mt = miss_traits or {}
        if mt.get("n", 0):
            verdmix = mt.get("verdict_mix")
            mixline = (f"   verdict mix: {verdmix}" if verdmix else "")
            lines += [
                "",
                "RECENT REGRET (tokens you SKIPPED that then pumped):",
                f"  missed winners: {int(mt['n'])}   median forward: {mt.get('median_fwd_pct', 0.0):+.0f}%   "
                f"gate_failed_frac: {mt.get('gate_failed_frac', 0.0) * 100:.0f}%{mixline}",
                "  HIGH gate_failed_frac = the hard safety gates rejected winners (note it as a CALIBRATION "
                "signal — do NOT conclude 'relax the safety gate'). LOW = the decision/ranking layer skipped "
                "winners that passed the gates (consider being slightly less strict THERE). Most 'misses' are "
                "fades/deaths, so selectivity is usually right — weigh regret against survival, don't chase.",
            ]
        lines += [
            "",
            "e.g. 'most losses are scalp timeouts -> demand fresh upward momentum before a scalp', or",
            "'hold winners are rare but large -> let hold targets run wider'. Be concrete and honest.",
            "Write ONE full-sentence lesson (~12-28 words) naming the PATTERN and the ACTION it implies.",
            "Tag it with one of: scalp, hold, rug, global.",
        ]
        return "\n".join(lines)

    # ── prompt builders (volatile data -> user message, never the cached system) ──
    def _candidate_prompt(self, c: Candidate, lessons: list[str], self_state: dict | None = None) -> str:
        def auth(v, name: str, danger: str) -> str:
            if v is None:
                return f"    {name} authority: UNKNOWN (caution)"
            return (f"    {name} authority: REVOKED -> SAFE (good, passed)" if v
                    else f"    {name} authority: ACTIVE -> DANGER ({danger})")

        def lp() -> str:
            if c.lp_burned_pct is None:
                return "    LP burned: unknown (caution)"
            return f"    LP burned: {round(c.lp_burned_pct, 1)}% " + ("(good)" if c.lp_burned_pct >= 90 else "(LOW -> risk)")

        def trend_line() -> str:
            t = c.features.get("trend") or {}
            d = t.get("dir")
            if not d or d == "unknown":
                return "    short-term trend: not enough price history yet (caution)"
            return (f"    short-term trend: {d} ({t.get('chg_pct', 0.0):+.1f}% over last {t.get('n', 0)} polls, "
                    f"now {t.get('off_high_pct', 0.0):.1f}% below the recent high)")

        def tech_line() -> str:
            t = c.features.get("tech") or {}
            if not t or t.get("ema_signal", "n/a") == "n/a":
                return "    indicators: warming up (need more price history)"
            return (f"    indicators: EMA {t.get('ema_signal')}, breakout {t.get('breakout')}, "
                    f"{t.get('res_dist_pct', 0.0):.1f}% to resistance / {t.get('sup_dist_pct', 0.0):.1f}% above support")

        def liq_line() -> str:
            t = c.features.get("liq_trend") or {}
            d = t.get("dir")
            if not d or d == "unknown":
                return "    liquidity trend: not enough polls yet"
            warn = "  -> POOL DRAINING (rug-in-progress)" if d == "falling" else ""
            return f"    liquidity trend: {d} ({t.get('chg_pct', 0.0):+.1f}% over {t.get('n', 0)} polls){warn}"

        def breadth_line() -> str:
            # P10b SR1/SR4: buyer-breadth + sell-count velocity + fresh 5m breadth. ADVISORY/WEAK
            # (velocity is research-refuted as a standalone predictor): rising buyers + falling sells is
            # a mild constructive cue; accelerating sells corroborates fade/exhaustion. Never a gate.
            bg = c.features.get("buyer_growth")
            sg = c.features.get("sell_growth")
            m5 = c.features.get("bsr_m5")
            if bg is None and sg is None and m5 is None:
                return "    breadth velocity: warming up (need more polls)"
            parts = []
            if bg is not None:
                parts.append(f"buyers {bg:+.0f}")
            if sg is not None:
                parts.append(f"sells {sg:+.0f}" + ("  (accelerating selling -> fade/exhaustion)" if sg > 0 else ""))
            if m5 is not None:
                parts.append(f"fresh 5m buy/sell {m5:.2f}")
            return "    breadth velocity (weak cue): " + ", ".join(parts)

        _PLAYBOOK = {
            "creator_gold": "the dev is fully OUT but the price is organically strong -> no dev-dump overhang "
                            "left, you only face other holders. A high-quality frame IF holders stay + the trend holds.",
            "safe_hold": "cleanly graduated + both authorities revoked + holding up -> durable enough to HOLD for "
                         "a bigger move (let the trail run).",
            "momentum": "positive short-term action but unproven -> scalp-grade: take a DEFINED move then exit, don't overstay.",
            "risky": "elevated scam/concentration risk -> if you act at all, smallest size + fastest exit; default lean is SKIP.",
            "unproven": "no clear strength signal yet -> default lean is SKIP; act only on real multi-signal convergence.",
        }
        stype = c.features.get("setup_type", "unproven")
        lines = [
            "Evaluate this Solana token and return a trade verdict.",
            "",
            "CANDIDATE",
            f"  symbol: {c.symbol or c.mint[:8]}   suggested_mode: {c.mode}",
            f"  SETUP TYPE: {stype} -> {_PLAYBOOK.get(stype, _PLAYBOOK['unproven'])}",
            f"  price_usd: {c.price_usd:.8g}   liquidity_usd: {c.liquidity_usd:.0f}   mcap_usd: {c.market_cap_usd:.0f}",
            f"  vol/mcap %: {c.vol_to_mcap_pct:.1f}   buy/sell ratio: {c.buy_sell_ratio:.2f}   "
            f"buyers(proxy): {c.unique_buyers}   sells(h1): {int(c.features.get('sells_h1', 0))}",
            f"  rule_score: {c.score:.2f}   vol_spike: {float(c.features.get('vol_spike', 0)):.2f}",
            "  PRICE ACTION / TREND  (read the trajectory, not just the snapshot)",
            f"    5m change: {c.price_change_m5:+.1f}%   1h change: {c.price_change_h1:+.1f}%",
            trend_line(),
            tech_line(),
            liq_line(),
            breadth_line(),
            "  SAFETY  (REVOKED authority = SAFE/good; ACTIVE = danger; unknown = caution)",
            auth(c.mint_revoked, "mint", "dev can print unlimited supply"),
            auth(c.freeze_revoked, "freeze", "honeypot - you may be unable to sell"),
            lp(),
            f"    top-5 holder concentration: {'unknown' if c.top5_concentration_pct is None else str(round(c.top5_concentration_pct, 1)) + '%'}",
            f"    scam-likelihood: {scam_likelihood(c, self.s):.2f}  (0=clean .. 1=likely scam; weigh it — high => skip)",
            f"    holder base: {holder_quality(c, self.s)}  (healthy => can HOLD for a bigger move; concentrated => SCALP and exit fast)",
            f"    creator launches: {int(c.features.get('creator_launches', 0))}  (a wallet that spam-launches many tokens is a serial rugger; a legit dev launches 1-2)",
        ]
        nrc = c.features.get("name_reuse_count")             # branding-duplication: scam-factory tell
        if nrc is not None and nrc > 2:
            lines.append(f"    name/ticker reuse: this name+symbol has appeared on {int(nrc)} mints "
                         "(reused branding -> a scam-factory tell, though popular memes repeat legitimately)")
        fcc = c.features.get("funder_creator_count")         # N12: rotated-creator cluster via common funder
        if fcc is not None and fcc >= self.s.safety.funder_serial_min:
            lines.append(
                f"    funder cluster: this creator's FUNDING wallet has spawned {int(fcc)} distinct "
                "creators (rotated wallets, one treasury — a serial operator hiding behind fresh creators)")
        hfc = c.features.get("holder_funder_cluster")        # holder funding-cluster: concealed concentration
        if hfc is not None and hfc >= self.s.safety.holder_cluster_warn:
            lines.append(
                f"    HOLDER cluster: {int(hfc)} of the top holders were funded from ONE wallet "
                "(several 'independent' top holders are one entity behind many wallets -> concealed "
                "concentration / coordinated-dump setup -> strong skip lean)")
        rug_rate = c.features.get("creator_rug_rate")        # P8: this creator's LEARNED track record
        if rug_rate is not None:
            rep_n = int(c.features.get('creator_rep_n', 0))
            lines.append(
                f"    creator track record: {rug_rate * 100:.0f}% of its "
                f"{rep_n} classified tokens RUGGED "
                "(learned from your OWN past outcomes — a high rate is a strong skip tell)")
            # P10b CAL3: if the RECENCY-weighted rug rate is materially lower than the raw rate, the
            # rugs are stale — a possibly-reformed/active creator. Advisory lean only (the deterministic
            # count-based penalty in rules.py is unchanged).
            rrr = c.features.get("creator_rug_rate_recent")
            if rrr is not None and rug_rate - rrr >= 0.15:
                lines.append(
                    f"    ...but RECENTLY only {rrr * 100:.0f}% (recency-weighted): the rugs are mostly "
                    "STALE, so weigh this creator less harshly than the raw rate suggests")
            # P10 #7: also surface a PROVEN POSITIVE record so a repeat-legit dev earns advisory credit,
            # not only blame. Lean ONLY — necessary-not-sufficient; safety + breadth must still converge.
            win_rate = c.features.get("creator_win_rate")
            if (win_rate is not None and rep_n >= self.s.safety.creator_rep_min_tokens
                    and win_rate >= 0.5 and rug_rate <= 0.2):
                lines.append(
                    f"    creator UPSIDE: {win_rate * 100:.0f}% of those {rep_n} tokens WON with only a "
                    f"{rug_rate * 100:.0f}% rug rate (a learned positive track record -> modest positive "
                    "lean, NOT a green light on its own)")
        chp = c.features.get("creator_holding_pct")          # P8: has the creator dumped their bag?
        if chp is not None:
            if chp >= 1.0:
                held = f"still holds {chp:.1f}% of supply (skin in the game)"
            else:
                # user insight: creator-sold + STILL THRIVING = the GOLD setup (dev overhang gone, demand
                # organic, you only face other holders). Creator-sold + weak = the rug risk.
                trend = (c.features.get("trend") or {}).get("dir")
                # SUSTAINED strength: a confirmed uptrend, or a positive 1h change while the short tape
                # isn't rolling over (h1 lags — a launch pump stays green ~an hour even mid-dump).
                # P10b SO3: keep IN SYNC with signals/setup.py thriving — require non-negative m5 even on
                # a rising-trend label, so a rolling-over token isn't framed as the GOLD setup.
                thriving = c.price_change_m5 >= 0.0 and (trend == "rising" or (c.price_change_h1 > 0.0 and trend != "falling"))
                held = ("has SOLD OUT its allocation BUT the price is rising/holding -> the dev overhang "
                        "is GONE and demand looks organic: a potential GOLD setup (you only compete with "
                        "the other holders now) IF holders stay + the trendline holds" if thriving
                        else "has SOLD OUT its allocation while the price is NOT strong -> strong rug risk; skip")
            lines.append(f"    creator wallet: {held}")
        cd = c.features.get("creator_dump_ratio")            # P8 (G3 tape): creator selling RIGHT NOW?
        if cd is not None and cd > 0.0:
            lines.append(f"    LIVE TAPE: creator has sold {cd * 100:.0f}% of what it bought "
                         "(real-time dev dump in progress -> strong skip)")
        ss = c.features.get("sniper_share")                  # P8 (G3 tape): sniper concentration
        if ss is not None:
            lines.append(f"    LIVE TAPE: first wallets own {ss * 100:.0f}% of buy volume "
                         f"({'sniper-dominated -> they dump on you' if ss >= 0.6 else 'broad'})")
        bs = c.features.get("bundle_share")                  # P8 (G3 tape): coordinated bundles
        if bs is not None and bs >= 0.3:
            lines.append(f"    LIVE TAPE: {bs * 100:.0f}% of buys are COORDINATED BUNDLES "
                         "(same-block wallet clusters -> concealed concentration, the #1 rug tell)")
        sm = c.features.get("smart_money_share")             # P8 (G3 tape): copy-trade POSITIVE axis
        if sm is not None and sm > 0.0:
            lines.append(f"    LIVE TAPE: {sm * 100:.0f}% of buy volume is from PROVEN-PROFITABLE wallets "
                         "(smart money is accumulating -> positive lean, the one bullish tape signal)")
        if lessons:
            lines.append("")
            lines.append("LESSONS FROM YOUR PAST TRADES")
            lines += [f"  - {x}" for x in lessons]
        if self_state:
            st = self_state
            lines.append("")
            lines.append("YOUR BOOK (account state — manage risk like a trader running a book)")
            lines.append(f"    equity: {st.get('equity_pct', 0.0):+.1f}% vs start   open: {st.get('open', 0)}/{st.get('max_positions', 0)} positions")
            lines.append(f"    today's loss: {st.get('daily_loss', 0.0):.3f} of {st.get('daily_cap', 0.0):.2f} SOL cap   loss streak: {st.get('streak', 0)}")
            if st.get('recent_n', 0):
                lines.append(f"    recent win-rate: {st.get('recent_win', 0.0) * 100:.0f}% of last {st.get('recent_n', 0)}")
            ob = st.get('outcome_base')                      # P10 #8: own measured base rate (calibrated prior)
            if ob and ob.get('n', 0):
                lines.append(
                    f"    measured base rate over {int(ob['n'])} of your OWN classified tokens: "
                    f"rug {ob.get('rug', 0.0) * 100:.0f}%, fade {ob.get('fade', 0.0) * 100:.0f}%, "
                    f"winner {ob.get('winner', 0.0) * 100:.0f}% -> the prior is heavily SKIP; only real "
                    "multi-signal convergence justifies entry")
            lines.append("    -> near the daily cap, on a losing streak, or with all slots full => be MORE selective or SKIP. Survival first.")
        lines.append("")
        lines.append("Weigh the PRICE ACTION: a rising/steady trend with real momentum favors entry; "
                     "buying into a sharp drop or far below the recent high risks catching a falling knife. "
                     "For scalp prefer fresh upward momentum; for hold prefer a constructive higher trend.")
        lines.append("Return your verdict. Skip if an authority is ACTIVE or signals are thin. "
                     "A token with mint & freeze REVOKED has PASSED those safety checks (that is good) — "
                     "but revocation is NECESSARY, not SUFFICIENT: scammers revoke too, then dump. "
                     "Also weigh the behavioral signals (volume, trend, holders, scam-likelihood).")
        return "\n".join(lines)

    def _reflect_prompt(self, ct: ClosedTrade) -> str:
        hold_s = max(0.0, ct.closed_ts - ct.opened_ts)
        outcome = "WIN" if ct.pnl_sol > 0 else ("LOSS" if ct.pnl_sol < 0 else "FLAT")
        lines = [
            "A trade just closed. Distill ONE concrete, reusable lesson for future trades.",
            "",
            f"  symbol: {ct.symbol or ct.mint[:8]}   mode: {ct.mode}",
            f"  outcome: {outcome}   pnl: {ct.pnl_sol:+.4f} SOL ({ct.pnl_pct*100:+.1f}%)   exit: {ct.reason}",
            f"  hold time: {hold_s:.0f}s   cost: {ct.cost_sol:.4f} SOL -> proceeds: {ct.proceeds_sol:.4f} SOL",
            # P10 #11: MFE/MAE — what the trade did BETWEEN entry and exit, so exit-timing is learnable.
            f"  peak unrealized: {ct.peak_pct*100:+.0f}%   worst drawdown: {ct.trough_pct*100:+.0f}%   during the hold",
            f"  entry rationale was: {ct.entry_note or '(none recorded)'}",
        ]
        # P10 #4: surface the brain's OWN at-entry concerns so a loss can be reflected against them.
        if ct.entry_risk_flags:
            lines.append(f"  at entry you flagged these risks: {', '.join(str(x) for x in ct.entry_risk_flags)}")
        lines += [
            "",
            "If the exit left a large gap below the peak (sold far under peak unrealized), the lesson "
            "should be about EXIT TIMING, not entry. If you flagged a risk at entry and it cost you, "
            "the lesson is about HEEDING that flag.",
            "Write the lesson as ONE specific, full sentence (~10-25 words) naming the SIGNAL and the "
            "ACTION it implies, e.g. \"When freeze authority is active, skip regardless of momentum - it's a honeypot.\" "
            "Do NOT reply with a single word or just the category.",
            "Tag it with one of: scalp, hold, rug, global.",
        ]
        return "\n".join(lines)
