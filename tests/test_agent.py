"""Brain-layer tests: Verdict/Reflection schemas, escalation, memory, local _loads,
rule-fallback decide. Async paths are wrapped with asyncio.run (no pytest-asyncio)."""
from __future__ import annotations

import asyncio

from memebot.agent.brain import TradingBrain
from memebot.agent.escalation import Escalator
from memebot.agent.local_llm import _loads
from memebot.agent.memory import AgentMemory
from memebot.agent.schema import ReflectionNote, Verdict
from memebot.config import Settings
from memebot.models import Candidate
from memebot.storage.db import Storage


def test_verdict_clamps_and_bounds():
    v = Verdict.from_dict(
        {"action": "buy", "mode": "scalp", "conviction": 5, "size_pct": -1,
         "tp_pct": 0.0005, "sl_pct": 18, "reasoning": "x"}, "rule")
    assert v.conviction == 1.0 and v.size_pct == 0.0
    assert v.tp_pct == 0.0 and v.sl_pct == 0.0          # out of range -> mode default
    v2 = Verdict.from_dict(
        {"action": "buy", "mode": "hold", "conviction": 0.6, "size_pct": 0.5,
         "tp_pct": 0.4, "sl_pct": 0.18, "reasoning": "y"}, "haiku")
    assert v2.tp_pct == 0.4 and v2.sl_pct == 0.18


def test_verdict_rejects_nonfinite():
    # a junk LLM verdict (NaN/Infinity, which json.loads accepts) must fail SAFE to 0,
    # not survive the clamp as max conviction / max size.
    v = Verdict.from_dict(
        {"action": "buy", "mode": "scalp", "conviction": float("nan"),
         "size_pct": float("inf"), "tp_pct": 0.4, "sl_pct": 0.18, "reasoning": "x"}, "haiku")
    assert v.conviction == 0.0 and v.size_pct == 0.0


def test_reflection_from_dict_normalizes():
    n = ReflectionNote.from_dict({"lesson": "x" * 1000, "tag": "SCALP", "confidence": 2})
    assert len(n.lesson) <= 600 and n.tag == "scalp" and n.confidence == 1.0
    # tag canonicalized onto the known vocabulary so recall works
    assert ReflectionNote.from_dict({"lesson": "x", "tag": "rug-pull"}).tag == "rug"
    assert ReflectionNote.from_dict({"lesson": "x", "tag": "scalp_exit"}).tag == "scalp"
    assert ReflectionNote.from_dict({"lesson": "x", "tag": "fakeout"}).tag == "global"
    assert ReflectionNote.from_dict({"lesson": "x", "tag": "scalp-rug"}).tag == "rug-scalp"


def test_escalator_band_budget_dedupe():
    e = Escalator(per_min=2, high_conf=0.8, entry_threshold=0.55, dedupe_window_s=120)
    assert not e.allow(0.40, "m", 1000.0)    # below band
    assert not e.allow(0.90, "m", 1000.0)    # above band (slam dunk)
    assert e.allow(0.60, "a", 1000.0)        # in band -> slot 1
    assert not e.allow(0.60, "a", 1001.0)    # same mint within window -> dedupe
    assert e.allow(0.60, "b", 1001.0)        # slot 2
    assert not e.allow(0.60, "c", 1002.0)    # budget exhausted


def test_escalator_try_reserve_and_seen_prune():
    e = Escalator(per_min=2, high_conf=0.8, entry_threshold=0.55, dedupe_window_s=120)
    assert e.allow(0.6, "a", 1000.0)
    assert e.try_reserve(1000.0)             # second slot
    assert not e.try_reserve(1000.0)         # exhausted
    e2 = Escalator(per_min=100, high_conf=0.8, entry_threshold=0.55, dedupe_window_s=120)
    e2.allow(0.6, "a", 1000.0)
    assert len(e2._seen) == 1
    e2.allow(0.6, "b", 1200.0)               # 200s later: 'a' pruned (window 120)
    assert len(e2._seen) == 1


def test_loads_robust():
    assert _loads('{"a":1}') == {"a": 1}
    assert _loads('thinking... {"a": 1} done') == {"a": 1}
    assert _loads("garbage") is None
    assert _loads("") is None


def test_memory_recall_token_match():
    st = Storage(":memory:"); st.connect()
    mem = AgentMemory(st)

    async def run():
        await st.log_lesson(lesson="household NOT a match", tag="household", confidence=0.5)
        await st.log_lesson(lesson="scalp lesson", tag="scalp", confidence=0.5)
        await st.log_lesson(lesson="compound", tag="scalp-rug", confidence=0.5)
        await mem.load()
        c = Candidate(mint="x"); c.mode = "scalp"
        r = mem.recall(c)
        assert "household NOT a match" not in r
        assert "scalp lesson" in r and "compound" in r

    asyncio.run(run())


def test_setup_tag_and_recall_loop():
    from memebot.agent.schema import setup_tag
    assert setup_tag("creator_gold") == "gold" and setup_tag("risky") == "risky"
    assert setup_tag("safe_hold") == "hold" and setup_tag("unproven") == "" and setup_tag(None) == ""
    st = Storage(":memory:"); st.connect()
    mem = AgentMemory(st)

    async def run():
        await mem.load()
        await mem.store("gold setups can still fade if holders leave", "gold", 0.6)
        await mem.store("a scalp-only note", "scalp", 0.6)
        c = Candidate(mint="x"); c.mode = "hold"; c.features["setup_type"] = "creator_gold"
        out = mem.recall(c, k=5)
        assert "gold setups can still fade if holders leave" in out   # recalled via the candidate's setup_type
        assert "a scalp-only note" not in out                        # scalp tag, hold candidate -> no match

    asyncio.run(run())


def test_lesson_curation_dedup_reinforcement_and_decay():
    from memebot.agent.memory import _norm, curate_lessons
    assert _norm("  Avoid  RUGS. ") == "avoid rugs"
    now = 1000.0 * 86_400
    rows = [
        {"lesson": "Avoid rugs", "tag": "rug", "confidence": 0.5, "ts": now - 10},
        {"lesson": "avoid rugs.", "tag": "global", "confidence": 0.6, "ts": now - 5},   # dup -> merged
        {"lesson": "avoid RUGS", "tag": "rug", "confidence": 0.4, "ts": now},            # dup -> hits=3
        {"lesson": "Hold winners", "tag": "hold", "confidence": 0.5, "ts": now},         # distinct, single
    ]
    cur = curate_lessons(rows, now=now)
    assert len(cur) == 2                                      # deduped from 4 raw to 2
    rug = next(e for e in cur if "rug" in e["lesson"].lower())
    assert rug["hits"] == 3 and "global" in rug["tag"] and "rug" in rug["tag"]   # reinforced + tag union
    assert cur[0]["lesson"].lower().startswith("avoid")      # reinforcement outscores the single lesson
    assert len(curate_lessons([{"lesson": f"l{i}", "tag": "g", "confidence": 0.5, "ts": now}
                               for i in range(50)], max_keep=10, now=now)) == 10   # prune cap
    # age-decay: a fresh one-off outscores a stale one-off of equal confidence
    aged = curate_lessons([{"lesson": "fresh", "tag": "global", "confidence": 0.5, "ts": now},
                           {"lesson": "stale", "tag": "global", "confidence": 0.5, "ts": now - 60 * 86_400}],
                          now=now, half_life_s=14 * 86_400)
    assert aged[0]["lesson"] == "fresh"


def test_memory_store_reinforces_and_recall_prefers_validated():
    st = Storage(":memory:"); st.connect()
    mem = AgentMemory(st)

    async def run():
        await mem.load()
        for _ in range(3):
            await mem.store("rugs dump fast", "rug", 0.5)    # re-learned 3x -> reinforced into ONE entry
        await mem.store("a one-off note", "rug", 0.5)
        rug_entries = [e for e in mem._cache if "rug" in e["lesson"]]
        assert len(rug_entries) == 1 and rug_entries[0]["hits"] == 3   # deduped + hit-counted, not 3 copies
        c = Candidate(mint="x"); c.mode = "hold"
        out = mem.recall(c, k=2)
        assert out[0] == "rugs dump fast"                    # the most-validated lesson surfaces first

    asyncio.run(run())


def test_brain_rule_fallback_decide():
    st = Storage(":memory:"); st.connect()
    mem = AgentMemory(st)

    async def run():
        await mem.load()
        brain = TradingBrain(Settings(), mem, None)      # no LLM -> rule fallback
        strong = Candidate(mint="m"); strong.score = 0.72; strong.mode = "scalp"
        v = await brain.decide(strong)
        assert v.action == "buy" and v.source == "rule" and 0 < v.size_pct <= 1
        weak = Candidate(mint="z"); weak.score = 0.20
        assert (await brain.decide(weak)).action == "skip"

    asyncio.run(run())


def test_brain_entry_threshold_override():
    # GBM-scale entry threshold is honored
    brain = TradingBrain(Settings(), AgentMemory(Storage(":memory:")), None, entry_threshold=0.5)
    assert brain.entry == 0.5


def test_candidate_prompt_includes_price_action():
    brain = TradingBrain(Settings(), AgentMemory(Storage(":memory:")), None)
    c = Candidate(mint="x"); c.mode = "scalp"
    c.price_change_m5, c.price_change_h1 = 12.5, -3.0
    c.features["trend"] = {"n": 10, "dir": "rising", "chg_pct": 8.0, "off_high_pct": 2.0}
    p = brain._candidate_prompt(c, [])
    assert "PRICE ACTION / TREND" in p
    assert "5m change: +12.5%" in p and "1h change: -3.0%" in p
    assert "short-term trend: rising" in p
    c.features["tech"] = {"n": 8, "ema_signal": "bull", "breakout": "up", "res_dist_pct": -2.0, "sup_dist_pct": 15.0}
    pp = brain._candidate_prompt(c, [])
    assert "indicators: EMA bull, breakout up" in pp
    assert "scam-likelihood:" in pp        # AI6 graded danger signal in the SAFETY block
    assert "holder base:" in pp            # AI7 holder-quality lean (hold vs scalp)


def test_candidate_prompt_includes_tape_signals():
    brain = TradingBrain(Settings(), AgentMemory(Storage(":memory:")), None)
    c = Candidate(mint="x"); c.mode = "scalp"
    assert "LIVE TAPE" not in brain._candidate_prompt(c, [])           # no tape data -> no line
    c.features["creator_dump_ratio"] = 0.5; c.features["sniper_share"] = 0.7
    p = brain._candidate_prompt(c, [])
    assert "creator has sold 50%" in p and "dev dump" in p             # real-time dev dump surfaced
    assert "first wallets own 70%" in p and "sniper-dominated" in p    # sniper concentration surfaced


def test_candidate_prompt_includes_creator_wallet():
    brain = TradingBrain(Settings(), AgentMemory(Storage(":memory:")), None)
    c = Candidate(mint="x"); c.mode = "scalp"
    assert "creator wallet" not in brain._candidate_prompt(c, [])          # not fetched -> no line
    c.features["creator_holding_pct"] = 0.0
    assert "SOLD OUT" in brain._candidate_prompt(c, [])                    # dumped -> rug warning surfaced
    c.features["creator_holding_pct"] = 30.0
    assert "still holds 30.0%" in brain._candidate_prompt(c, [])           # skin in the game


def test_candidate_prompt_includes_liquidity_trend():
    brain = TradingBrain(Settings(), AgentMemory(Storage(":memory:")), None)
    c = Candidate(mint="x"); c.mode = "scalp"
    assert "liquidity trend: not enough polls" in brain._candidate_prompt(c, [])   # warming up
    c.features["liq_trend"] = {"dir": "falling", "chg_pct": -25.0, "n": 6}
    p = brain._candidate_prompt(c, [])
    assert "liquidity trend: falling" in p and "POOL DRAINING" in p                # rug pre-tell surfaced


def test_candidate_prompt_includes_creator_track_record():
    brain = TradingBrain(Settings(), AgentMemory(Storage(":memory:")), None)
    c = Candidate(mint="x"); c.mode = "scalp"
    assert "creator track record" not in brain._candidate_prompt(c, [])   # absent reputation -> no line
    c.features["creator_rug_rate"] = 0.75; c.features["creator_rep_n"] = 8
    p = brain._candidate_prompt(c, [])
    assert "creator track record: 75%" in p and "8 classified tokens RUGGED" in p


def test_candidate_prompt_includes_account_state():
    brain = TradingBrain(Settings(), AgentMemory(Storage(":memory:")), None)
    c = Candidate(mint="x"); c.mode = "scalp"
    state = {"equity_pct": -3.5, "open": 2, "max_positions": 3, "daily_loss": 0.9,
             "daily_cap": 1.5, "streak": 2, "recent_n": 10, "recent_win": 0.3}
    p = brain._candidate_prompt(c, [], state)
    assert "YOUR BOOK" in p and "open: 2/3 positions" in p and "loss streak: 2" in p
    assert "YOUR BOOK" not in brain._candidate_prompt(c, [])   # absent state -> backward compatible


def test_meta_reflect_stores_and_recalls_lesson():
    from memebot.portfolio.pnl import compute_stats
    from memebot.portfolio.portfolio import ClosedTrade

    class FakeLLM:
        name = "fake"
        supports_escalation = False
        async def reflect(self, system, prompt):
            assert "RECENT TRADING PERFORMANCE" in prompt   # the meta prompt, not per-trade
            return {"lesson": "Most losses are scalp timeouts; demand fresh momentum before scalping.",
                    "tag": "scalp", "confidence": 0.6}

    st = Storage(":memory:"); st.connect()
    mem = AgentMemory(st)

    async def run():
        await mem.load()
        brain = TradingBrain(Settings(), mem, FakeLLM())
        recent = [ClosedTrade("m", "M", "scalp", 0.1, 0.09, -0.01, -0.1, 0, 1, "timeout") for _ in range(6)]
        note = await brain.meta_reflect(compute_stats(recent), recent)
        assert note is not None and note.lesson
        c = Candidate(mint="x"); c.mode = "scalp"
        assert any("scalp timeouts" in x for x in mem.recall(c))   # stored + immediately recalled

    asyncio.run(run())


# ── dual local model (fast screen + deep confirm) + backend-correct source labels ───────
class _FakeOllama:
    """An OllamaLLM-shaped stub recording which model was called (for DualLocalLLM routing)."""
    def __init__(self, model: str) -> None:
        self.model = model
        self.name = f"ollama:{model}"
        self.calls: list = []
        self.closed = False

    async def decide(self, system, prompt, *, escalate=False):
        self.calls.append(("decide", escalate))
        return {"action": "skip", "mode": "scalp", "conviction": 0.1, "reasoning": "x"}

    async def reflect(self, system, prompt):
        self.calls.append(("reflect",))
        return {"lesson": "x", "tag": "global", "confidence": 0.5}

    async def aclose(self):
        self.closed = True


def test_duallocal_routing_name_and_close():
    from memebot.agent.local_llm import DualLocalLLM
    fast, deep = _FakeOllama("gemma3:4b"), _FakeOllama("qwen3:8b")
    dual = DualLocalLLM(fast, deep)
    assert dual.supports_escalation is True
    assert "gemma3:4b" in dual.name and "qwen3:8b" in dual.name
    assert dual.base_source == "ollama:gemma3:4b" and dual.escalated_source == "ollama:qwen3:8b"

    async def run():
        await dual.decide("c", "p", escalate=False)   # -> fast
        await dual.decide("c", "p", escalate=True)    # -> deep
        await dual.reflect("c", "p")                  # -> deep (quality path)
        await dual.aclose()

    asyncio.run(run())
    assert fast.calls == [("decide", False)]                       # screen only
    assert deep.calls == [("decide", False), ("reflect",)]         # confirm + reflect
    assert fast.closed and deep.closed                             # both closed


def test_duallocal_create_degrades_to_single():
    from memebot.agent.local_llm import DualLocalLLM, OllamaLLM
    fast = _FakeOllama("gemma3:4b")
    saved_create = OllamaLLM.__dict__["create"]
    saved_named = OllamaLLM.__dict__["_create_named"]
    try:
        OllamaLLM.create = classmethod(lambda cls, s: fast)
        OllamaLLM._create_named = classmethod(lambda cls, s, want, *, strict=False: None)  # confirm not pulled
        res = DualLocalLLM.create(Settings(ollama_confirm_model="qwen3:8b"))
        assert res is fast                                          # graceful single-model fallback
        # empty confirm model -> also single
        assert DualLocalLLM.create(Settings(ollama_confirm_model="")) is fast
        # Ollama unreachable -> None
        OllamaLLM.create = classmethod(lambda cls, s: None)
        assert DualLocalLLM.create(Settings()) is None
    finally:
        OllamaLLM.create = saved_create
        OllamaLLM._create_named = saved_named


def _band_brain(llm):
    """A brain whose escalation band [0.5,0.8) is live, with budget, for source-label tests."""
    st = Storage(":memory:"); st.connect()
    return TradingBrain(Settings(llm_per_min=5), AgentMemory(st), llm,
                        entry_threshold=0.5, high_conf_threshold=0.8)


def test_brain_source_label_local_dual_escalates():
    class FakeDual:
        name = "ollama-dual:gemma3:4b+qwen3:8b"
        supports_escalation = True
        base_source = "ollama:gemma3:4b"
        escalated_source = "ollama:qwen3:8b"
        async def decide(self, system, prompt, *, escalate=False):
            if escalate:   # deep confirm -> confident buy
                return {"action": "buy", "mode": "scalp", "conviction": 0.80, "size_pct": 0.6, "reasoning": "deep"}
            return {"action": "buy", "mode": "scalp", "conviction": 0.40, "size_pct": 0.5, "reasoning": "screen"}
        async def reflect(self, system, prompt): return None

    async def run():
        brain = _band_brain(FakeDual())
        await brain.memory.load()
        c = Candidate(mint="m"); c.mode = "scalp"; c.score = 0.6   # in the escalation band
        v = await brain.decide(c)
        # low-conviction screen buy escalated to the DEEP model -> source is the deep model's label
        assert v.action == "buy" and v.source == "ollama:qwen3:8b"

    asyncio.run(run())


def test_brain_source_label_cloud_fallback_unchanged():
    # a backend WITHOUT base_source attrs must still label haiku/sonnet via the getattr fallback
    class FakeCloud:
        name = "claude:x"
        supports_escalation = True
        async def decide(self, system, prompt, *, escalate=False):
            return {"action": "buy", "mode": "scalp", "conviction": 0.80, "size_pct": 0.5, "reasoning": "x"}
        async def reflect(self, system, prompt): return None

    async def run():
        brain = _band_brain(FakeCloud())
        await brain.memory.load()
        c = Candidate(mint="m"); c.mode = "scalp"; c.score = 0.6
        v = await brain.decide(c)
        assert v.action == "buy" and v.source == "haiku"   # high conviction -> no escalation, legacy label

    asyncio.run(run())


def test_validate_llm_parallel_and_confirm_model():
    assert not any("llm_parallel" in w or "ollama_confirm_model" in w for w in Settings().validate())
    assert any("llm_parallel" in w for w in Settings(llm_parallel=0).validate())
    assert any("ollama_confirm_model" in w for w in
               Settings(ollama_confirm_model="gemma3:4b", ollama_model="gemma3:4b").validate())


# ── P8 miss-learning: regret reflection ─────────────────────────────────────────────────
def test_miss_prompt_contains_traits_and_skip_guard():
    import json
    brain = TradingBrain(Settings(), AgentMemory(Storage(":memory:")), None)
    feats = json.dumps({"liquidity_usd": 18000, "vol_h1": 6000, "bsr_h1": 2.5,
                        "trend": {"dir": "rising", "chg_pct": 8.0, "n": 5, "off_high_pct": 2.0},
                        "tech": {"ema_signal": "bull", "breakout": "up"}})
    cand = {"symbol": "TKN", "mint": "m", "mode": "hold", "score": 0.45, "rule_passed": 0, "features": feats}
    p = brain._miss_prompt(cand, 0.85)
    assert "+85%" in p and "Default-to-SKIP" in p and "survival" in p.lower()   # the over-trading guard
    assert "18000" in p and "REJECTED by a hard safety/market gate" in p
    cand["rule_passed"] = 1
    assert "PASSED the safety gate" in brain._miss_prompt(cand, 0.85)            # the other branch


def test_reflect_miss_stores_clamped_lesson_and_recalls():
    class FakeLLM:
        name = "fake"; supports_escalation = False
        async def reflect(self, system, prompt):
            assert "MISS" in prompt
            return {"lesson": "When liquidity>15k and buy/sell>2 with a rising trend, lean to a small starter.",
                    "tag": "global", "confidence": 0.9}

    st = Storage(":memory:"); st.connect(); mem = AgentMemory(st)

    async def run():
        await mem.load()
        brain = TradingBrain(Settings(), mem, FakeLLM())
        cand = {"symbol": "TKN", "mint": "m", "mode": "hold", "score": 0.45, "rule_passed": 1, "features": "{}"}
        note = await brain.reflect_miss(cand, 0.8)
        assert note is not None and note.lesson
        conf = st._conn.execute("SELECT confidence FROM lessons ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert conf <= 0.7                                          # a miss is damped vs a realized loss
        c = Candidate(mint="x"); c.mode = "scalp"
        assert any("small starter" in x for x in mem.recall(c))     # global tag -> recalled for any candidate

    asyncio.run(run())


def test_reflect_miss_no_llm_is_noop():
    st = Storage(":memory:"); st.connect()
    brain = TradingBrain(Settings(), AgentMemory(st), None)

    async def run():
        cand = {"symbol": "T", "mint": "m", "mode": "hold", "score": 0.4, "rule_passed": 1, "features": "{}"}
        assert await brain.reflect_miss(cand, 0.8) is None
        assert st._conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0] == 0

    asyncio.run(run())


def test_duallocal_create_real_resolution():
    # drive the REAL OllamaLLM._create_named via a faked /api/tags (not by stubbing the method),
    # so the strict-not-pulled, same-family-collision, and distinct-models branches all run.
    import httpx
    from memebot.agent.local_llm import DualLocalLLM, OllamaLLM

    class _Resp:
        def __init__(self, names): self._names = names
        def raise_for_status(self): pass
        def json(self): return {"models": [{"name": n} for n in self._names]}

    class _Client:
        names: list = []
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): return _Resp(_Client.names)

    saved = httpx.Client
    try:
        httpx.Client = _Client
        _Client.names = ["gemma3:4b"]                  # confirm family not pulled -> strict None -> single
        r1 = DualLocalLLM.create(Settings(ollama_model="gemma3:4b", ollama_confirm_model="qwen3:8b"))
        assert isinstance(r1, OllamaLLM) and r1.supports_escalation is False
        _Client.names = ["gemma3:latest"]             # both names family-resolve to the SAME pulled model -> single
        r2 = DualLocalLLM.create(Settings(ollama_model="gemma3:4b", ollama_confirm_model="gemma3:27b"))
        assert isinstance(r2, OllamaLLM) and not isinstance(r2, DualLocalLLM)
        _Client.names = ["gemma3:4b", "qwen3:8b"]      # distinct models -> real dual cascade
        r3 = DualLocalLLM.create(Settings(ollama_model="gemma3:4b", ollama_confirm_model="qwen3:8b"))
        assert isinstance(r3, DualLocalLLM)
        assert r3.base_source == "ollama:gemma3:4b" and r3.escalated_source == "ollama:qwen3:8b"
    finally:
        httpx.Client = saved


def test_brain_source_label_local_dual_budget_denied():
    # supports_escalation=True but the per-minute budget is exhausted by the initial allow() ->
    # the deep model must NOT be called and the verdict keeps the FAST/base source label.
    class FakeDual:
        name = "ollama-dual"; supports_escalation = True
        base_source = "ollama:gemma3:4b"; escalated_source = "ollama:qwen3:8b"
        def __init__(self): self.escalated = False
        async def decide(self, system, prompt, *, escalate=False):
            if escalate:
                self.escalated = True
                return {"action": "buy", "mode": "scalp", "conviction": 0.80, "size_pct": 0.6, "reasoning": "deep"}
            return {"action": "buy", "mode": "scalp", "conviction": 0.40, "size_pct": 0.5, "reasoning": "screen"}
        async def reflect(self, system, prompt): return None

    st = Storage(":memory:"); st.connect()
    fake = FakeDual()

    async def run():
        brain = TradingBrain(Settings(llm_per_min=1), AgentMemory(st), fake,
                             entry_threshold=0.5, high_conf_threshold=0.8)
        await brain.memory.load()
        c = Candidate(mint="m"); c.mode = "scalp"; c.score = 0.6
        v = await brain.decide(c)
        assert v.action == "buy" and v.source == "ollama:gemma3:4b"   # base label kept (no escalation)
        assert fake.escalated is False                                # deep model never invoked

    asyncio.run(run())


def test_validate_miss_learning_invariants():
    assert not any("replay window" in w or "miss_win_threshold_pct" in w or "miss_batch_size" in w
                   for w in Settings().validate())                                       # defaults clean
    assert any("replay window" in w for w in
               Settings(miss_min_age_s=4000.0, miss_max_age_s=300.0).validate())         # empty window
    assert any("miss_win_threshold_pct" in w for w in Settings(miss_win_threshold_pct=0.0).validate())
    assert any("miss_batch_size" in w for w in Settings(miss_batch_size=40).validate())   # > 30
    assert any("miss_batch_size" in w for w in Settings(miss_batch_size=0).validate())    # < 1 silent-disable
    # disabled master switch -> no miss warnings even with bad values
    assert not any("replay window" in w for w in
                   Settings(miss_learn_enabled=False, miss_min_age_s=9999.0, miss_max_age_s=1.0).validate())
