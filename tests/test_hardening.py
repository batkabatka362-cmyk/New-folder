"""Hardening edge cases: config validation + degradation/robustness paths."""
from __future__ import annotations

import time

from memebot.config import RiskLimits, Settings
from memebot.data.candles import CandleBuilder
from memebot.data.indicators import ema, sma, volume_spike_ratio
from memebot.data.token_state import TokenRegistry, TokenState
from memebot.execution.base import Fill
from memebot.models import NewTokenEvent, TradeEvent
from memebot.portfolio.portfolio import Portfolio


def test_config_validate():
    assert Settings().validate() == []                         # defaults are sane
    bad = Settings(high_conf_threshold=0.4, entry_threshold=0.55)
    assert any("never be consulted" in m for m in bad.validate())
    assert any("initial_sol" in m for m in Settings(risk=RiskLimits(initial_sol=0.0)).validate())
    assert any("PUMPPORTAL_API_KEY" in m
               for m in Settings(trade_stream_enabled=True, pumpportal_api_key="").validate())
    # rule-scorer calibration guards
    assert any("rule_score_ref_hi" in m
               for m in Settings(rule_score_ref_lo=0.3, rule_score_ref_hi=0.2).validate())
    assert any("rule_high_conf_threshold" in m
               for m in Settings(rule_high_conf_threshold=1.0).validate())
    # W2 guards
    from memebot.portfolio.portfolio import ExitParams
    assert any("liq_collapse_frac" in m for m in Settings(exit=ExitParams(liq_collapse_frac=0.0)).validate())
    assert any("max_price_misses" in m for m in Settings(max_price_misses=0).validate())


_ENV_KEEP = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC",
             "HOMEPATH", "HOMEDRIVE", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
             "PROGRAMFILES", "PROGRAMDATA", "PYTHONPATH", "PYTHONHOME", "OS"}


def _flatten_settings(obj, prefix=""):
    import dataclasses as dc
    out = {}
    for f in dc.fields(obj):
        v = getattr(obj, f.name)
        if dc.is_dataclass(v):
            out.update(_flatten_settings(v, prefix + f.name + "."))
        else:
            out[prefix + f.name] = v
    return out


def test_load_defaults_match_dataclass_defaults():
    """Footgun guard: Settings.load()'s env-var fallbacks must equal the dataclass
    field defaults. A mismatch (e.g. the old ENTRY_THRESHOLD 0.55 vs 0.50) silently
    overrides config so tuning the dataclass never reaches the running bot."""
    import os
    saved = dict(os.environ)
    try:
        for k in list(os.environ):                 # strip any config override, restore in finally
            if k.isupper() and k not in _ENV_KEEP:
                del os.environ[k]
        a, b = _flatten_settings(Settings()), _flatten_settings(Settings.load())
        diff = {k: (a[k], b.get(k)) for k in a if a[k] != b.get(k)}
        assert not diff, f"load() defaults diverge from dataclass defaults: {diff}"
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_apply_buy_insufficient_balance():
    pf = Portfolio(0.1)
    buy = Fill("m", "buy", 1000.0, 0.5, 5e-4, 0.01, 0.03)      # needs 0.5 SOL > 0.1 balance
    assert pf.apply_buy(buy, symbol="X", mode="scalp") is False
    assert "m" not in pf.positions and pf.sol_balance == 0.1   # untouched


def test_registry_evict_stale():
    reg = TokenRegistry(ttl_s=1.0)
    reg.on_new_token(NewTokenEvent(mint="old", ts=time.time() - 100))
    reg.on_new_token(NewTokenEvent(mint="fresh"))
    assert len(reg) == 2
    reg.evict_stale()
    assert reg.get("old") is None and reg.get("fresh") is not None


def test_candle_builder():
    cb = CandleBuilder(interval_s=10)
    cb.add(100.0, 1.0, 5.0)        # bucket 100
    cb.add(102.0, 1.5, 3.0)        # same bucket -> update OHLCV
    cb.add(115.0, 2.0, 1.0)        # bucket 110 -> new candle
    assert len(cb.candles) == 2
    c0 = cb.candles[0]
    assert (c0.open, c0.high, c0.low, c0.close) == (1.0, 1.5, 1.0, 1.5)
    assert c0.volume_sol == 8.0 and c0.trades == 2
    assert cb.closes() == [1.5, 2.0]


def test_indicators_guarded():
    assert sma([1, 2, 3, 4], 2) == 3.5
    assert sma([], 3) == 0.0 and sma([1, 2], 0) == 0.0      # guards
    assert ema([5, 5, 5], 3) == 5.0 and ema([1, 2, 3], 0) == 0.0
    assert volume_spike_ratio([1, 1, 1, 4], 3) == 4.0       # 4 / avg([1,1,1])
    assert volume_spike_ratio([5], 3) == 0.0               # too few points


def test_compounding_position_size():
    from memebot.risk.limits import RiskManager

    class PF:
        sol_balance = 100.0
        positions: dict = {}

    # explicit risk params (defensive defaults are now 0.04 / 1.0) so this tests the LOGIC
    rm = RiskManager(Settings(risk=RiskLimits(
        risk_per_trade_frac=0.05, max_position_sol=2.0, max_liquidity_frac=0.02)))
    # equity 100 * 0.05 = 5.0, capped by absolute ceiling 2.0
    assert rm.position_size_sol(PF(), equity=100.0, frac=1.0) == 2.0
    # equity grows -> size grows (compounding) up to the ceiling
    assert abs(rm.position_size_sol(PF(), equity=4.0, frac=1.0) - 0.2) < 1e-9    # 5% of 4
    assert abs(rm.position_size_sol(PF(), equity=8.0, frac=1.0) - 0.4) < 1e-9    # doubled equity -> doubled size
    # brain size fraction scales it
    assert abs(rm.position_size_sol(PF(), equity=4.0, frac=0.5) - 0.1) < 1e-9
    # liquidity cap bounds it (1 SOL pool * 0.02)
    assert abs(rm.position_size_sol(PF(), equity=4.0, frac=1.0, liquidity_sol=1.0) - 0.02) < 1e-9
    # cash-balance cap
    PF.sol_balance = 0.05
    assert abs(rm.position_size_sol(PF(), equity=4.0, frac=1.0) - 0.05 * 0.95) < 1e-9


def test_total_exposure_cap_shrinks_size():
    from memebot.portfolio.portfolio import Position
    from memebot.risk.limits import RiskManager

    class PF:
        sol_balance = 100.0
        positions = {"a": Position("a", "A", "scalp", qty=1, cost_sol=1.2)}   # 1.2 SOL deployed

    rm = RiskManager(Settings(risk=RiskLimits(
        risk_per_trade_frac=0.5, max_position_sol=10.0, max_total_exposure_frac=0.15)))
    # equity 10 -> exposure budget 1.5; 1.2 already deployed -> only 0.3 of room left
    assert abs(rm.position_size_sol(PF(), equity=10.0, frac=1.0) - 0.3) < 1e-9
    PF.positions = {"a": Position("a", "A", "scalp", qty=1, cost_sol=1.5)}    # fully deployed
    assert rm.position_size_sol(PF(), equity=10.0, frac=1.0) == 0.0           # no room -> no buy


def test_exposure_cap_uses_current_value_not_cost():
    from memebot.portfolio.portfolio import Position
    from memebot.risk.limits import RiskManager

    class PF:
        sol_balance = 100.0
        positions = {"a": Position("a", "A", "scalp", qty=1.0, cost_sol=1.0)}   # cost 1.0, avg 1.0

    rm = RiskManager(Settings(risk=RiskLimits(
        risk_per_trade_frac=0.5, max_position_sol=10.0, max_total_exposure_frac=0.15)))
    # equity 10 -> budget 1.5. Valued at CURRENT 2.0 the position already exceeds budget -> no room
    assert rm.position_size_sol(PF(), equity=10.0, frac=1.0, price_map={"a": 2.0}) == 0.0
    # current price below cost frees room (cost-basis would have wrongly blocked/allowed differently)
    assert rm.position_size_sol(PF(), equity=10.0, frac=1.0, price_map={"a": 0.5}) > 0


def test_kelly_fraction():
    # W6: fractional-Kelly bet size. f* = p - (1-p)/b, b = tp/sl; half-Kelly = 0.5*f*; 0 on no edge.
    from memebot.risk.limits import kelly_fraction
    assert abs(kelly_fraction(0.6, 0.40, 0.20, 0.5) - 0.2) < 1e-9   # b=2, f*=0.4, half=0.2
    assert kelly_fraction(0.3, 0.40, 0.20, 0.5) == 0.0              # f*=-0.05 -> 0 (never scale a losing edge)
    assert kelly_fraction(0.99, 1.0, 0.1, 1.0, cap=0.5) == 0.5      # cap binds
    assert kelly_fraction(0.8, 0.0, 0.2) == 0.0                     # degenerate odds -> 0


def test_creator_history():
    # W5: per-creator launch tally (serial-spam/rug tell) — load from storage + increment on new tokens
    from memebot.data.creator_history import CreatorHistory
    h = CreatorHistory()
    h.load({"dev1": 700, "dev2": 1, "": 5})                  # empty creator key ignored
    assert h.launch_count("dev1") == 700 and h.launch_count("dev2") == 1
    assert h.launch_count("unknown") == 0 and h.launch_count("") == 0 and h.launch_count(None) == 0
    h.record("dev2"); h.record("dev3"); h.record(None)        # None is a no-op
    assert h.launch_count("dev2") == 2 and h.launch_count("dev3") == 1


def test_helius_supports_largest_accounts_probe():
    # W3: the public mainnet-beta RPC doesn't serve getTokenLargestAccounts (concentration dark);
    # an enhanced RPC (Helius) does. URL heuristic (a fixed-mint probe false-negatives on big mints).
    from memebot.feed.helius_rpc import HeliusRPC
    assert HeliusRPC("https://api.mainnet-beta.solana.com").supports_largest_accounts() is False
    assert HeliusRPC("https://mainnet.helius-rpc.com/?api-key=x").supports_largest_accounts() is True


def test_hard_per_trade_equity_ceiling_binds():
    # D5: a single trade can never exceed max_position_equity_frac of equity, EVEN IF every other
    # cap is cranked wide open (a future edit / aggressive config). This is the survival invariant.
    from memebot.risk.limits import RiskManager

    class PF:
        sol_balance = 1e9
        positions: dict = {}

    rm = RiskManager(Settings(risk=RiskLimits(
        risk_per_trade_frac=0.50,        # absurd per-trade frac
        max_position_sol=1e9,            # absolute ceiling effectively disabled
        max_total_exposure_frac=1.0,     # exposure ceiling disabled
        max_position_equity_frac=0.05)))
    # target would be 100*0.5=50, but the hard ceiling clamps to 100*0.05 = 5.0
    assert abs(rm.position_size_sol(PF(), equity=100.0, frac=1.0, liquidity_sol=1e9) - 5.0) < 1e-9
    # and it TIGHTENS automatically with drawn-down equity (5% of 20 = 1.0) — unlike the absolute cap
    assert abs(rm.position_size_sol(PF(), equity=20.0, frac=1.0, liquidity_sol=1e9) - 1.0) < 1e-9


def test_sol_at_risk_bounds_within_daily_cap():
    from memebot.risk.limits import RiskManager
    rm = RiskManager(Settings())                                  # defensive defaults
    rep = rm.sol_at_risk(10.0)
    # per-trade worst case = min(max_position_sol 1.0, 5% of 10 = 0.5) = 0.5
    assert abs(rep["per_trade_sol"] - 0.5) < 1e-9 and abs(rep["per_trade_pct"] - 0.05) < 1e-9
    # W2 defaults: whole-book rug = min(0.5*5, 20% of 10) = min(2.5, 2.0) = 2.0, capped by the
    # 20% exposure term; 2.0 > the 1.5 daily cap -> book_within_daily_cap False (deliberate: the
    # daily breaker is KEPT tight at 1.5 for survival while concurrency rose).
    assert abs(rep["max_book_rug_sol"] - 2.0) < 1e-9 and rep["book_within_daily_cap"] is False
    # equity=0 (wiped account, exactly when this report matters) -> no division-by-zero, bounds 0
    rep0 = rm.sol_at_risk(0.0)
    assert rep0["per_trade_sol"] == 0.0 and rep0["per_trade_pct"] == 0.0
    assert rep0["max_book_rug_pct"] == 0.0 and rep0["book_within_daily_cap"] is True   # 0 < cap
    # disambiguate the two min() terms: per-trade term dominates, well under a roomy cap -> True
    rm_pt = RiskManager(Settings(risk=RiskLimits(max_positions=3, max_total_exposure_frac=0.30, daily_loss_cap_sol=2.0)))
    rpt = rm_pt.sol_at_risk(10.0)        # min(0.5*3=1.5, 10*0.30=3.0)=1.5 < 2.0
    assert abs(rpt["max_book_rug_sol"] - 1.5) < 1e-9 and rpt["book_within_daily_cap"] is True
    # exposure term dominates AND exceeds the cap -> the alarm (False) branch
    rm_ex = RiskManager(Settings(risk=RiskLimits(max_position_sol=10.0, max_position_equity_frac=1.0,
                                                 max_total_exposure_frac=0.50, daily_loss_cap_sol=1.5)))
    rex = rm_ex.sol_at_risk(10.0)        # min(10*3=30, 10*0.50=5.0)=5.0 > 1.5
    assert abs(rex["max_book_rug_sol"] - 5.0) < 1e-9 and rex["book_within_daily_cap"] is False
    # config guard: a per-trade frac ABOVE the ceiling (would clip normal sizing) is flagged
    assert any("max_position_equity_frac" in m for m in
               Settings(risk=RiskLimits(risk_per_trade_frac=0.2, max_position_equity_frac=0.05)).validate())


def test_consecutive_loss_halt_and_recovery():
    from memebot.risk.limits import RiskManager
    rm = RiskManager(Settings(risk=RiskLimits(max_consecutive_losses=3)))
    rm.on_loss("m"); rm.on_loss("m")
    assert not rm.halted                                   # 2 losses < threshold 3
    rm.on_loss("m")
    assert rm.halted and rm.halt_reason == "loss_streak"   # 3rd in a row trips it
    rm.on_win()                                            # a winning close lifts it + resets
    assert not rm.halted and rm._consecutive_losses == 0
    rm0 = RiskManager(Settings(risk=RiskLimits(max_consecutive_losses=0)))
    for _ in range(10):
        rm0.on_loss("m")
    assert not rm0.halted                                  # 0 = disabled


def test_loss_streak_clears_on_day_rollover():
    from memebot.risk.limits import RiskManager
    rm = RiskManager(Settings(risk=RiskLimits(max_consecutive_losses=2)))

    class PF:
        def daily_loss(self, pm, now=None): return 0.0
        def has_position(self, m): return False
        def open_count(self): return 0

    pf = PF()
    rm.can_open(pf, {}, "x", now=10 * 86_400.0)             # set _day=10
    rm.on_loss("m"); rm.on_loss("m")
    assert rm.halted and rm.halt_reason == "loss_streak"
    assert rm.can_open(pf, {}, "x", now=10 * 86_400.0 + 3600)[0] is False   # same day -> still halted
    ok, _ = rm.can_open(pf, {}, "x", now=11 * 86_400.0)    # next UTC day -> streak halt clears
    assert ok and not rm.halted and rm._consecutive_losses == 0


def test_price_trend_directions():
    from memebot.data.token_state import TokenState
    st = TokenState("m")
    assert st.price_trend()["dir"] == "unknown"            # <3 obs
    for p in [1.0, 1.1, 1.2, 1.3, 1.5]:
        st.record_price(p)
    up = st.price_trend()
    assert up["dir"] == "rising" and up["chg_pct"] > 0 and up["off_high_pct"] == 0.0
    st2 = TokenState("m2")
    for p in [2.0, 1.8, 1.5, 1.2, 1.0]:
        st2.record_price(p)
    down = st2.price_trend()
    assert down["dir"] == "falling" and down["chg_pct"] < 0 and down["off_high_pct"] > 0
    st2.record_price(0.0)                                  # non-positive ignored
    assert st2.price_trend()["n"] == 5


def test_technicals_ema_breakout():
    from memebot.data.token_state import TokenState
    st = TokenState("m")
    assert st.technicals()["ema_signal"] == "n/a"          # <5 obs -> warming up
    for p in [1.0, 1.05, 1.1, 1.2, 1.35, 1.5]:             # steady uptrend, new high last
        st.record_price(p)
    t = st.technicals()
    assert t["ema_signal"] == "bull"                       # fast EMA above slow on an uptrend
    assert t["breakout"] == "up"                           # last 1.5 > prior max 1.35
    assert t["res_dist_pct"] <= 0.0 and t["sup_dist_pct"] > 0.0


def test_token_state_windowed_metrics():
    st = TokenState("m")
    st.apply_trade(TradeEvent(mint="m", trader="A", side="buy", sol_amount=1.0, token_amount=1000))
    st.apply_trade(TradeEvent(mint="m", trader="B", side="buy", sol_amount=2.0, token_amount=1000))
    st.apply_trade(TradeEvent(mint="m", trader="A", side="sell", sol_amount=0.5, token_amount=500))
    assert st.unique_buyers(60.0) == 2                     # A and B (sellers don't count)
    assert st.buy_sell_counts(60.0) == (2, 1)
    assert abs(st.volume_sol(60.0) - 3.5) < 1e-9
    assert st.last_price_sol > 0


# ── P9 production-harden audit fixes ──────────────────────────────────────────
def test_safety_momentum_env_overrides_take_effect():
    """P9 #6: SafetyGates / MomentumThresholds / the 3 late RiskLimits knobs were never
    read from env in load(), so .env overrides silently did nothing despite the docs."""
    import os
    saved = dict(os.environ)
    try:
        for k in list(os.environ):
            if k.isupper() and k not in _ENV_KEEP:
                del os.environ[k]
        os.environ.update({
            "MAX_TOP5_CONCENTRATION_PCT": "80", "REQUIRE_MINT_REVOKED": "false",
            "MIN_VOL_H1": "2500", "MIN_UNIQUE_BUYERS": "30",
            "PER_TOKEN_COOLDOWN_S": "600", "MIN_LIQUIDITY_USD": "5000",
            "MAX_MODELED_SLIPPAGE_PCT": "10",
        })
        s = Settings.load()
        assert s.safety.max_top5_concentration_pct == 80.0
        assert s.safety.require_mint_revoked is False
        assert s.momentum.min_vol_h1 == 2500.0
        assert s.momentum.min_unique_buyers == 30
        assert s.risk.per_token_cooldown_s == 600.0
        assert s.risk.min_liquidity_usd == 5000.0
        assert s.risk.max_modeled_slippage_pct == 10.0
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_state_growth_caps_bound_and_preserve_signal():
    """P9 #4/#5/#11/#13: the hot-path registries must bound their growth without losing
    the high-signal entries (serial-spam counts, reuse counts, exact funder cluster sizes)."""
    from memebot.constants import SIDE_BUY, SIDE_SELL
    from memebot.data.creator_history import CreatorHistory
    from memebot.data.funder_history import FunderRegistry
    from memebot.data.metadata_history import MetadataRegistry
    from memebot.data.smart_money import SmartMoney

    ch = CreatorHistory(max_creators=100)
    for i in range(500):
        ch.record(f"creator{i}")
    for _ in range(61):
        ch.record("SPAMMER")
    assert len(ch._counts) <= 100
    assert ch.launch_count("SPAMMER") == 61                # high-launch signal preserved

    mr = MetadataRegistry(max_keys=100)
    for i in range(500):
        mr.record(f"name{i}", f"sym{i}")
    for _ in range(20):
        mr.record("PEPE", "PEPE")
    assert len(mr._counts) <= 100
    assert mr.reuse_count("PEPE", "PEPE") == 20            # reuse>1 duplication signal preserved

    fr = FunderRegistry(max_creators=100)
    for i in range(300):
        fr.record(f"c{i}", f"singlefunder{i}")            # 300 size-1 (no-signal) clusters
    for j in range(5):
        fr.record(f"bigc{j}", "BIGFUNDER")               # one size-5 (signal) cluster
    assert len(fr._funder_of) <= 100
    assert fr.funder_creator_count("bigc0") == 5          # remaining cluster size stays EXACT

    sm = SmartMoney(max_open=100)
    for i in range(500):
        sm.on_trade(f"w{i}", f"m{i}", SIDE_BUY, 1.0, 100.0)   # never-closed opens
    assert len(sm._open) <= 100
    sm.on_trade("T", "M", SIDE_BUY, 1.0, 100.0)
    sm.on_trade("T", "M", SIDE_SELL, 1.5, 100.0)
    assert sm._wallet["T"]["closed"] == 1                 # realized-PnL book unaffected by the open cap
    assert abs(sm._wallet["T"]["pnl"] - 0.5) < 1e-9


def test_loads_rejects_non_dict_json():
    """P9 #9: a model emitting valid-but-non-object JSON must degrade to None (-> rule
    fallback), not return a list/scalar that makes Verdict.from_dict raise AttributeError."""
    from memebot.agent.local_llm import _loads
    assert _loads('{"action": "buy"}') == {"action": "buy"}
    assert _loads("[1, 2, 3]") is None
    assert _loads("42") is None
    assert _loads('"buy"') is None
    assert _loads("") is None
    assert _loads("prefix [not an object] suffix") is None


def test_schema_migrate_reconciles_missing_columns():
    """P9 #10: a generic forward-migration must ADD every column the current _SCHEMA defines
    but an older DB file lacks, so a writer naming a later-added column never hits 'no such column'."""
    import os
    import sqlite3
    import tempfile

    from memebot.storage.db import Storage

    path = os.path.join(tempfile.mkdtemp(), "old.db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE observations (id INTEGER PRIMARY KEY, ts REAL, mint TEXT, symbol TEXT)")
    old.execute("CREATE TABLE candidates (id INTEGER PRIMARY KEY, ts REAL, mint TEXT)")  # missing features
    old.execute("INSERT INTO observations(ts,mint,symbol) VALUES(1.0,'OLD','O')")
    old.commit()
    old.close()

    s = Storage(path)
    s.connect()
    try:
        obs_cols = {r[1] for r in s._conn.execute("PRAGMA table_info(observations)").fetchall()}
        cand_cols = {r[1] for r in s._conn.execute("PRAGMA table_info(candidates)").fetchall()}
        assert {"price_usd", "liquidity_usd", "features", "rule_passed", "score"} <= obs_cols
        assert "features" in cand_cols
        idx = {r[0] for r in s._conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        assert "idx_obs_ts" in idx                                # P9 #3 index present
        # a writer naming the migrated columns must succeed (would raise OperationalError if not migrated)
        s._conn.execute(
            "INSERT INTO observations(ts,mint,symbol,mode,rule_passed,score,price_usd,liquidity_usd,"
            "market_cap_usd,vol_h1,features) VALUES(2.0,'NEW','N','hold',1,0.5,1.0,1e3,5e3,200.0,'{}')"
        )
        assert s._conn.execute("SELECT symbol FROM observations WHERE mint='OLD'").fetchone()[0] == "O"
    finally:
        s.close()


def test_first_completed_detects_dead_task_among_infinite_loops():
    """P9 #1: asyncio.wait(FIRST_COMPLETED) surfaces a dying task while its infinite-loop
    siblings keep running — the detection gather(return_exceptions=True) cannot provide (it
    would block forever on the survivors). The legitimately-finite task (watchlist analogue)
    is excluded from the sentinel set so its normal completion never triggers a teardown."""
    import asyncio

    async def scenario():
        async def forever():
            while True:
                await asyncio.sleep(0.01)

        async def dies():
            await asyncio.sleep(0.02)
            raise RuntimeError("eval loop crashed")

        async def watchlist_returns():          # excluded sentinel: returns normally (G3 unavailable)
            await asyncio.sleep(0.001)
            return "done"

        f1 = asyncio.create_task(forever(), name="eval")
        f2 = asyncio.create_task(forever(), name="manage")
        dead = asyncio.create_task(dies(), name="ingest")
        watch = asyncio.create_task(watchlist_returns(), name="watchlist")
        tasks = [f1, f2, dead, watch]
        sentinels = [t for t in tasks if t is not watch]
        done, pending = await asyncio.wait(sentinels, return_when=asyncio.FIRST_COMPLETED)
        t = next(iter(done))
        exc = t.exception() if not t.cancelled() else None
        for x in tasks:
            x.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return t.get_name(), isinstance(exc, RuntimeError), len(pending)

    name, is_crash, n_pending = asyncio.run(scenario())
    assert name == "ingest"            # the dead task is the one surfaced (not the forever siblings)
    assert is_crash is True            # its exception is retrievable -> would drive SystemExit(1)
    assert n_pending == 2              # the two infinite loops are still pending, never awaited to completion


# ── P10 intelligence-layer assessment fixes ───────────────────────────────────
def test_primary_signal_enum_clamped():
    """P10 #10: primary_signal is case-normalized + clamped to the allowed set; default 'none'."""
    from memebot.agent.schema import Verdict
    assert Verdict.from_dict({"primary_signal": "HOLDER_CONCENTRATION"}, "rule").primary_signal == "holder_concentration"
    assert Verdict.from_dict({"primary_signal": "make_money_fast"}, "rule").primary_signal == "none"
    assert Verdict().primary_signal == "none"             # rule-fallback default


def test_escalation_predicate_targets_riskiest_buys():
    """P10 #2: escalate low-conviction OR flagged OR rug-lesson-contradicting BUYS; never skips."""
    from memebot.agent.brain import TradingBrain
    from memebot.agent.memory import AgentMemory
    from memebot.agent.schema import Verdict
    b = TradingBrain(Settings(), AgentMemory(":memory:"), llm=None)
    assert b._should_escalate(Verdict(action="buy", conviction=0.4), []) is True              # low conviction
    assert b._should_escalate(Verdict(action="buy", conviction=0.9), []) is False             # confident + clean
    assert b._should_escalate(Verdict(action="buy", conviction=0.9, risk_flags=["x"]), []) is True   # flagged
    assert b._should_escalate(Verdict(action="buy", conviction=0.9), ["it RUGGED on us"]) is True    # rug lesson
    assert b._should_escalate(Verdict(action="skip", conviction=0.3), []) is False            # skips never escalate
    # P10 #1: the escalation addendum hands the deep model the screener verdict to arbitrate, skeptically
    add = b._escalation_addendum(Verdict(action="buy", conviction=0.5, primary_signal="trend"))
    assert "SECOND-OPINION" in add and "skeptical" in add.lower() and "conviction=0.50" in add


def test_risk_flags_and_mfe_mae_carried_to_closed_trade():
    """P10 #4/#11: entry risk_flags + peak(MFE)/trough(MAE) survive the round-trip into ClosedTrade."""
    from memebot.execution.base import Fill
    from memebot.portfolio.portfolio import ExitParams, Portfolio
    pf = Portfolio(10.0)
    buy = Fill("m", "buy", 1000.0, 1.0, 0.001, 0.01, 0.03)        # entry price 0.001
    assert pf.apply_buy(buy, symbol="X", mode="hold", risk_flags=["low_liquidity", "few_buyers"])
    pos = pf.positions["m"]
    params = ExitParams()
    pos.should_exit(0.002, params)                                # peak 0.002 -> +100% MFE
    pos.should_exit(0.0005, params)                               # trough 0.0005 -> -50% MAE
    pf.apply_sell(Fill("m", "sell", 1000.0, 1.2, 0.0012, 0.01, 0.03), reason="tp")
    ct = pf.closed[-1]
    assert ct.entry_risk_flags == ["low_liquidity", "few_buyers"]
    assert abs(ct.peak_pct - 1.0) < 1e-6                          # +100%
    assert abs(ct.trough_pct - (-0.5)) < 1e-6                     # -50%


def test_reflect_prompt_surfaces_flags_and_excursion():
    """P10 #4/#11: reflection shows the at-entry flags + peak/drawdown so exit-timing is learnable."""
    from memebot.agent.brain import TradingBrain
    from memebot.agent.memory import AgentMemory
    from memebot.portfolio.portfolio import ClosedTrade
    b = TradingBrain(Settings(), AgentMemory(":memory:"), llm=None)
    ct = ClosedTrade(mint="m", symbol="X", mode="hold", cost_sol=1.0, proceeds_sol=1.1,
                     pnl_sol=0.1, pnl_pct=0.1, opened_ts=0.0, closed_ts=100.0, reason="tp",
                     entry_note="momentum", entry_risk_flags=["low_liquidity"], peak_pct=0.8, trough_pct=-0.2)
    p = b._reflect_prompt(ct)
    assert "low_liquidity" in p and "+80%" in p and "-20%" in p and "EXIT TIMING" in p


def test_constitution_has_p10_reasoning_cues():
    """P10 #3/#5/#10: the CONSTITUTION carries the base-rate anchor, size/flag self-consistency, primary_signal."""
    from memebot.agent.policy import CONSTITUTION
    assert "HOSTILE" in CONSTITUTION and "posterior" in CONSTITUTION   # #3 hostile base-rate anchor
    assert "Self-consistency" in CONSTITUTION              # #5 size <-> risk_flag coupling
    assert "primary_signal" in CONSTITUTION                # #10 cite the decisive signal


# ── P10b intelligence re-verify batch (measurement / miss-regret / calibration) ──
def test_verdict_source_conviction_carried_to_closed_trade():
    """P10b #1: the decider source + conviction survive into ClosedTrade (brain-vs-rule A/B key)."""
    from memebot.execution.base import Fill
    from memebot.portfolio.portfolio import Portfolio
    pf = Portfolio(10.0)
    pf.apply_buy(Fill("m", "buy", 1000.0, 1.0, 0.001, 0.01, 0.03), symbol="X", mode="hold",
                 verdict_source="sonnet", verdict_conviction=0.72)
    pf.apply_sell(Fill("m", "sell", 1000.0, 1.1, 0.0011, 0.01, 0.03), reason="tp")
    ct = pf.closed[-1]
    assert ct.verdict_source == "sonnet" and abs(ct.verdict_conviction - 0.72) < 1e-9


def test_recall_caps_miss_origin_lessons():
    """P10b #2: 'miss'-origin regret lessons are bounded in recall so they can't crowd out survival lessons."""
    from memebot.agent.memory import AgentMemory
    from memebot.models import Candidate
    m = AgentMemory(":memory:", miss_recall_frac=0.34)            # cap = floor(6*0.34) = 2
    m._cache = ([{"lesson": f"miss{i}", "tag": "miss-global", "score": 10 - i} for i in range(4)]
                + [{"lesson": f"surv{i}", "tag": "global", "score": 3 - i} for i in range(3)])
    c = Candidate(mint="x"); c.mode = "hold"
    out = m.recall(c, k=6)
    assert sum(1 for x in out if x.startswith("miss")) <= 2       # miss-lessons capped
    assert any(x.startswith("surv") for x in out)                 # survival lessons back-fill the slots


def test_creator_reputation_recency_decay():
    """P10b CAL3: stale rugs decay so a reformed/active creator's recency-weighted rug rate is lower."""
    from memebot.backtest.calibrate import creator_reputation
    recs = [{"mint": "a", "outcome": "rug", "t0": 0.0}, {"mint": "b", "outcome": "rug", "t0": 0.0},
            {"mint": "c", "outcome": "winner", "t0": 1_000_000.0}]
    mc = {"a": "CRE", "b": "CRE", "c": "CRE"}
    rep = creator_reputation(recs, mc, min_tokens=3, now_ts=1_000_000.0, half_life_s=604_800.0)["CRE"]
    assert rep["rug_rate"] == 2 / 3                               # raw count unchanged
    assert rep["rug_rate_recent"] < rep["rug_rate"]              # stale rugs decayed


def test_suggest_thresholds_detects_signed_velocity():
    """P10b CAL2 (REAL path): a signed velocity separator must emit a suggestion through the actual
    build_dataset -> summarize -> suggest_thresholds chain. A hand-built summary hid a producer/consumer
    bug (summarize() didn't compute the velocity features' medians, so CAL2 was dead code)."""
    from memebot.backtest.calibrate import suggest_thresholds
    from memebot.backtest.dataset import build_dataset, summarize
    rows = []
    for i in range(6):                                   # winners: +120% forward, held; m5 strongly +
        feats = {"liquidity_usd": 1e4, "price_change_m5": 5.0}
        rows += [(f"w{i}", 0.0, 1.0, feats), (f"w{i}", 100.0, 2.2, feats)]
    for i in range(6):                                   # rugs: -90% forward; m5 strongly - (rolling over)
        feats = {"liquidity_usd": 1e4, "price_change_m5": -8.0}
        rows += [(f"r{i}", 0.0, 1.0, feats), (f"r{i}", 100.0, 0.1, feats)]
    summary = summarize(build_dataset(rows, horizon_s=300, tol_s=900))
    assert "price_change_m5" in summary["classes"]["winner"]   # the producer now carries its median
    feats = [s["feature"] for s in suggest_thresholds(summary, min_class_n=5, min_sep=0.2)]
    assert "price_change_m5" in feats                          # ...so the consumer can act on it


def test_selectivity_sweep_counts_avoided_losers():
    """P10b #15: raising the entry bar drops sub-threshold losers (counterfactual on realized trades)."""
    from memebot.backtest.selectivity import sweep
    oc = [(0.5, -0.1), (0.55, -0.15), (0.7, 0.3), (0.9, 1.0)]
    rows = sweep(oc, steps=4)
    top = [r for r in rows if r["threshold"] >= 0.69][0]
    assert top["losers_avoided"] == 2 and top["retained"] == 2


# ── P10b signal-richness + setup-orchestration batch ──────────────────────────
def test_feature_names_has_res_sup_dist_appended():
    """P10b SR6: res/sup_dist appended at the END of FEATURE_NAMES (prefix-trained artifacts stay
    valid); build_features keys match exactly; an OLD row missing them defaults serve-consistently."""
    from memebot.filter.features import FEATURE_NAMES, FEATURE_DEFAULTS, build_features, row_from_feats
    from memebot.models import Candidate
    assert FEATURE_NAMES[-2:] == ["res_dist_pct", "sup_dist_pct"]
    c = Candidate(mint="x"); c.mode = "hold"; c.features["tech"] = {"res_dist_pct": 5.0, "sup_dist_pct": 3.0}
    f = build_features(c)
    assert set(f.keys()) == set(FEATURE_NAMES) and f["res_dist_pct"] == 5.0
    old = {n: 1.0 for n in FEATURE_NAMES if n not in ("res_dist_pct", "sup_dist_pct")}
    assert row_from_feats(old)[-2:] == [0.0, 0.0]              # serve-consistent default for old rows


def test_sell_velocity_and_m5_breadth():
    """P10b SR4/SR5: 5m txn breadth parses; sell-count velocity mirrors buyer velocity."""
    from memebot.data.token_state import TokenState
    from memebot.feed.dexscreener import _to_pair
    snap = _to_pair({"baseToken": {"address": "M", "symbol": "X"},
                     "txns": {"h1": {"buys": 10, "sells": 5}, "m5": {"buys": 4, "sells": 1}},
                     "volume": {"h1": 1000, "h24": 5000}, "priceUsd": "1", "priceNative": "0.01",
                     "liquidity": {"usd": 9000}})
    assert snap.buys_m5 == 4 and snap.buy_sell_ratio_m5 == 4.0
    st = TokenState("m")
    for x in [5, 6, 8, 10, 14]:
        st.record_sells(x)
    assert st.sell_trend()["growth"] > 0                       # accelerating selling


def test_setup_type_rolling_over_not_gold():
    """P10b SO3: a rising-trend label that is rolling over (m5<0) is NOT promoted to creator_gold."""
    from memebot.models import Candidate
    from memebot.signals.setup import setup_type
    s = Settings()
    c = Candidate(mint="x"); c.mode = "hold"
    c.features["trend"] = {"dir": "rising"}; c.price_change_h1 = 10.0
    c.features["creator_holding_pct"] = 0.0; c.top5_concentration_pct = 50.0
    c.price_change_m5 = -2.0
    assert setup_type(c, s) != "creator_gold"
    c.price_change_m5 = 1.0
    assert setup_type(c, s) == "creator_gold"


def test_rule_verdict_risky_sizes_down():
    """P10b SO1: the rule fallback sizes a 'risky' setup down deterministically + flags it."""
    from memebot.agent.brain import TradingBrain
    from memebot.agent.memory import AgentMemory
    from memebot.models import Candidate
    b = TradingBrain(Settings(), AgentMemory(":memory:"), llm=None)
    c = Candidate(mint="x"); c.mode = "scalp"; c.score = 0.9
    v_plain = b._rule_verdict(c)
    c.features["setup_type"] = "risky"
    v_risky = b._rule_verdict(c)
    assert v_risky.size_pct < v_plain.size_pct and "risky_setup" in v_risky.risk_flags


def test_scorer_penalizes_offhigh_and_downtrend():
    """P10b SO4: the rule scorer ranks down off-high / falling candidates the setup gate distrusts."""
    from memebot.models import Candidate
    from memebot.signals.scoring import score_candidate
    s = Settings()

    def mk(dirn, offh):
        c = Candidate(mint="y"); c.mode = "scalp"; c.liquidity_usd = 4000; c.buy_sell_ratio = 1.6
        c.unique_buyers = 16; c.vol_to_mcap_pct = 11
        c.features = {"vol_h1": 1600, "vol_spike": 1.2, "trend": {"dir": dirn, "off_high_pct": offh}}
        return c
    base = score_candidate(mk("rising", 0.0), s)
    assert score_candidate(mk("rising", 30.0), s) < base       # off-high penalty
    assert score_candidate(mk("falling", 0.0), s) < base       # downtrend penalty


# ── A1: Token-2022 extension hard-fail ────────────────────────────────────────
def test_token2022_extension_risk_parse():
    """A1: dangerous Token-2022 extensions are detected; safe / classic-SPL tokens return None."""
    from memebot.feed.helius_rpc import HeliusRPC
    f = HeliusRPC._t2022_risk_from_account

    def acct(program, exts=None, typ="mint"):
        return {"value": {"data": {"program": program,
                "parsed": {"type": typ, "info": {"extensions": exts or []}}}}}

    # classic SPL token -> no Token-2022 risk
    assert f(acct("spl-token"), 5.0) is None
    # token-2022 with no dangerous extensions -> safe
    assert f(acct("spl-token-2022", [{"extension": "transferFeeConfig",
             "state": {"newerTransferFee": {"transferFeeBasisPoints": 100}}}]), 5.0) is None   # 1% < 5%
    # transfer hook (non-null program) -> HARD FAIL
    assert "transfer_hook" in f(acct("spl-token-2022",
             [{"extension": "transferHook", "state": {"programId": "Hook1111111111111111111111111111111111111111"}}]), 5.0)
    # permanent delegate -> HARD FAIL
    assert f(acct("spl-token-2022",
             [{"extension": "permanentDelegate", "state": {"delegate": "Deleg11111111111111111111111111111111111111"}}]), 5.0) == "token2022_permanent_delegate"
    # default account state frozen -> HARD FAIL
    assert f(acct("spl-token-2022",
             [{"extension": "defaultAccountState", "state": {"accountState": "frozen"}}]), 5.0) == "token2022_default_frozen"
    # high transfer fee (10% > 5% cap) -> HARD FAIL
    assert "transfer_fee" in f(acct("spl-token-2022",
             [{"extension": "transferFeeConfig", "state": {"newerTransferFee": {"transferFeeBasisPoints": 1000}}}]), 5.0)
    # a null-program transfer hook is harmless
    assert f(acct("spl-token-2022",
             [{"extension": "transferHook", "state": {"programId": None}}]), 5.0) is None
    # unparseable -> None (unknown, not a false veto)
    assert f({"value": None}, 5.0) is None


def test_token2022_risk_hard_vetoes_the_buy():
    """A1: a detected Token-2022 risk makes the rule gate fail (rule_passed=False)."""
    from memebot.filter.rules import evaluate
    from memebot.models import Candidate
    s = Settings()
    c = Candidate(mint="x"); c.mode = "scalp"
    c.liquidity_usd = 50000; c.market_cap_usd = 100000; c.vol_to_mcap_pct = 20
    c.buy_sell_ratio = 2.0; c.unique_buyers = 30; c.mint_revoked = True; c.freeze_revoked = True
    c.features["vol_h1"] = 5000
    c.token2022_risk = "token2022_transfer_hook(Hook1234)"
    evaluate(c, s)
    assert c.rule_passed is False and any("token2022" in r for r in c.rule_reasons)


# ── A2: Jupiter quote-based honeypot check ────────────────────────────────────
def test_honeypot_check_quote_logic():
    """A2: buys-but-won't-sell or extreme-tax = honeypot; safe round-trip or unroutable = None."""
    import asyncio
    from memebot.feed.jupiter import JupiterClient, SOL_MINT
    lam = int(0.05 * 1e9)

    def run(buy_out, sell_out):
        j = JupiterClient()

        async def fake_quote(inp, outp, amount, slippage_bps=1500):
            if inp == SOL_MINT:                       # BUY leg
                return {"outAmount": str(buy_out)} if buy_out else None
            return {"outAmount": str(sell_out)} if sell_out is not None else None   # SELL leg (None = no route)
        j._quote = fake_quote
        return asyncio.run(j.honeypot_check("M", sol_amount=0.05, min_roundtrip_keep=0.5))

    assert run(1_000_000, None) == "honeypot_no_sell_route"        # buys but won't sell
    assert run(1_000_000, int(lam * 0.9)) is None                  # 90% recovered -> safe
    assert "high_tax" in run(1_000_000, int(lam * 0.2))            # 20% recovered -> stealth tax
    assert run(0, None) is None                                    # can't even buy -> can't assess, NOT a veto


# ── A3: 2x take-initial asymmetric exit ───────────────────────────────────────
def test_take_initial_recovers_principal_and_rides_house_money():
    """A3: at 2x the take-initial recovers the full principal and leaves a house-money tail; its
    OWN latch composes with the P3 partial (independent triggers)."""
    from memebot.execution.base import Fill
    from memebot.portfolio.portfolio import ExitParams, Portfolio
    pf = Portfolio(10.0)
    pf.apply_buy(Fill("m", "buy", 100.0, 1.0, 0.01, 0.0, 0.0), symbol="X", mode="hold")   # 100 tok @ 0.01 = 1.0 SOL
    pos = pf.positions["m"]
    p = ExitParams()
    assert pos.should_take_initial(0.015, p) is False        # +50% < 2x
    assert pos.should_take_initial(0.02, p) is True          # 2x
    f = pos.derisk_fraction(0.02, 0.0, p.derisk_max_frac)    # sell_cost 0 -> f = 0.5 (recover full principal)
    assert abs(f - 0.5) < 1e-9
    pf.apply_sell(Fill("m", "sell", pos.qty * f, pos.qty * f * 0.02, 0.02, 0.0, 0.0), reason="take_initial")
    assert abs(pf.sol_balance - 10.0) < 1e-6                 # full 1.0 SOL principal recovered
    assert pf.positions["m"].qty > 0                         # a house-money tail still rides
    # own-latch independence: initial latched, partial still available
    pf.positions["m"].initial_taken = True
    assert pos.should_take_initial(0.02, p) is False and pf.positions["m"].should_take_partial(0.02, p) is True


def test_proactive_derisk_gate_off_by_default_and_fires_when_enabled():
    """WL4: derisk_proactive_pct was modeled in exitlab but DEAD in the live loop (unwired). The manage
    loop now gates on should_take_proactive_derisk, so the knob is real. OFF by default (0.0) -> live
    behavior is UNCHANGED; set it and it fires past the bar, sharing the P3-partial latch."""
    from memebot.execution.base import Fill
    from memebot.portfolio.portfolio import ExitParams, Portfolio
    pf = Portfolio(10.0)
    pf.apply_buy(Fill("m", "buy", 100.0, 1.0, 0.01, 0.0, 0.0), symbol="X", mode="hold")   # 1.0 SOL @ 0.01
    pos = pf.positions["m"]
    assert pos.should_take_proactive_derisk(0.0125, ExitParams()) is False     # default 0.0 -> OFF (no behavior change)
    ep = ExitParams(derisk_proactive_pct=0.20)
    assert pos.should_take_proactive_derisk(0.011, ep) is False                # +10% < 20% -> no
    assert pos.should_take_proactive_derisk(0.0125, ep) is True                # +25% >= 20% -> fire
    pos.partial_taken = True
    assert pos.should_take_proactive_derisk(0.0125, ep) is False               # shares the P3-partial latch (mutually exclusive w/ clean partial)


# ── B1: RugCheck risk cross-check ─────────────────────────────────────────────
def test_rugcheck_report_parse():
    """B1: a RugCheck 'danger'-level risk surfaces danger=True; warn/info or junk does not veto."""
    from memebot.feed.rugcheck import RugCheckClient
    p = RugCheckClient._parse
    # a danger-level risk -> danger True + names captured
    out = p({"score_normalised": 80, "risks": [
        {"name": "Mint authority enabled", "level": "danger"},
        {"name": "Low liquidity", "level": "warn"}]})
    assert out["danger"] is True and "Mint authority enabled" in out["risks"] and out["score"] == 80.0
    # only warn/info -> not a veto
    assert p({"risks": [{"name": "x", "level": "warn"}, {"name": "y", "level": "info"}]})["danger"] is False
    # empty / no risks -> safe
    assert p({"risks": []})["danger"] is False
    # junk body -> None (no opinion, never a false veto)
    assert p("nope") is None and p(None) is None
    assert p({"risks": "bad"})["danger"] is False        # tolerant of schema drift


# ── spec-S7: gate rug-dodge attribution ───────────────────────────────────────
def test_gate_attribution_confusion_matrix():
    """spec-S7: the gate's rug-avoidance confusion matrix + per-reason dodge/false-reject."""
    from memebot.backtest.gate_attribution import attribute
    recs = [{"mint": "r1", "outcome": "rug"}, {"mint": "r2", "outcome": "dead"},
            {"mint": "w1", "outcome": "winner"}, {"mint": "w2", "outcome": "winner"},
            {"mint": "f1", "outcome": "flat"}]
    gate = {
        "r1": {"passed": False, "reasons": {"mint_not_revoked": 1}},   # dodged a rug (attributed)
        "r2": {"passed": True, "reasons": {}},                          # missed a rug (passed harmful)
        "w1": {"passed": False, "reasons": {"buyers_low": 1}},          # lost a winner (false reject)
        "w2": {"passed": True, "reasons": {}},                          # kept a winner
        "f1": {"passed": False, "reasons": {}},                        # flat -> ignored
    }
    a = attribute(recs, gate)
    assert a["harmful_total"] == 2 and a["keep_total"] == 2
    assert a["cm"] == {"dodged": 1, "lost_winner": 1, "miss": 1, "kept_winner": 1}
    assert a["rug_dodge_recall"] == 0.5 and a["winner_loss_rate"] == 0.5
    assert a["reason_dodge"] == {"mint_not_revoked": 1}            # the check that dodged the rug
    assert a["reason_falsereject"] == {"buyers_low": 1}           # the check that lost a winner


def test_winner_loss_attribution_too_strict():
    """WL1 (winner_loss): per gate-reason winners-lost vs rugs-dodged, re-derived through the REAL gate
    (candidate_from_features -> rules.evaluate). A reason rejecting more WINNERS than RUGS is the
    over-strict calibration lever. Verifies the three branches: only-REJECTED mints counted, only the
    re-derivable reason attributed, and the kept/unexplained rejects ignored."""
    from memebot.config import Settings
    from memebot.backtest.winner_loss import attribute
    s = Settings()
    # feats that trip EXACTLY one gate reason (buyers_low) — every OTHER gate satisfied, so the
    # re-derivation is deterministic regardless of the buy/sell calibration value.
    def feats(buyers):
        return {"liquidity_usd": 10_000.0, "market_cap_usd": 50_000.0, "vol_to_mcap_pct": 50.0,
                "buy_sell_ratio": 2.0, "unique_buyers": buyers, "vol_h1": 5_000.0,
                "mint_revoked": 1.0, "freeze_revoked": 1.0, "lp_burned_pct": 100.0}
    recs = [{"mint": "w1", "outcome": "winner"}, {"mint": "w2", "outcome": "winner"},
            {"mint": "r1", "outcome": "rug"},    {"mint": "p1", "outcome": "winner"},
            {"mint": "k1", "outcome": "winner"}, {"mint": "f1", "outcome": "flat"}]
    gate = {
        "w1": {"passed": False, "feats": feats(5),  "price": 0.001},   # rejected winner -> buyers_low
        "w2": {"passed": False, "feats": feats(5),  "price": 0.001},   # rejected winner -> buyers_low
        "r1": {"passed": False, "feats": feats(5),  "price": 0.001},   # rejected rug    -> buyers_low (correct dodge)
        "p1": {"passed": True,  "feats": feats(5),  "price": 0.001},   # gate KEPT it -> not a reject, ignored
        "k1": {"passed": False, "feats": feats(30), "price": 0.001},   # rejected live but re-derives NO reason -> dropped
        "f1": {"passed": False, "feats": feats(5),  "price": 0.001},   # flat -> neither harmful nor keep, ignored
    }
    a = attribute(recs, gate, s)
    assert a["n_win_lost"] == 2 and a["n_rug_dodged"] == 1          # w1,w2 lost; r1 dodged; p1/k1/f1 excluded
    row = {r["reason"]: r for r in a["table"]}["buyers_low"]
    assert row["winners_lost"] == 2 and row["rugs_dodged"] == 1
    assert abs(row["precision"] - 1.0 / 3.0) < 1e-9                 # rugs / (rugs + winners) it rejected


def test_conc_trajectory_analyze():
    """WL3: hold-time concentration-rise trajectory -> per-mint PEAK rise, win/lose join, threshold sweep
    (losers-caught vs winners-wrongly-cut). The read side of the new hold_concentration capture."""
    from memebot.backtest.conc_trajectory import peak_rise_by_mint, win_by_mint, analyze
    hc = [("L1", 2.0), ("L1", 9.0), ("L1", 5.0),   # loser peaks at +9pp (MAX over readings)
          ("L2", 6.0),                              # loser peaks at +6pp
          ("W1", 1.0), ("W1", 3.0),                 # winner peaks at +3pp
          ("U1", None)]                             # unknown entry baseline -> dropped (no reference)
    peak = peak_rise_by_mint(hc)
    assert peak == {"L1": 9.0, "L2": 6.0, "W1": 3.0} and "U1" not in peak
    to = [("L1", -0.5, -0.5), ("L2", -0.2, -0.2), ("W1", 0.4, 0.4),
          ("G1", 99.0, 50.0)]                       # >20x pricing glitch -> excluded from the book
    wins = win_by_mint(to)
    assert wins == {"L1": False, "L2": False, "W1": True} and "G1" not in wins
    a = analyze(peak, wins, thresholds=(5.0, 8.0))
    assert a["n"] == 3 and a["n_win"] == 1 and a["n_lose"] == 2
    assert a["winner_med"] == 3.0 and a["loser_med"] == 7.5         # median(6,9)=7.5 -> losers rise MORE
    s5 = {r["threshold"]: r for r in a["sweep"]}[5.0]
    assert s5["losers_caught"] == 2 and s5["winners_cut"] == 0 and s5["precision"] == 1.0  # +5pp catches both losers, no winner
    s8 = {r["threshold"]: r for r in a["sweep"]}[8.0]
    assert s8["losers_caught"] == 1 and s8["winners_cut"] == 0      # +8pp catches only the +9pp loser


# ── holder funding-cluster (free-data concealed-concentration) ─────────────────
def test_holder_funder_cluster_and_owner_resolution():
    """Holder funding-cluster: the pure cluster fn + get_holder_owners (vault/burn excluded, deduped)."""
    import asyncio
    from memebot.feed.helius_rpc import HeliusRPC, largest_funder_cluster
    assert largest_funder_cluster(["F", "F", "F", "G"]) == 3      # 3 share funder F = concealed cluster
    assert largest_funder_cluster(["A", "B", "C"]) == 1           # all distinct -> no cluster
    assert largest_funder_cluster([None, "", "F"]) == 1           # ignores None/empty
    assert largest_funder_cluster([]) == 0

    async def run():
        h = HeliusRPC("https://x.helius-rpc.com/?api-key=k")
        async def fake_largest(mint):
            return [("VAULT", 1000.0), ("h1", 100.0), ("h2", 90.0), ("h1dup", 80.0)]
        owner_of = {"h1": "ownerA", "h2": "ownerB", "h1dup": "ownerA"}   # h1dup -> same owner as h1
        async def fake_rpc(method, params):
            return {"value": {"data": {"parsed": {"info": {"owner": owner_of.get(params[0])}}}}}
        h.get_largest_holders = fake_largest
        h._rpc = fake_rpc
        return await h.get_holder_owners("M", top_n=4, exclude_largest=True)
    owners = asyncio.run(run())
    assert "ownerA" in owners and "ownerB" in owners               # the vault (largest) is dropped
    assert owners.count("ownerA") == 1                             # owners deduped


# ── brain audit (is the LLM brain used / does it earn its keep?) ───────────────
def test_brain_audit_usage_and_ab():
    """deferred-5 #1: brain-usage rate (brain vs rule decisions) + per-source CLEAN-book A/B."""
    from memebot.backtest.brain_audit import usage_rate, ab_book, _is_brain
    assert _is_brain("ollama:gemma3:4b") and _is_brain("sonnet") and not _is_brain("rule")
    u = usage_rate([("rule", 6511), ("ollama:gemma3:4b", 6), ("sonnet", 3)])
    assert u["total"] == 6520 and u["brain"] == 9 and u["rule"] == 6511
    assert abs(u["brain_frac"] - 9 / 6520) < 1e-9
    # A/B: glitch round-trip (>20x) excluded; brain vs rule grouped; unlabeled kept separate
    ab = ab_book([("ollama:gemma3:4b", 0.5, 0.5), ("ollama:gemma3:4b", -0.1, -0.1),
                  ("rule", 0.2, 0.2), ("rule", 99.0, 50.0), (None, 1.0, 1.0)])
    assert ab["brain"]["n"] == 2 and abs(ab["brain"]["net"] - 0.4) < 1e-9
    assert ab["rule"]["n"] == 1                       # the 50x glitch row dropped, leaving 1 clean rule trade
    assert ab["(unlabeled)"]["n"] == 1               # NULL-source rows tracked separately, not mixed in
