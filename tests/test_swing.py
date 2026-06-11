"""WL18 swing mode — the mean-reversion strategy + paper engine (pure logic, no network)."""
from memebot.swing.strategy import sma, mean_rev_position, dip_depth
from memebot.swing.engine import SwingEngine, SwingParams


def test_sma_and_dip_depth():
    assert sma([1, 2, 3], 5) is None                 # not enough history
    assert sma([1, 2, 3, 4], 2) == 3.5               # last 2
    assert dip_depth([100, 100, 100], 3) == 0.0      # price at SMA -> not a dip
    d = dip_depth([100, 100, 80], 3)                  # SMA=93.33, price 80 -> ~14.3% below
    assert d is not None and 0.14 < d < 0.15
    assert dip_depth([100, 100, 120], 3) == 0.0      # above SMA -> 0, not negative


def test_mean_rev_position():
    # SMA of last 3 = 95; dip_k 0.10 -> enter threshold 85.5
    assert mean_rev_position([100, 100, 85], window=3, dip_k=0.10, pos=0) == 1    # 85 < 85.5 -> enter
    assert mean_rev_position([100, 100, 90], window=3, dip_k=0.10, pos=0) == 0    # 90 > 85.5 -> stay flat
    # held: exit when price reverts above SMA (exit_k 0)
    assert mean_rev_position([100, 85, 110], window=3, dip_k=0.10, pos=1) == 0    # 110 > SMA 98.33 -> exit
    assert mean_rev_position([100, 85, 90], window=3, dip_k=0.10, pos=1) == 1     # 90 < SMA 91.67 -> hold
    assert mean_rev_position([100], window=3, dip_k=0.10, pos=0) == 0             # no SMA yet -> no action


def test_engine_dip_then_reversion_is_profitable():
    eng = SwingEngine(SwingParams(window=3, dip_k=0.10, exit_k=0.0, fee_pct=0.0, size_sol=1.0), initial_sol=10.0)
    assert eng.step("M", "SYM", [100, 100, 100], 1.0) is None        # flat -> no action
    r = eng.step("M", "SYM", [100, 100, 85], 2.0)                    # deep dip -> ENTER
    assert r and r[0] == "enter" and "M" in eng.positions
    assert abs(eng.cash - 9.0) < 1e-9                                # 10 - 1 size
    r2 = eng.step("M", "SYM", [100, 85, 110], 3.0)                   # reversion -> EXIT
    assert r2 and r2[0] == "exit" and "M" not in eng.positions
    assert r2[1].pnl_sol > 0 and r2[1].reason == "reversion"         # bought 85, sold 110 -> profit
    assert eng.stats()["closed"] == 1 and eng.stats()["win_rate"] == 1.0


def test_engine_fee_and_max_positions():
    # fee eats into pnl: a round-trip at the same price loses the fee
    eng = SwingEngine(SwingParams(window=2, dip_k=0.0, exit_k=-1.0, fee_pct=0.02, size_sol=1.0), initial_sol=10.0)
    eng.step("A", "A", [100, 98], 1.0)                               # SMA 99, price 98<99 -> enter
    out = eng.step("A", "A", [100, 98], 2.0)                         # exit_k -1 -> threshold 0 -> exit at 98
    assert out and out[0] == "exit" and out[1].pnl_sol < 0           # same-price round-trip fee -> loss
    # max_positions cap
    eng2 = SwingEngine(SwingParams(window=2, dip_k=0.0, fee_pct=0.0, size_sol=1.0, max_positions=1), initial_sol=10.0)
    eng2.step("A", "A", [100, 98], 1.0)                              # enter A
    eng2.step("B", "B", [100, 98], 1.0)                             # blocked by max_positions=1
    assert len(eng2.positions) == 1


def test_engine_time_stop():
    eng = SwingEngine(SwingParams(window=2, dip_k=0.0, exit_k=5.0, fee_pct=0.0, size_sol=1.0, max_hold_bars=2),
                      initial_sol=10.0)
    eng.step("A", "A", [100, 98], 1.0)                               # enter (exit_k 5 -> never reverts out)
    eng.step("A", "A", [100, 98], 2.0)                              # bars_held 1
    out = eng.step("A", "A", [100, 98], 3.0)                        # bars_held 2 -> time stop
    assert out and out[0] == "exit" and out[1].reason == "time_stop"


def test_engine_stop_loss():
    """WL20 rank-2: a hard stop exits when price falls stop_k below ENTRY (falling-knife cap)."""
    eng = SwingEngine(SwingParams(window=2, dip_k=0.0, exit_k=5.0, fee_pct=0.0, size_sol=1.0, stop_k=0.20),
                      initial_sol=10.0)
    eng.step("A", "A", [100, 98], 1.0)                              # enter at 98 (exit_k 5 -> no reversion exit)
    out = eng.step("A", "A", [100, 78], 2.0)                        # (98-78)/98=0.204 >= 0.20 -> stop
    assert out and out[0] == "exit" and out[1].reason == "stop"


def test_engine_slippage_costs():
    """WL20 rank-4: slippage makes a same-price round-trip lose more than with no slippage."""
    def round_trip(slip_bps):
        eng = SwingEngine(SwingParams(window=2, dip_k=0.0, exit_k=-1.0, fee_pct=0.0, size_sol=1.0,
                                      slippage_bps=slip_bps), initial_sol=10.0)
        eng.step("A", "A", [100, 98], 1.0)
        out = eng.step("A", "A", [100, 98], 2.0)
        return out[1].pnl_sol
    assert abs(round_trip(0)) < 1e-9                                # no fee/slip -> flat
    assert round_trip(100) < round_trip(0)                          # 100 bps/side slippage -> a loss


def test_engine_kill_switch():
    """WL20 rank-5: a rolling realized-loss halt + an aggregate-exposure cap block NEW entries."""
    eng = SwingEngine(SwingParams(window=2, dip_k=0.0, exit_k=-1.0, fee_pct=0.0, size_sol=1.0,
                                  rolling_loss_halt_sol=0.5, loss_halt_lookback=5), initial_sol=10.0)
    eng.step("A", "A", [100, 90], 1.0)                              # enter at 90
    eng.step("A", "A", [100, 40], 2.0)                             # exit at 40 -> pnl ~ -0.56
    assert eng.closed and eng.closed[0].pnl_sol < -0.5
    eng.step("B", "B", [100, 90], 3.0)                             # halted by the rolling-loss gate
    assert "B" not in eng.positions
    eng2 = SwingEngine(SwingParams(window=2, dip_k=0.0, fee_pct=0.0, size_sol=1.0, max_total_exposure_sol=1.5),
                       initial_sol=10.0)
    eng2.step("A", "A", [100, 90], 1.0)                            # deployed 1.0
    eng2.step("B", "B", [100, 90], 1.0)                           # 1.0+1.0 > 1.5 cap -> blocked
    assert len(eng2.positions) == 1


def test_runner_first_sighting_then_steps_once():
    """WL20/WL21 rank-1: first sighting sets the baseline (no acting on history); a NEWLY-closed bar steps
    the engine exactly once; re-polling the same bars does NOT double-step (no bars_held over-count / repaint)."""
    import asyncio
    from memebot.swing.runner import SwingRunner
    from memebot.config import Settings
    r = SwingRunner(Settings.load())
    r._save = lambda: None                                          # don't write swing_state.json in the test
    r.universe = {"SYM": "MINT"}
    flat = [{"close": 100.0, "high": 101, "low": 99, "time": i * 1000} for i in range(25)]
    scan1 = flat + [{"close": 100.0, "high": 101, "low": 99, "time": 25000}]            # only a forming bar
    scan2 = flat + [{"close": 70.0, "high": 101, "low": 69, "time": 25000},             # the dip just CLOSED
                    {"close": 71.0, "high": 72, "low": 70, "time": 26000}]              # + a new forming bar
    seq = [scan1, scan2, scan2]                                     # scan3 re-polls scan2 (no new bar)

    class FakeClient:
        base_url = ""
        def __init__(self_):
            self_.i = 0
        async def chart(self_, mint, interval):
            b = seq[min(self_.i, len(seq) - 1)]
            self_.i += 1
            return b

    async def run():
        c = FakeClient()
        await r._scan_once(c)                                       # first sighting -> baseline only
        a = r.engine.positions.get("MINT")
        await r._scan_once(c)                                       # the dip bar closed -> ENTER once
        b = r.engine.positions.get("MINT")
        bh_b = b.bars_held if b else None
        await r._scan_once(c)                                       # re-poll, no new bar -> no re-step
        d = r.engine.positions.get("MINT")
        return a, b, bh_b, (d.bars_held if d else None)
    a, b, bh_b, bh_d = asyncio.run(run())
    assert a is None                                                # first sighting did NOT act on history
    assert b is not None                                            # entered on the newly-closed dip bar
    assert bh_b == bh_d                                             # re-poll did NOT double-step


def test_swing_readiness_gate():
    """WL27: the forward-proof GO/NO-GO — GO only on enough trades + net-positive + pf + win-rate."""
    from memebot.swing.readiness import assess
    assert assess([{"pnl_sol": 0.5}], min_trades=40, min_pf=1.2, min_winrate=0.5)["go"] is False  # tiny sample
    winning = [{"pnl_sol": 0.3}] * 30 + [{"pnl_sol": -0.1}] * 10                 # 40 trades, 75% win, net +8
    r = assess(winning, min_trades=40, min_pf=1.2, min_winrate=0.5)
    assert r["go"] and r["n"] == 40 and abs(r["win_rate"] - 0.75) < 1e-9 and r["net"] > 0
    losing = [{"pnl_sol": -0.2}] * 40
    r2 = assess(losing, min_trades=40, min_pf=1.2, min_winrate=0.5)
    assert r2["go"] is False and any("net" in x for x in r2["reasons"])
    # pf-only NO-GO: net-positive + enough trades + adequate win-rate, but a few big losses drag pf under the
    # floor -> the profit-factor branch must be the DECIDING reject (isolates pf from the net/win-rate gates).
    pf_short = [{"pnl_sol": 0.2}] * 24 + [{"pnl_sol": -0.28}] * 16   # 40 trades, 60% win, net +0.32, pf ~1.07
    r3 = assess(pf_short, min_trades=40, min_pf=1.2, min_winrate=0.5)
    assert r3["go"] is False and r3["net"] > 0 and r3["win_rate"] >= 0.5
    assert any("profit factor" in x for x in r3["reasons"]) and not any("net" in x for x in r3["reasons"])
    # infinite-PF (no-loss) edge branch: all winners -> gross_loss==0 -> pf=inf must read GO, not crash/NaN.
    all_win = [{"pnl_sol": 0.1}] * 40
    r4 = assess(all_win, min_trades=40, min_pf=1.2, min_winrate=0.5)
    assert r4["go"] is True and r4["pf"] == float("inf")


def test_promote_self_improvement():
    """WL25: a challenger that beats live by the margin for streak_needed CONSECUTIVE runs is promoted;
    a within-margin challenger or a non-mean-reversion (Bollinger) winner is NOT."""
    from memebot.swing.promote import decide_promotion, parse_meanrev, live_name
    assert parse_meanrev("meanrev w24 k0.18") == {"window": 24, "dip_k": 0.18}
    assert parse_meanrev("bolling w24 k2.5") is None
    assert live_name(24, 0.18) == "meanrev w24 k0.18"
    rows = [
        (2.00, "bolling w24 k2.5", {}, True),          # highest gmean but NOT mean-rev -> ineligible
        (1.80, "meanrev w12 k0.25", {}, True),         # beats live 1.50 by 20% (> 15% margin)
        (1.50, "meanrev w24 k0.18", {}, True),         # the live config
    ]
    s = {}
    for run in (1, 2):
        p, s, _ = decide_promotion(rows, 24, 0.18, s, margin=0.15, streak_needed=3)
        assert p is None and s == {"meanrev w12 k0.25": run}     # streak builds, no promotion yet
    p, s, note = decide_promotion(rows, 24, 0.18, s, margin=0.15, streak_needed=3)
    assert p == {"window": 12, "dip_k": 0.25} and "PROMOTE" in note   # 3rd consecutive win -> promote
    # a challenger that does NOT clear the margin -> hold, no streak
    close = [(1.55, "meanrev w12 k0.25", {}, True), (1.50, "meanrev w24 k0.18", {}, True)]
    p2, s2, _ = decide_promotion(close, 24, 0.18, {}, margin=0.15, streak_needed=3)
    assert p2 is None and s2 == {}


def test_geckoterminal_chart_parse():
    """WL23: GeckoTerminal ohlcv_list ([ts,o,h,l,c,v], newest-first) -> the engine's ascending dict bars."""
    import asyncio
    from memebot.feed.geckoterminal import GeckoTerminalClient
    c = GeckoTerminalClient()
    c._pool = {"MINT": "POOL"}                                      # pre-cache the pool so chart skips pool_for
    payload = {"data": {"attributes": {"ohlcv_list": [
        [200, 2.0, 2.1, 1.9, 2.05, 100.0],                         # GeckoTerminal returns newest-first
        [100, 1.0, 1.1, 0.9, 1.05, 50.0],
    ]}}}

    class FakeResp:
        status_code = 200
        def json(self_):
            return payload

    class FakeClient:
        async def get(self_, url, params=None):
            return FakeResp()
    c._client = FakeClient()
    bars = asyncio.run(c.chart("MINT", "4h"))
    assert len(bars) == 2
    assert bars[0]["time"] == 100 and bars[-1]["time"] == 200       # sorted ASCENDING (latest last)
    assert bars[-1]["close"] == 2.05 and bars[0]["close"] == 1.05


def test_run_discovery_smoke():
    """WL19: the discovery engine searches + walk-forward-validates and returns ranked rows. Synthetic
    oscillating prices (mean-reversion has signal) -> at least one config runs + the row shape holds."""
    import math
    from memebot.backtest.swing_discover import run_discovery
    def bars(phase):
        return [{"close": 100 + 20 * math.sin(i / 5 + phase), "high": 121, "low": 79, "time": i * 3600}
                for i in range(260)]
    data = {"A": bars(0.0), "B": bars(1.0)}
    rows = run_discovery(data, fee=0.01, clip=3.0, split=0.7)
    assert isinstance(rows, list) and rows
    g, name, r, passed = rows[0]
    assert isinstance(name, str) and {"n", "train_gmean", "test_gmean"} <= set(r) and isinstance(passed, bool)


def test_engine_simulate_cost_parity():
    """EXCELLENCE regression: the LIVE engine and the BACKTEST _simulate must agree on a trade's net return
    (locks in the WL21 entry-cost parity fix). Same dip->bounce + same fee+slippage -> identical per-trade
    pnl, so the forward book is honestly comparable to the validated backtest."""
    from memebot.backtest.swing_lab import _simulate, mean_rev
    W, K, FEE, SLIP = 3, 0.10, 0.02, 30.0
    closes = [100.0, 100.0, 100.0, 80.0, 110.0, 105.0]               # flat -> deep dip -> reversion bounce
    bars = [{"close": c, "high": c * 1.01, "low": c * 0.99, "time": i * 1000} for i, c in enumerate(closes)]
    eng = SwingEngine(SwingParams(window=W, dip_k=K, exit_k=0.0, fee_pct=FEE, slippage_bps=SLIP, size_sol=1.0),
                      initial_sol=10.0)
    eng.step("M", "M", closes[:3], 1.0)                              # no dip
    eng.step("M", "M", closes[:4], 2.0)                             # enter at 80
    eng.step("M", "M", closes[:5], 3.0)                             # reversion -> exit at 110
    assert len(eng.closed) == 1
    _eq, _nt, _nw, trade_rets = _simulate(bars, mean_rev(W, K), FEE, W, clip=3.0, slippage_bps=SLIP)
    assert len(trade_rets) == 1
    assert abs(eng.closed[0].pnl_pct - trade_rets[0]) < 1e-9        # full parity: same prices + same per-side cost


def test_runner_multibar_gap_replay():
    """WL22 EXCELLENCE: a poll GAP (downtime) where SEVERAL bars closed at once must REPLAY each closed bar
    so bars_held counts BARS not polls (a time-stop/decision is never lost). 3 new closed bars -> 3 steps."""
    import asyncio
    from memebot.swing.runner import SwingRunner
    from memebot.swing.engine import SwingPosition
    from memebot.config import Settings
    r = SwingRunner(Settings.load())
    r._save = lambda: None
    r.universe = {"SYM": "MINT"}
    r.engine.positions["MINT"] = SwingPosition("MINT", "SYM", 1.0, 100.0, 0.0, 1.0, 0.18, 0)  # held, bars_held 0
    r._last_bar["MINT"] = 2000.0                                     # we last stepped the bar at t=2000
    # closed bars at t=1000..5000 (3000/4000/5000 are NEW) + a forming bar at 6000; flat price -> stays held
    bars = [{"close": 95.0, "high": 96, "low": 94, "time": t} for t in (1000, 2000, 3000, 4000, 5000, 6000)]

    class FakeClient:
        base_url = ""
        async def chart(self_, mint, interval):
            return bars
    asyncio.run(r._scan_once(FakeClient()))
    held = r.engine.positions.get("MINT")
    assert held is not None and held.bars_held == 3                 # replayed exactly the 3 new closed bars


def test_engine_glitch_price_guard():
    """EXCELLENCE (audit): a NaN/inf/<=0 glitch price must never enter/exit/price a position or crash."""
    import math
    eng = SwingEngine(SwingParams(window=2, dip_k=0.0, fee_pct=0.0, size_sol=1.0), initial_sol=10.0)
    for bad in (float("nan"), float("inf"), 0.0, -5.0):
        assert eng.step("A", "A", [100.0, bad], 1.0) is None        # glitch -> no action
    assert not eng.positions                                        # nothing entered on a glitch
    eng.step("A", "A", [100.0, 90.0], 2.0)                          # a clean dip enters
    assert math.isfinite(eng.equity({"A": float("inf")}))          # a glitch MARK falls back to entry price


def test_reconstruct_schema_drift():
    """EXCELLENCE (audit): _reconstruct drops unknown keys so a state-schema change can't crash the loader."""
    from memebot.swing.runner import _reconstruct
    from memebot.swing.engine import SwingPosition
    d = {"mint": "M", "symbol": "S", "qty": 1.0, "entry_price": 100.0, "entry_ts": 0.0, "sol_in": 1.0,
         "dip_at_entry": 0.18, "bars_held": 2, "REMOVED_FIELD": 999}        # a stale/extra field
    p = _reconstruct(SwingPosition, d)
    assert p.mint == "M" and p.bars_held == 2 and not hasattr(p, "REMOVED_FIELD")


def test_runner_manages_held_not_in_universe():
    """EXCELLENCE (audit): a held position whose token left the universe is still STEPPED (managed), not
    stranded with no exit."""
    import asyncio
    from memebot.swing.runner import SwingRunner
    from memebot.swing.engine import SwingPosition
    from memebot.config import Settings
    r = SwingRunner(Settings.load())
    r._save = lambda: None
    r.universe = {}                                                 # token has dropped OUT of the universe
    r.engine.positions["M"] = SwingPosition("M", "SYM", 1.0, 100.0, 0.0, 1.0, 0.18, 0)
    r._last_bar["M"] = 2000.0
    bars = [{"close": 100.0, "high": 101, "low": 99, "time": 1000},
            {"close": 100.0, "high": 101, "low": 99, "time": 2000},
            {"close": 100.0, "high": 101, "low": 99, "time": 3000},        # one NEW closed bar
            {"close": 100.0, "high": 101, "low": 99, "time": 4000}]        # forming

    class FakeClient:
        async def chart(self_, mint, interval):
            return bars
    asyncio.run(r._scan_once(FakeClient()))
    assert r.engine.positions["M"].bars_held == 1                  # managed (stepped) despite not in universe


def test_swing_universe_discovery_filters():
    """WL33 candidate filter (pure): keep only NEW, established, single-mint memecoins — drop the existing
    universe, stables/blue-chips, copy-spam (a symbol on >1 mint), thin liquidity, and too-fresh pools."""
    from memebot.swing.discover_universe import _rank_candidates, _age_days, _EXCLUDE
    from memebot.swing.universe import DEFAULT_UNIVERSE
    bonk = DEFAULT_UNIVERSE["BONK"]
    rows = [
        {"symbol": "BAR", "mint": "barmint", "liq": 500_000, "age": 200},     # established memecoin -> KEEP
        {"symbol": "FOO", "mint": "foo1", "liq": 900_000, "age": 300},         # copy-spam: same symbol,
        {"symbol": "FOO", "mint": "foo2", "liq": 800_000, "age": 300},         #   two mints -> BOTH dropped
        {"symbol": "USDC", "mint": "usdcmint", "liq": 9_000_000, "age": 999},  # stable -> drop
        {"symbol": "FRESH", "mint": "freshmint", "liq": 600_000, "age": 5},    # too young -> drop
        {"symbol": "SMALL", "mint": "smallmint", "liq": 50_000, "age": 300},   # below min_liq -> drop
        {"symbol": "BONK", "mint": bonk, "liq": 700_000, "age": 999},          # already in universe -> drop
    ]
    out = _rank_candidates(rows, min_liq=250_000, top=10, min_age_days=90)
    assert [c["symbol"] for c in out] == ["BAR"]
    assert "USDC" in _EXCLUDE and "HYPE" in _EXCLUDE                            # stables + non-memecoins listed
    assert _age_days(None) is None and _age_days("garbage") is None
    assert _age_days("2020-01-01T00:00:00Z") > 1000                            # years old -> large positive age
