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


def test_runner_steps_once_per_closed_bar():
    """WL20 rank-1 (the bug): repeated polls with the SAME latest-bar ts must NOT step the engine twice
    (no per-poll over-counting of bars_held, no intra-bar repaint)."""
    import asyncio
    from memebot.swing.runner import SwingRunner
    from memebot.config import Settings
    r = SwingRunner(Settings.load())
    r._save = lambda: None                                          # don't write swing_state.json in the test
    r.universe = {"SYM": "MINT"}
    flat = [{"close": 100.0, "high": 101, "low": 99, "time": i * 1000} for i in range(25)]
    bars = flat + [{"close": 70.0, "high": 101, "low": 69, "time": 25000}]   # last CLOSED bar is a deep dip
    bars += [{"close": 71.0, "high": 72, "low": 70, "time": 26000}]          # a still-forming bar (excluded)

    class FakeClient:
        base_url = ""
        async def chart(self_, mint, interval):
            return bars

    async def run():
        c = FakeClient()
        await r._scan_once(c)
        h1 = r.engine.positions.get("MINT")
        await r._scan_once(c)                                       # identical poll, same ts -> must not re-step
        h2 = r.engine.positions.get("MINT")
        return h1, h2
    h1, h2 = asyncio.run(run())
    assert h1 is not None                                           # entered on the closed dip bar
    assert h1.bars_held == h2.bars_held                            # second identical poll did NOT advance


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
