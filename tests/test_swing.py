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
