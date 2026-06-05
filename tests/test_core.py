"""Core logic tests: money, portfolio, pnl, risk, rules, scoring, features.

Plain assert-based; runs under pytest OR `python run_tests.py` (no deps).
"""
from __future__ import annotations

import time

from memebot.config import Fees, Settings
from memebot.constants import MODE_HOLD, MODE_SCALP
from memebot.execution.base import Fill
from memebot.filter import rules
from memebot.filter.features import FEATURE_NAMES, build_features, feature_vector
from memebot.models import Candidate
from memebot.portfolio.pnl import compute_stats, max_drawdown
from memebot.portfolio.portfolio import ClosedTrade, ExitParams, Portfolio, Position
from memebot.risk.limits import RiskManager
from memebot.signals.scoring import Scorer
from memebot.utils.money import (estimate_slippage_pct, lamports_to_sol,
                                 simulate_buy, simulate_sell, sol_to_lamports)

FEES = Fees()


def test_lamports_roundtrip():
    assert sol_to_lamports(1.5) == 1_500_000_000
    assert abs(lamports_to_sol(1_500_000_000) - 1.5) < 1e-9


def test_slippage_scales_with_size():
    base = FEES.base_slippage_pct
    small = estimate_slippage_pct(0.1, 100.0, base)
    big = estimate_slippage_pct(10.0, 100.0, base)
    assert small >= base
    assert big > small
    assert estimate_slippage_pct(1.0, 0.0, base) == 0.5   # zero liquidity -> cap


def test_simulate_buy_fees_and_slippage():
    r = simulate_buy(1.0, 1e-6, 0.03, FEES)
    assert r.sol == 1.0                 # everything-in budget
    assert r.fee_sol > 0
    assert r.eff_price_sol > 1e-6       # buy pays the slippage-raised price
    assert 0 < r.tokens < 1e6           # fewer than ideal due to fees + slippage


def test_simulate_sell_net_after_fees():
    r = simulate_sell(1_000_000, 1e-6, 0.03, FEES)
    gross = 1_000_000 * 1e-6 * (1 - 0.03)
    assert r.sol < gross
    assert r.fee_sol > 0


def test_portfolio_round_trip_profit():
    pf = Portfolio(10.0)
    br = simulate_buy(0.5, 1e-6, 0.03, FEES)
    buy = Fill("m", "buy", br.tokens, br.sol, br.eff_price_sol, br.fee_sol, br.slippage_pct)
    assert pf.apply_buy(buy, symbol="X", mode="scalp")
    assert abs(pf.sol_balance - 9.5) < 1e-9
    pos = pf.positions["m"]
    sr = simulate_sell(pos.qty, 3e-6, 0.03, FEES)     # ~3x price
    sell = Fill("m", "sell", sr.tokens, sr.sol, sr.eff_price_sol, sr.fee_sol, sr.slippage_pct)
    pnl = pf.apply_sell(sell, reason="tp")
    assert pnl > 0
    assert len(pf.closed) == 1 and "m" not in pf.positions
    assert abs(pf.sol_balance - (10.0 + pnl)) < 1e-6


def test_apply_sell_fill_sanity_clamp():
    # a single round-trip booking proceeds >> max_return_mult x cost is a PRICING GLITCH (curve-buy
    # vs DexScreener-sell SOL-scale mismatch) — clamp it so one fill can't fabricate the whole book.
    def _buy(pf):
        pf.apply_buy(Fill("m", "buy", 1000.0, 0.5, 5e-4, 0.01, 0.03), symbol="m", mode="hold")
    pf = Portfolio(10.0); _buy(pf)
    pf.apply_sell(Fill("m", "sell", 1000.0, 100.0, 0.1, 0.0, 0.0), reason="tp", max_return_mult=20.0)
    assert abs(pf.closed[-1].proceeds_sol - 10.0) < 1e-9     # 100 SOL (200x) clamped to 20x*0.5 = 10
    assert abs(pf.closed[-1].pnl_sol - 9.5) < 1e-9
    pf2 = Portfolio(10.0); _buy(pf2)
    pf2.apply_sell(Fill("m", "sell", 1000.0, 100.0, 0.1, 0.0, 0.0), reason="tp", max_return_mult=0.0)
    assert abs(pf2.closed[-1].proceeds_sol - 100.0) < 1e-9   # 0 disables -> legacy unclamped
    pf3 = Portfolio(10.0); _buy(pf3)
    pf3.apply_sell(Fill("m", "sell", 1000.0, 1.5, 1.5e-3, 0.0, 0.0), reason="tp", max_return_mult=20.0)
    assert abs(pf3.closed[-1].proceeds_sol - 1.5) < 1e-9     # a real +200% (3x) winner is NOT clamped


def test_partial_sell_keeps_position():
    pf = Portfolio(10.0)
    pf.apply_buy(Fill("m", "buy", 1000.0, 0.5, 5e-4, 0.01, 0.03), symbol="X", mode="scalp")
    pnl = pf.apply_sell(Fill("m", "sell", 400.0, 0.3, 7.5e-4, 0.005, 0.03), reason="partial")
    assert pnl is not None
    assert "m" in pf.positions
    assert abs(pf.positions["m"].qty - 600.0) < 1e-6
    assert len(pf.closed) == 0


def test_partial_sell_accounting_books_full_round_trip():
    # P3/P2: a partially-exited trade must book the FULL round-trip into ClosedTrade, not just the
    # final slice — otherwise trade_outcomes / PnL stats understate any partial-TP trade.
    pf = Portfolio(10.0)
    pf.apply_buy(Fill("m", "buy", 1000.0, 0.5, 5e-4, 0.0, 0.0), symbol="X", mode="scalp")
    pf.apply_sell(Fill("m", "sell", 500.0, 0.4, 8e-4, 0.0, 0.0), reason="partial_tp")
    assert "m" in pf.positions and len(pf.closed) == 0            # still open after the partial
    pf.apply_sell(Fill("m", "sell", 500.0, 0.4, 8e-4, 0.0, 0.0), reason="tp")
    assert "m" not in pf.positions and len(pf.closed) == 1
    ct = pf.closed[-1]
    assert abs(ct.cost_sol - 0.5) < 1e-9                          # FULL original cost (not the 0.25 final slice)
    assert abs(ct.proceeds_sol - 0.8) < 1e-9                      # full proceeds 0.4 + 0.4
    assert abs(ct.pnl_sol - 0.3) < 1e-9                           # full pnl 0.8 - 0.5
    assert abs(ct.pnl_pct - 0.6) < 1e-9                           # 0.3 / 0.5 — matches a single full sell


def test_should_take_partial_trigger():
    ep = ExitParams()                                            # partial_tp_pct=0.15, frac=0.5
    pos = Position("m", "X", MODE_SCALP, qty=1000.0, cost_sol=0.5)   # avg 5e-4
    assert pos.should_take_partial(5e-4 * 1.10, ep) is False     # +10% < 15% -> no
    assert pos.should_take_partial(5e-4 * 1.20, ep) is True      # +20% >= 15% -> yes
    pos.partial_taken = True
    assert pos.should_take_partial(5e-4 * 1.50, ep) is False     # one-time only
    pos.partial_taken = False
    assert pos.should_take_partial(5e-4 * 1.50, ExitParams(partial_tp_frac=0.0)) is False  # disabled


def test_entry_features_carry_to_closed_trade():
    # P2: apply_buy stashes the entry feature vector + score on the Position, and apply_sell
    # carries them onto the ClosedTrade -> the trade_outcomes supervised row.
    pf = Portfolio(10.0)
    feats = {"vol_h1": 2500.0, "liquidity_usd": 9000.0}
    pf.apply_buy(Fill("m", "buy", 1000.0, 0.5, 5e-4, 0.01, 0.03), symbol="X", mode="scalp",
                 features=feats, score=0.61)
    pos = pf.positions["m"]
    assert pos.entry_features == feats and abs(pos.entry_score - 0.61) < 1e-9
    pf.apply_sell(Fill("m", "sell", 1000.0, 0.8, 8e-4, 0.01, 0.03), reason="tp")
    ct = pf.closed[-1]
    assert ct.entry_features == feats and abs(ct.entry_score - 0.61) < 1e-9


def test_should_exit_scalp():
    ep = ExitParams()
    p = Position("m", "X", "scalp", qty=1000, cost_sol=0.001, peak_price_sol=1e-6)  # avg = 1e-6
    assert p.should_exit(1.5e-6, ep) == (True, "tp")        # +50% >= 0.40
    p_sl = Position("m", "X", "scalp", qty=1000, cost_sol=0.001, peak_price_sol=1e-6)
    assert p_sl.should_exit(0.8e-6, ep) == (True, "sl")     # -20% <= -0.18
    p_to = Position("m", "X", "scalp", qty=1000, cost_sol=0.001, opened_ts=time.time() - 1000)
    assert p_to.should_exit(1.05e-6, ep)[1] == "timeout"   # +5% (not under water), held long -> timeout
    p_st = Position("m", "X", "scalp", qty=1000, cost_sol=0.001, opened_ts=time.time() - 100)
    assert p_st.should_exit(1.0e-6, ep)[1] == "stall"      # flat past the stall window -> P3 stall


def test_be_trail_exit():
    ep = ExitParams()                                       # arm 0.12, trail_after_arm 0.04
    p = Position("m", "X", "scalp", qty=1000, cost_sol=0.001)   # avg 1e-6
    p.should_exit(1.20e-6, ep)                              # +20% -> arm, peak 1.20
    assert p.breakeven_armed
    assert p.should_exit(1.10e-6, ep) == (True, "be_trail")    # 8.3% off peak (>4%) -> P3 arm-trail


def test_breakeven_stop():
    # explicit arm/floor + be_trail disabled (trail_after_arm 1.0) so this isolates the breakeven-FLOOR logic
    ep = ExitParams(breakeven_arm_pct=0.12, breakeven_floor_pct=0.02, trail_after_arm_pct=1.0)
    p = Position("m", "X", "scalp", qty=1000, cost_sol=0.001)   # avg 1e-6
    assert p.should_exit(1.05e-6, ep)[0] is False and not p.breakeven_armed   # +5%: not armed
    assert p.should_exit(1.15e-6, ep)[0] is False and p.breakeven_armed       # +15%: arms, no exit yet
    assert p.should_exit(1.01e-6, ep) == (True, "breakeven")                  # falls to +1% -> breakeven
    # a position that never armed uses the normal stop-loss, not breakeven
    q = Position("m", "X", "scalp", qty=1000, cost_sol=0.001)
    assert q.should_exit(0.80e-6, ep) == (True, "sl")                         # -20% <= -18% sl
    assert not q.breakeven_armed
    # -10% with no arming: within sl, holds
    r = Position("m", "X", "scalp", qty=1000, cost_sol=0.001)
    assert r.should_exit(0.90e-6, ep)[0] is False
    # armed, then a hard gap-down THROUGH the stop -> recorded as 'sl', not 'breakeven'
    g = Position("m", "X", "scalp", qty=1000, cost_sol=0.001)
    g.should_exit(1.20e-6, ep)                              # arm at +20%
    assert g.breakeven_armed
    assert g.should_exit(0.75e-6, ep) == (True, "sl")       # -25% breaches SL


def test_breakeven_armed_giveback_to_loss_labels_sl():
    # an armed give-back ABOVE entry is "breakeven"; BELOW entry is an honest "sl" (be_trail disabled to isolate)
    ep = ExitParams(breakeven_arm_pct=0.12, breakeven_floor_pct=0.02, trail_after_arm_pct=1.0)
    win = Position("m", "X", "scalp", qty=1000, cost_sol=0.001)
    win.should_exit(1.15e-6, ep)                                 # arm at +15%
    assert win.should_exit(1.01e-6, ep) == (True, "breakeven")   # +1% -> breakeven
    loss = Position("m", "X", "scalp", qty=1000, cost_sol=0.001)
    loss.should_exit(1.15e-6, ep)                                # arm at +15%
    assert loss.should_exit(0.95e-6, ep) == (True, "sl")         # -5% give-back -> honest sl


def test_should_exit_overrides():
    ep = ExitParams()
    p = Position("m", "X", "scalp", qty=1000, cost_sol=0.001, tp_override=0.10)  # avg 1e-6
    assert p.should_exit(1.11e-6, ep) == (True, "tp")       # +11% >= 0.10 override
    p2 = Position("m", "X", "scalp", qty=1000, cost_sol=0.001, sl_override=0.05)
    assert p2.should_exit(0.94e-6, ep) == (True, "sl")      # -6% <= -0.05 override


def test_pnl_stats_breakeven_is_neither():
    closed = [
        ClosedTrade("a", "A", "scalp", 1, 2, 1.0, 1.0, 0, 1, "tp"),
        ClosedTrade("b", "B", "scalp", 1, 0.5, -0.5, -0.5, 0, 1, "sl"),
        ClosedTrade("c", "C", "scalp", 1, 1, 0.0, 0.0, 0, 1, "tp"),
    ]
    s = compute_stats(closed)
    assert s.n_trades == 3 and s.wins == 1 and s.losses == 1   # break-even is neither
    assert abs(s.total_pnl_sol - 0.5) < 1e-9


def test_max_drawdown():
    assert abs(max_drawdown([10, 12, 6, 8]) - 0.5) < 1e-9


def test_risk_daily_cap_trips_then_resets():
    s = Settings()
    rm = RiskManager(s)

    class PF:
        dl = s.risk.daily_loss_cap_sol + 1.0
        def daily_loss(self, pm, now=None): return self.dl
        def has_position(self, m): return False
        def open_count(self): return 0

    pf = PF()
    ok, _ = rm.can_open(pf, {}, "m")
    assert not ok and rm.halted          # cap breached -> halt
    pf.dl = 0.0
    ok2, _ = rm.can_open(pf, {}, "m")
    assert ok2 and not rm.halted          # next day under cap -> halt cleared


def test_rules_gates():
    s = Settings()
    good = Candidate(mint="m")
    good.liquidity_usd, good.vol_to_mcap_pct, good.buy_sell_ratio, good.unique_buyers = 20000, 40, 2.5, 60
    good.mint_revoked = good.freeze_revoked = True
    rules.evaluate(good, s)
    assert good.rule_passed

    honeypot = Candidate(mint="b")
    honeypot.liquidity_usd, honeypot.vol_to_mcap_pct, honeypot.buy_sell_ratio, honeypot.unique_buyers = 20000, 40, 2.5, 60
    honeypot.mint_revoked, honeypot.freeze_revoked = True, False
    rules.evaluate(honeypot, s)
    assert not honeypot.rule_passed and "freeze_not_revoked" in honeypot.rule_reasons

    nodata = Candidate(mint="z")          # all zero -> breadth gates must NOT fire
    rules.evaluate(nodata, s)
    assert "buyers_low" not in nodata.rule_reasons and "buy/sell_low" not in nodata.rule_reasons


def test_scorer_rule_fallback_bounds():
    sc = Scorer(Settings(), None)
    assert sc.kind == "rule"
    c = Candidate(mint="m")
    c.liquidity_usd, c.vol_to_mcap_pct, c.buy_sell_ratio, c.unique_buyers = 20000, 40, 2.5, 60
    c.features["vol_spike"] = 3.0
    sc.score(c)
    assert 0.0 <= c.score <= 1.0


def _mk(mode, vs, bsr, vmc, buyers, liq, safe_known=True):
    c = Candidate(mint="m"); c.mode = mode
    c.buy_sell_ratio, c.vol_to_mcap_pct, c.unique_buyers, c.liquidity_usd = bsr, vmc, buyers, liq
    c.features["vol_spike"] = vs
    if not safe_known:
        c.features["safety_unknown"] = True
    return c


def test_scorer_calibration_spread_monotonic_and_safety():
    s = Settings(); sc = Scorer(s, None)
    # reachability: a near-ideal known-safe HOLD reaches the (rescaled) slam-dunk tier
    ideal = _mk(MODE_HOLD, 3.0, 3.0, 40.0, 60, 30000.0, safe_known=True)
    sc.score(ideal)
    assert ideal.score >= s.rule_high_conf_threshold
    # floor: a weak candidate scores well below entry
    weak = _mk(MODE_SCALP, 0.4, 1.0, 5.0, 4, 800.0)
    sc.score(weak)
    assert weak.score < s.entry_threshold
    # monotonic: a strictly dominating candidate scores >= the dominated one
    hi = _mk(MODE_SCALP, 2.0, 2.0, 18.0, 40, 12000.0)
    lo = _mk(MODE_SCALP, 1.0, 1.5, 12.0, 20, 6000.0)
    sc.score(hi); sc.score(lo)
    assert hi.score >= lo.score
    # safety now matters: known-safe HOLD outscores the same candidate with safety unknown
    safe = _mk(MODE_HOLD, 1.5, 1.5, 10.0, 15, 5000.0, safe_known=True)
    unk = _mk(MODE_HOLD, 1.5, 1.5, 10.0, 15, 5000.0, safe_known=False)
    sc.score(safe); sc.score(unk)
    assert safe.score > unk.score


def test_scorer_weights_volume():
    # R2-a: 1h volume (vol_h1) is the #1 winner signal -> it must lift the score
    s = Settings(); sc = Scorer(s, None)
    base = _mk(MODE_SCALP, 2.0, 2.0, 15.0, 20, 12000.0)
    hi = _mk(MODE_SCALP, 2.0, 2.0, 15.0, 20, 12000.0); hi.features["vol_h1"] = 5000.0
    sc.score(base); sc.score(hi)
    assert hi.score > base.score


def test_scorer_gbm_path_not_rescaled():
    class StubGBM:
        def score(self, c):
            return 0.9
    sc = Scorer(Settings(), StubGBM())
    assert sc.kind == "gbm"
    c = Candidate(mint="m"); c.mode = MODE_SCALP
    assert abs(sc.score(c) - 0.9) < 1e-12      # GBM probability passes through — no affine rescale


def test_rule_verdict_size_scales_with_score():
    import asyncio

    from memebot.agent.brain import TradingBrain
    from memebot.agent.memory import AgentMemory
    from memebot.storage.db import Storage
    brain = TradingBrain(Settings(), AgentMemory(Storage(":memory:")), None)   # entry 0.50

    async def run():
        strong = Candidate(mint="a"); strong.score = 0.95; strong.mode = MODE_SCALP
        v = await brain.decide(strong)
        assert v.action == "buy" and v.size_pct > 0.25        # conviction->size restored by the rescale
        edge = Candidate(mint="b"); edge.score = 0.52; edge.mode = MODE_SCALP
        assert abs((await brain.decide(edge)).size_pct - 0.25) < 1e-9   # just above entry -> floor

    asyncio.run(run())


def test_has_momentum_guard():
    from memebot.main import Bot
    assert Bot._has_momentum(Candidate(mint="m")) is False        # no signal -> blind scalp
    c1 = Candidate(mint="m"); c1.price_change_m5 = 5.0
    assert Bot._has_momentum(c1) is True                          # 5m rising
    c2 = Candidate(mint="m"); c2.features["trend"] = {"dir": "rising"}
    assert Bot._has_momentum(c2) is True                          # rising short-term trend
    c3 = Candidate(mint="m"); c3.features["vol_spike"] = 2.0
    assert Bot._has_momentum(c3) is True                          # volume spike


def _setup_candidate(**kw):
    c = Candidate(mint="m")
    c.liquidity_usd = kw.get("liq", 15000.0); c.market_cap_usd = kw.get("mcap", 100000.0)
    c.buy_sell_ratio = kw.get("bsr", 2.0); c.unique_buyers = kw.get("buyers", 30)
    c.price_change_m5 = kw.get("m5", 5.0); c.price_change_h1 = kw.get("h1", 30.0)
    c.mint_revoked = kw.get("mint_rev", True); c.freeze_revoked = kw.get("freeze_rev", True)
    c.features["trend"] = {"dir": kw.get("dir", "rising"), "off_high_pct": kw.get("off_high", 5.0)}
    c.features["tech"] = {"ema_signal": kw.get("ema", "bull"), "breakout": kw.get("breakout", "up")}
    return c


def test_classify_setup():
    from memebot.signals.setup import classify_setup
    s = Settings()
    g, _why, q = classify_setup(_setup_candidate(), s)
    assert g == "good" and 0.0 <= q <= 1.0                              # converging healthy setup
    assert classify_setup(_setup_candidate(off_high=50.0), s)[0] == "bad"      # far off high = knife
    assert classify_setup(_setup_candidate(liq=2000.0), s)[0] == "bad"         # thin liquidity
    assert classify_setup(_setup_candidate(mcap=1_000_000.0), s)[0] == "bad"   # ghost liq/mcap (1.5%)
    assert classify_setup(_setup_candidate(h1=200.0, m5=-3.0), s)[0] == "bad"  # post-pump fade
    assert classify_setup(_setup_candidate(ema="bear"), s)[0] == "bad"         # breaking down
    assert classify_setup(_setup_candidate(freeze_rev=False), s)[0] == "bad"   # unsafe (honeypot)
    assert classify_setup(_setup_candidate(dir="unknown"), s)[0] == "marginal" # unknown trend never 'good'
    assert classify_setup(_setup_candidate(buyers=14), s)[0] == "marginal"     # thin breadth -> marginal


def test_setup_type():
    from memebot.constants import MODE_HOLD
    from memebot.signals.setup import setup_type
    s = Settings()
    gold = _setup_candidate(); gold.features["creator_holding_pct"] = 0.0        # dev out + rising
    assert setup_type(gold, s) == "creator_gold"
    hold = _setup_candidate(); hold.mode = MODE_HOLD; hold.features["creator_holding_pct"] = 50.0
    assert setup_type(hold, s) == "safe_hold"                                    # HOLD + revoked + rising
    risky = _setup_candidate(); risky.top5_concentration_pct = 95.0              # warn-zone concentration
    assert setup_type(risky, s) == "risky"                                      # caution dominates
    mom = _setup_candidate(); mom.freeze_revoked = None                         # not safe_hold (freeze unknown), scalp
    assert setup_type(mom, s) == "momentum"
    weak = _setup_candidate(dir="choppy", m5=-1.0, h1=0.0)                       # no sustained strength
    assert setup_type(weak, s) == "unproven"


def test_liq_collapsed_guard():
    from memebot.main import Bot
    # W2: rug exit when the pool drains below the floor OR below frac of entry liquidity
    assert Bot._liq_collapsed(20000.0, entry_liq=18000.0, min_liq=3000.0, frac=0.5) is False  # healthy
    assert Bot._liq_collapsed(8000.0, entry_liq=18000.0, min_liq=3000.0, frac=0.5) is True     # <50% of entry 18k
    assert Bot._liq_collapsed(2500.0, entry_liq=4000.0, min_liq=3000.0, frac=0.5) is True      # below the 3k floor
    assert Bot._liq_collapsed(0.0, entry_liq=18000.0, min_liq=3000.0, frac=0.5) is False       # no snapshot -> blackout's job
    assert Bot._liq_collapsed(10000.0, entry_liq=0.0, min_liq=3000.0, frac=0.5) is False       # unknown entry liq, above floor


def test_concentration_rose_guard():
    from memebot.main import Bot
    # W6: cut a held position when concentration RISES >= threshold pp above entry (rug-prep)
    assert Bot._concentration_rose(95.0, 85.0, 8.0) is True       # +10pp -> rug-prep cut
    assert Bot._concentration_rose(90.0, 85.0, 8.0) is False      # +5pp < 8 -> hold
    assert Bot._concentration_rose(80.0, 85.0, 8.0) is False      # fell -> not a rise
    assert Bot._concentration_rose(95.0, -1.0, 8.0) is False      # entry unknown -> don't act
    assert Bot._concentration_rose(None, 85.0, 8.0) is False      # current unknown -> don't act
    assert Bot._concentration_rose(99.0, 85.0, 0.0) is False      # threshold 0 disables


def test_concentration_vetoed_guard():
    from memebot.main import Bot
    # W3: veto a BUY only when concentration is KNOWN and exceeds the extreme threshold (90)
    assert Bot._concentration_vetoed(97.6, 90.0) is True      # near-total whale dominance -> veto
    assert Bot._concentration_vetoed(90.0, 90.0) is False     # exactly at threshold -> not over -> allow
    assert Bot._concentration_vetoed(42.0, 90.0) is False     # healthy distribution -> allow
    assert Bot._concentration_vetoed(None, 90.0) is False     # unknown (public RPC) -> never veto on missing data


def test_trade_scalp_off_by_default():
    # W2: SCALP buy lane disabled by default (0/39 wins in the full-history reconstruction)
    assert Settings().trade_scalp is False
    assert Settings.load().trade_scalp is False


def test_candidate_from_snapshot_field_mapping():
    # W2 restructure: the extracted _candidate_from_snapshot must map a DexScreener snapshot onto a
    # Candidate exactly as the old inline build did (self is unused, so call it unbound with None).
    from types import SimpleNamespace
    from memebot.main import Bot
    from memebot.data.creator_history import CreatorHistory
    from memebot.data.token_state import TokenState
    from memebot.feed.dexscreener import PairSnapshot
    stub = SimpleNamespace(creator_history=CreatorHistory())   # _candidate_from_snapshot only uses self.creator_history
    snap = PairSnapshot(mint="M", symbol="TOK", pair_address="p", dex_id="raydium",
                        price_usd=1.5e-4, price_native=1e-6, liquidity_usd=15000.0,
                        volume_h1=5000.0, volume_h24=0.0, buys_h1=80, sells_h1=40,
                        price_change_m5=5.0, price_change_h1=20.0, market_cap=60000.0,
                        fdv=0.0, pair_created_at=0)
    st = TokenState("M")
    c = Bot._candidate_from_snapshot(stub, st, snap)
    assert st.migrated is True and c.mode == MODE_HOLD              # non-pumpfun DEX -> migrated -> hold
    assert c.price_sol == 1e-6 and c.price_usd == 1.5e-4
    assert c.liquidity_usd == 15000.0 and c.market_cap_usd == 60000.0
    assert abs(c.buy_sell_ratio - 2.0) < 1e-9 and c.unique_buyers == 80
    assert c.price_change_m5 == 5.0 and c.price_change_h1 == 20.0
    assert c.features["vol_h1"] == 5000.0 and "trend" in c.features and "tech" in c.features
    assert c.lp_burned_pct is None                                  # LP-lock: a NON-pump (raydium) pool stays unknown
    # LP-lock: a pump-AMM (PumpSwap) graduate has protocol-burned LP -> lp_burned_pct = 100, no RPC
    pump = PairSnapshot(mint="P", symbol="P", pair_address="p", dex_id="pumpswap",
                        price_usd=1e-4, price_native=1e-6, liquidity_usd=20000.0, volume_h1=5000.0,
                        volume_h24=0.0, buys_h1=50, sells_h1=25, price_change_m5=1.0, price_change_h1=10.0,
                        market_cap=50000.0, fdv=0.0, pair_created_at=0)
    cp = Bot._candidate_from_snapshot(stub, TokenState("P"), pump)
    assert cp.lp_burned_pct == 100.0
    # a pumpfun (curve-only) snapshot stays non-migrated -> scalp (the cohort the W2 veto blocks)
    curve = PairSnapshot(mint="C", symbol="", pair_address="", dex_id="pumpfun",
                         price_usd=0.0, price_native=0.0, liquidity_usd=0.0, volume_h1=0.0,
                         volume_h24=0.0, buys_h1=0, sells_h1=0, price_change_m5=0.0,
                         price_change_h1=0.0, market_cap=0.0, fdv=0.0, pair_created_at=0)
    st2 = TokenState("C")
    c2 = Bot._candidate_from_snapshot(stub, st2, curve)
    assert st2.migrated is False and c2.mode == MODE_SCALP


def test_observe_held_closes_on_collapse_else_observes():
    # W2 integration: _observe_held must CLOSE a held mint whose pool collapsed (liq_collapse) and
    # OBSERVE (log, not close) a healthy one — the held-position P4-label + rug-cut path.
    import asyncio
    from memebot.main import Bot
    from memebot.data.token_state import TokenState
    from memebot.feed.dexscreener import PairSnapshot

    bot = Bot(Settings(db_path=":memory:", llm_backend="off"))
    bot.storage.connect()
    closes = []
    async def fake_close(mint, reason):
        closes.append((mint, reason))
    bot._close_position = fake_close                       # don't touch the real executor/portfolio

    def snap(mint, liq):
        return PairSnapshot(mint=mint, symbol="T", pair_address="", dex_id="raydium",
                            price_usd=1e-4, price_native=1e-6, liquidity_usd=liq, volume_h1=5000.0,
                            volume_h24=0.0, buys_h1=50, sells_h1=25, price_change_m5=1.0,
                            price_change_h1=10.0, market_cap=50000.0, fdv=0.0, pair_created_at=0)
    for mint in ("HEALTHY", "RUG"):                        # both entered at 18k liquidity
        bot.portfolio.apply_buy(Fill(mint, "buy", 1000.0, 0.4, 4e-4, 0.01, 0.03),
                                symbol=mint, mode="hold", features={"liquidity_usd": 18000.0}, score=0.6)
    st_h, st_r = TokenState("HEALTHY"), TokenState("RUG")
    snaps = {"HEALTHY": snap("HEALTHY", 18000.0), "RUG": snap("RUG", 5000.0)}   # RUG drained to <50% of 18k
    asyncio.run(bot._observe_held([st_h, st_r], snaps))
    assert ("RUG", "liq_collapse") in closes               # collapsed pool -> force-closed
    assert all(m != "HEALTHY" for m, _ in closes)          # healthy -> observed, never closed
    n_obs = bot.storage._conn.execute("SELECT COUNT(*) FROM observations WHERE mint='HEALTHY'").fetchone()[0]
    assert n_obs == 1                                       # healthy held mint logged a P4 observation
    bot.storage.close()


def test_trend_broke_guard():
    from memebot.main import Bot
    # rising -> never a break
    assert Bot._trend_broke({"n": 8, "dir": "rising", "chg_pct": 5.0}, {"breakout": "up", "ema_signal": "bull"}) is False
    # a SUSTAINED falling trend WITH real magnitude -> a confirmed break
    assert Bot._trend_broke({"n": 6, "dir": "falling", "chg_pct": -15.0}, {}) is True
    # falling but SHALLOW (no magnitude) -> jitter, NOT a break (this was the 0/14 fade leak)
    assert Bot._trend_broke({"n": 6, "dir": "falling", "chg_pct": -2.0}, {}) is False
    # falling, big drop, but too FEW polls -> noise, don't fade
    assert Bot._trend_broke({"n": 2, "dir": "falling", "chg_pct": -20.0}, {}) is False
    # a CORROBORATED breakdown (both breakdown AND ema bear) -> a break
    assert Bot._trend_broke({}, {"breakout": "down", "ema_signal": "bear"}) is True
    # a SINGLE tech flag alone is NOT enough anymore (was a false trigger)
    assert Bot._trend_broke({}, {"breakout": "down"}) is False
    assert Bot._trend_broke({}, {"ema_signal": "bear"}) is False


def test_data_backed_setup_guard():
    from memebot.main import Bot
    assert Bot._data_backed_setup(Candidate(mint="m")) is False    # liquidity 0 -> blind, skip
    rich = Candidate(mint="m"); rich.liquidity_usd = 8000.0
    assert Bot._data_backed_setup(rich) is True                    # real DexScreener data -> assessable


def test_scam_likelihood():
    s = Settings()
    clean = Candidate(mint="m"); clean.mint_revoked = True; clean.freeze_revoked = True
    clean.lp_burned_pct = 96.0; clean.top5_concentration_pct = 20.0
    assert rules.scam_likelihood(clean, s) == 0.0
    honeypot = Candidate(mint="m"); honeypot.mint_revoked = True; honeypot.freeze_revoked = False
    assert abs(rules.scam_likelihood(honeypot, s) - 0.35) < 1e-9       # freeze ACTIVE
    both = Candidate(mint="m"); both.mint_revoked = False; both.freeze_revoked = False
    assert abs(rules.scam_likelihood(both, s) - 0.70) < 1e-9           # both ACTIVE
    assert abs(rules.scam_likelihood(Candidate(mint="m"), s) - 0.20) < 1e-9   # both UNKNOWN -> mild
    midc = Candidate(mint="m"); midc.mint_revoked = True; midc.freeze_revoked = True
    midc.top5_concentration_pct = 92.0
    assert abs(rules.scam_likelihood(midc, s) - 0.10) < 1e-9           # R2-b: >warn(90) & <=96 -> graded +0.10
    worst = Candidate(mint="m"); worst.mint_revoked = False; worst.freeze_revoked = False
    worst.lp_burned_pct = 0.0; worst.top5_concentration_pct = 99.0
    assert rules.scam_likelihood(worst, s) == 1.0                      # >96 (+0.20) atop everything -> capped at 1.0
    spammer = Candidate(mint="m"); spammer.mint_revoked = True; spammer.freeze_revoked = True
    spammer.features["creator_launches"] = 67                          # > creator_spam_launches(40)
    assert abs(rules.scam_likelihood(spammer, s) - 0.15) < 1e-9        # W5: serial-spam creator -> +0.15 soft penalty
    okdev = Candidate(mint="m"); okdev.mint_revoked = True; okdev.freeze_revoked = True
    okdev.features["creator_launches"] = 20                            # a 20-launch creator (the +237% winner) is NOT penalized
    assert rules.scam_likelihood(okdev, s) == 0.0


def test_liquidity_trend():
    from memebot.data.token_state import TokenState
    st = TokenState("m")
    assert st.liquidity_trend()["dir"] == "unknown"          # < 3 polls
    for liq in (10000, 9000, 8000, 7000):                    # draining
        st.record_liquidity(liq)
    assert st.liquidity_trend()["dir"] == "falling" and st.liquidity_trend()["chg_pct"] < 0
    st2 = TokenState("m2")
    for liq in (5000, 6000, 7000, 8000):                     # filling
        st2.record_liquidity(liq)
    assert st2.liquidity_trend()["dir"] == "rising"
    st3 = TokenState("m3")
    st3.record_liquidity(0.0); st3.record_liquidity(-5.0)    # non-positive ignored
    assert st3.liquidity_trend()["dir"] == "unknown"


def test_tape_signals_creator_dump_and_sniper_share():
    from memebot.data.token_state import TokenState
    from memebot.models import TradeEvent
    # creator_dump_ratio: DEV bought 1.0, sold 0.5 -> dumped 50% on the live tape
    st = TokenState("m")
    st.apply_trade(TradeEvent(mint="m", trader="DEV", side="buy", sol_amount=1.0, token_amount=1000.0, ts=1.0))
    st.apply_trade(TradeEvent(mint="m", trader="DEV", side="sell", sol_amount=0.5, token_amount=500.0, ts=2.0))
    assert abs(st.creator_dump_ratio("DEV") - 0.5) < 1e-9
    assert st.creator_dump_ratio("NEVER_TRADED") is None      # can't assess a wallet we never saw buy
    assert st.creator_dump_ratio("") is None
    # sniper_share: first 2 wallets capture almost all buy volume
    st2 = TokenState("m2")
    plan = [("A", 5.0), ("B", 5.0), ("C", 0.1), ("D", 0.1), ("E", 0.1), ("F", 0.1), ("G", 0.1), ("H", 0.1)]
    for i, (w, sol) in enumerate(plan):
        st2.apply_trade(TradeEvent(mint="m2", trader=w, side="buy", sol_amount=sol, token_amount=sol * 1000, ts=float(i)))
    assert st2.sniper_share(top_n=2) > 0.9                    # A+B own >90% of buy volume -> sniper-dominated
    st3 = TokenState("m3")                                    # too few buys -> unknown
    for i in range(3):
        st3.apply_trade(TradeEvent(mint="m3", trader=f"W{i}", side="buy", sol_amount=1.0, token_amount=1000.0, ts=float(i)))
    assert st3.sniper_share() is None


def test_bundle_share():
    from memebot.data.token_state import TokenState
    from memebot.models import TradeEvent
    st = TokenState("m")
    for w in ("A", "B", "C", "D"):           # 4 distinct wallets buy in the SAME instant -> a bundle
        st.apply_trade(TradeEvent(mint="m", trader=w, side="buy", sol_amount=2.0, token_amount=2000.0, ts=1.0))
    for i, w in enumerate(("E", "F", "G", "H"), start=1):   # organic buys spread out in time
        st.apply_trade(TradeEvent(mint="m", trader=w, side="buy", sol_amount=0.5, token_amount=500.0, ts=10.0 + i * 5))
    assert abs(st.bundle_share(window_s=2.0, min_cluster=3) - 0.8) < 1e-9   # 8 bundled / 10 total
    st2 = TokenState("m2")                    # all organic (spread out) -> 0 bundled
    for i in range(8):
        st2.apply_trade(TradeEvent(mint="m2", trader=f"W{i}", side="buy", sol_amount=1.0, token_amount=1000.0, ts=float(i * 10)))
    assert st2.bundle_share(window_s=2.0, min_cluster=3) == 0.0
    st3 = TokenState("m3")                    # too few buys -> unknown
    for i in range(3):
        st3.apply_trade(TradeEvent(mint="m3", trader=f"W{i}", side="buy", sol_amount=1.0, token_amount=1000.0, ts=float(i)))
    assert st3.bundle_share() is None


def test_buyer_growth_velocity():
    from memebot.data.token_state import TokenState
    st = TokenState("m")
    assert st.buyer_trend() == {"n": 0, "growth": 0.0}       # no obs -> neutral
    for b in (10, 12, 14):                                   # rolling 1h buy count rising -> accelerating
        st.record_buyers(b)
    bt = st.buyer_trend()
    assert bt["n"] == 3 and bt["growth"] > 0                 # positive breadth velocity
    st2 = TokenState("m2")
    for b in (30, 20, 10):                                   # 1h buy count falling -> fading breadth
        st2.record_buyers(b)
    assert st2.buyer_trend()["growth"] < 0                   # negative velocity
    st3 = TokenState("m3")
    st3.record_buyers(5); st3.record_buyers(5)               # < 3 obs -> not enough to call it
    assert st3.buyer_trend() == {"n": 2, "growth": 0.0}


def test_smart_money_ledger():
    from memebot.data.smart_money import SmartMoney
    sm = SmartMoney(min_closed=2, min_pnl_sol=0.05, win_bar=0.55)
    # WINNER wallet: buys 1.0, sells back 2.0 across two tokens (net +2.0 over 2 closes, 100% win)
    sm.on_trade("WIN", "t1", "buy", 1.0, 1000.0)
    sm.on_trade("WIN", "t1", "sell", 2.0, 1000.0)        # +1.0 realized, position closed
    sm.on_trade("WIN", "t2", "buy", 1.0, 1000.0)
    sm.on_trade("WIN", "t2", "sell", 2.0, 1000.0)        # +1.0 realized
    assert sm.is_smart("WIN")                             # 2 closes, +2.0 pnl, 100% win -> smart
    # LOSER wallet: buys 1.0, dumps for 0.2 twice (net -1.6, 0% win)
    sm.on_trade("LOSE", "t1", "buy", 1.0, 1000.0)
    sm.on_trade("LOSE", "t1", "sell", 0.2, 1000.0)
    sm.on_trade("LOSE", "t3", "buy", 1.0, 1000.0)
    sm.on_trade("LOSE", "t3", "sell", 0.2, 1000.0)
    assert not sm.is_smart("LOSE")                        # net-negative -> not smart
    # too few closes -> not yet trustworthy even if profitable
    sm.on_trade("NEW", "t1", "buy", 1.0, 1000.0)
    sm.on_trade("NEW", "t1", "sell", 5.0, 1000.0)
    assert not sm.is_smart("NEW")                         # only 1 close < min_closed=2
    assert sm.is_smart("UNSEEN") is False                 # never traded
    assert sm.smart_count() == 1                          # just WIN
    # partial sell realizes proportional PnL (sell half the qty -> half the cost basis)
    sm2 = SmartMoney(min_closed=1, min_pnl_sol=0.0, win_bar=0.0)
    sm2.on_trade("P", "t", "buy", 2.0, 1000.0)            # cost 2.0 for 1000 tokens
    sm2.on_trade("P", "t", "sell", 1.5, 500.0)           # sell half: cost_removed=1.0, pnl=+0.5
    snap = {w: (pnl, cl, wn) for (w, pnl, cl, wn) in sm2.snapshot()}
    assert abs(snap["P"][0] - 0.5) < 1e-9 and snap["P"][1] == 1


def test_smart_money_persistence_roundtrip():
    from memebot.data.smart_money import SmartMoney
    a = SmartMoney(min_closed=2, min_pnl_sol=0.05, win_bar=0.55)
    a.on_trade("W", "t1", "buy", 1.0, 1000.0); a.on_trade("W", "t1", "sell", 2.0, 1000.0)
    a.on_trade("W", "t2", "buy", 1.0, 1000.0); a.on_trade("W", "t2", "sell", 2.0, 1000.0)
    rows = a.snapshot()
    b = SmartMoney(min_closed=2, min_pnl_sol=0.05, win_bar=0.55)
    b.load(rows)                                          # rebuild reputation from persisted rows
    assert b.is_smart("W")                                # the track record survived the round-trip


def test_funder_registry_clustering():
    from memebot.data.funder_history import FunderRegistry
    fr = FunderRegistry()
    assert fr.funder_creator_count("X") is None              # unresolved -> None (no penalty on missing)
    fr.record("c1", "TREASURY"); fr.record("c2", "TREASURY"); fr.record("c3", "TREASURY")
    fr.record("c4", "OTHER")
    assert fr.funder_creator_count("c1") == 3                # 3 rotated creators share one treasury
    assert fr.funder_creator_count("c4") == 1               # OTHER funded only c4
    assert fr.resolved("c1") and not fr.resolved("zz")
    fr.record("c1", "DIFFERENT")                             # idempotent: a wallet's funder is immutable
    assert fr.funder_of("c1") == "TREASURY"
    fr2 = FunderRegistry(); fr2.load(fr.snapshot())          # persistence roundtrip
    assert fr2.funder_creator_count("c2") == 3 and fr2.funder_of("c4") == "OTHER"


def test_metadata_registry_duplication():
    from memebot.data.metadata_history import MetadataRegistry, _key
    assert _key(" A ", "b") == "a|b" and _key("", "x") == ""      # normalized; empty name -> uncounted
    mr = MetadataRegistry()
    assert mr.reuse_count("Doge", "DOGE") == 0
    mr.record("Doge", "DOGE"); mr.record("doge", " doge "); mr.record("DOGE", "Doge")  # same branding, 3 mints
    assert mr.reuse_count("DOGE ", "doge") == 3                   # case/space-insensitive
    assert mr.reuse_count("Pepe", "PEPE") == 0                    # different branding
    mr.record("", "X")                                           # empty name -> not counted
    assert mr.reuse_count("", "X") == 0
    mr2 = MetadataRegistry(); mr2.load({"doge|doge": 5})         # seed roundtrip
    assert mr2.reuse_count("Doge", "Doge") == 5


def test_scam_likelihood_name_reuse():
    s = Settings()
    c = Candidate(mint="m"); c.mint_revoked = True; c.freeze_revoked = True
    c.lp_burned_pct = 96.0; c.top5_concentration_pct = 20.0
    assert rules.scam_likelihood(c, s) == 0.0
    c.features["name_reuse_count"] = 10                          # > name_reuse_warn(6) -> +0.10
    assert abs(rules.scam_likelihood(c, s) - 0.10) < 1e-9
    c.features["name_reuse_count"] = 3                           # <= warn -> no penalty
    assert rules.scam_likelihood(c, s) == 0.0


def test_creator_sold_conditional():
    # User insight: creator-sold is a rug tell ONLY when the coin is weak; creator-sold + THRIVING is
    # the GOLD setup (dev overhang gone, organic demand) -> no penalty.
    s = Settings()
    c = Candidate(mint="m"); c.mint_revoked = True; c.freeze_revoked = True
    c.lp_burned_pct = 96.0; c.top5_concentration_pct = 20.0
    c.features["creator_holding_pct"] = 0.0                  # creator DUMPED its whole allocation
    assert abs(rules.scam_likelihood(c, s) - 0.15) < 1e-9   # weak (no momentum) -> rug penalty
    c.features["trend"] = {"dir": "rising"}                  # ...but the price is rising (sustained)
    assert rules.scam_likelihood(c, s) == 0.0               # creator-out + thriving = GOLD, no penalty
    c.features["trend"] = {"dir": "choppy"}; c.price_change_h1 = 12.0; c.price_change_m5 = 1.0  # h1 up, tape not rolling over
    assert rules.scam_likelihood(c, s) == 0.0
    c.features["trend"] = {"dir": "choppy"}; c.price_change_h1 = 0.0; c.price_change_m5 = 4.0  # 5-min blip only, h1 flat
    assert abs(rules.scam_likelihood(c, s) - 0.15) < 1e-9   # a momentary m5 pump does NOT excuse the dump
    # stale-green h1 (lagging launch pump) while the LIVE tape rolls over (m5<0) -> penalty STILL fires
    c.features["trend"] = {"dir": "choppy"}; c.price_change_h1 = 30.0; c.price_change_m5 = -8.0
    assert abs(rules.scam_likelihood(c, s) - 0.15) < 1e-9


def test_scam_likelihood_funder_cluster():
    s = Settings()
    c = Candidate(mint="m"); c.mint_revoked = True; c.freeze_revoked = True
    c.lp_burned_pct = 96.0; c.top5_concentration_pct = 20.0
    assert rules.scam_likelihood(c, s) == 0.0
    c.features["funder_creator_count"] = 5                   # in [3,200] operator band -> +0.15
    assert abs(rules.scam_likelihood(c, s) - 0.15) < 1e-9
    c.features["funder_creator_count"] = 2                   # below min -> not a cluster -> no penalty
    assert rules.scam_likelihood(c, s) == 0.0
    c.features["funder_creator_count"] = 500                 # above max -> INFRASTRUCTURE -> excluded
    assert rules.scam_likelihood(c, s) == 0.0


def test_get_funder():
    import asyncio
    from memebot.feed.helius_rpc import HeliusRPC
    h = HeliusRPC("https://x.helius-rpc.com/?api-key=k")

    async def fake_rpc(method, params):
        if method == "getSignaturesForAddress":
            return [{"signature": "newest"}, {"signature": "oldest"}]    # newest-first -> oldest = funding tx
        if method == "getTransaction":
            return {"transaction": {"message": {"instructions": [
                {"program": "vote", "parsed": {"type": "x", "info": {}}},   # ignored
                {"program": "system", "parsed": {"type": "transfer",
                    "info": {"source": "TREASURY", "destination": "CREATOR"}}}]}}}
        return None
    h._rpc = fake_rpc
    assert asyncio.run(h.get_funder("CREATOR")) == "TREASURY"
    assert asyncio.run(h.get_funder("")) is None                          # empty wallet -> None

    async def empty_rpc(method, params):
        return [] if method == "getSignaturesForAddress" else None
    h._rpc = empty_rpc
    assert asyncio.run(h.get_funder("CREATOR")) is None                   # no history -> None (never guesses)


def test_creator_funders_persistence_and_unresolved():
    from memebot.storage.db import Storage
    st = Storage(":memory:"); st.connect()
    assert st.load_creator_funders() == []
    st.save_creator_funders([("c1", "F1"), ("c2", "F1")])
    st.save_creator_funders([])                              # empty save is a no-op
    assert dict(st.load_creator_funders()) == {"c1": "F1", "c2": "F1"}
    st._conn.execute("INSERT INTO tokens(mint,creator) VALUES('m1','cA')")
    st._conn.execute("INSERT INTO tokens(mint,creator) VALUES('m2','cB')")
    st._conn.execute("INSERT INTO tokens(mint,creator) VALUES('m3','c1')")   # c1 already resolved
    st._conn.commit()
    assert set(st.unresolved_creators(10)) == {"cA", "cB"}  # c1 excluded (resolved)
    st.save_creator_funders([("cA", "F2")])
    assert st.unresolved_creators(10) == ["cB"]             # cA now resolved
    assert st.unresolved_creators(-1) == []                # negative is CLAMPED (not SQLite LIMIT -1 = unbounded)
    assert st.unresolved_creators(0) == []                 # 0 -> empty batch
    st.close()


def test_scam_likelihood_tape_signals():
    s = Settings()
    c = Candidate(mint="m"); c.mint_revoked = True; c.freeze_revoked = True
    c.lp_burned_pct = 96.0; c.top5_concentration_pct = 20.0
    assert rules.scam_likelihood(c, s) == 0.0
    c.features["sniper_share"] = 0.7         # >= 0.6 sniper-dominated -> +0.15
    assert abs(rules.scam_likelihood(c, s) - 0.15) < 1e-9
    c.features["sniper_share"] = 0.3         # broad -> no penalty
    c.features["creator_dump_ratio"] = 0.4   # >= 0.3 creator dumping on the tape -> +0.20
    assert abs(rules.scam_likelihood(c, s) - 0.20) < 1e-9
    c.features["creator_dump_ratio"] = 0.0   # holding -> no penalty
    c.features["bundle_share"] = 0.6         # >= 0.5 coordinated bundles -> +0.15 (research #1 signal)
    assert abs(rules.scam_likelihood(c, s) - 0.15) < 1e-9


def test_creator_holding_pct():
    import asyncio
    from memebot.feed.helius_rpc import HeliusRPC
    h = HeliusRPC("https://x.helius-rpc.com/?api-key=k")

    async def fake_rpc(method, params):
        if method == "getTokenSupply":
            return {"value": {"uiAmount": 1000.0}}
        if method == "getTokenAccountsByOwner":
            if params[0] == "DUMP":
                return {"value": []}                                  # creator holds nothing -> sold out
            return {"value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": {"uiAmount": 200.0}}}}}}]}
        return None
    h._rpc = fake_rpc

    async def run():
        assert abs(await h.creator_holding_pct("M", "HOLD") - 20.0) < 1e-9   # 200/1000 = 20%
        assert await h.creator_holding_pct("M", "DUMP") == 0.0              # sold out
        assert abs(await h.creator_holding_pct("M", "HOLD", supply=1000.0) - 20.0) < 1e-9  # supply reused
        assert await h.creator_holding_pct("M", "") is None                # no creator -> unknown
    asyncio.run(run())


def test_scam_likelihood_creator_dumped():
    # P8 (user's #1 tell): a creator that SOLD OUT its allocation -> graded scam penalty.
    s = Settings()
    c = Candidate(mint="m"); c.mint_revoked = True; c.freeze_revoked = True
    c.lp_burned_pct = 96.0; c.top5_concentration_pct = 20.0
    assert rules.scam_likelihood(c, s) == 0.0
    c.features["creator_holding_pct"] = 0.0     # creator SOLD OUT (dumped)
    assert abs(rules.scam_likelihood(c, s) - 0.15) < 1e-9
    c.features["creator_holding_pct"] = 15.0    # creator still holds -> no penalty (skin in the game)
    assert rules.scam_likelihood(c, s) == 0.0


def test_scam_likelihood_liquidity_drain():
    # P8: a pool DRAINING before entry is a rug-in-progress -> graded scam penalty (direction, not level).
    s = Settings()
    c = Candidate(mint="m"); c.mint_revoked = True; c.freeze_revoked = True
    c.lp_burned_pct = 96.0; c.top5_concentration_pct = 20.0
    assert rules.scam_likelihood(c, s) == 0.0
    c.features["liq_trend"] = {"dir": "falling", "chg_pct": -30.0}   # draining 30% > 20% threshold
    assert abs(rules.scam_likelihood(c, s) - 0.15) < 1e-9
    c.features["liq_trend"] = {"dir": "falling", "chg_pct": -5.0}    # shallow drain < threshold
    assert rules.scam_likelihood(c, s) == 0.0
    c.features["liq_trend"] = {"dir": "rising", "chg_pct": 30.0}     # filling -> no penalty
    assert rules.scam_likelihood(c, s) == 0.0


def test_scam_likelihood_creator_reputation():
    # P8: a creator whose OWN past tokens rugged often (over enough samples) is a graded danger.
    s = Settings()
    c = Candidate(mint="m"); c.mint_revoked = True; c.freeze_revoked = True
    c.lp_burned_pct = 96.0; c.top5_concentration_pct = 20.0
    assert rules.scam_likelihood(c, s) == 0.0                    # clean baseline
    c.features["creator_rug_rate"] = 0.6; c.features["creator_rep_n"] = 4
    assert abs(rules.scam_likelihood(c, s) - 0.15) < 1e-9        # high realized rug-rate -> +0.15
    c.features["creator_rep_n"] = 2                              # too few samples -> not weighed
    assert rules.scam_likelihood(c, s) == 0.0
    c.features["creator_rep_n"] = 5; c.features["creator_rug_rate"] = 0.2   # low rug-rate -> not weighed
    assert rules.scam_likelihood(c, s) == 0.0


def test_holder_quality():
    s = Settings()   # W4-recalibrated: holder_cut_pct=96, holder_good_pct=85 (fresh memecoins run 82-96%)
    assert rules.holder_quality(Candidate(mint="m"), s) == "unknown"   # concentration unknown
    healthy = Candidate(mint="m"); healthy.top5_concentration_pct = 80.0
    assert rules.holder_quality(healthy, s) == "healthy"               # <= good_pct(85) -> healthier end -> can HOLD
    conc = Candidate(mint="m"); conc.top5_concentration_pct = 97.0
    assert rules.holder_quality(conc, s) == "concentrated"             # >= cut_pct(96) -> extreme -> scalp & exit
    mixed = Candidate(mint="m"); mixed.top5_concentration_pct = 90.0
    assert rules.holder_quality(mixed, s) == "mixed"                   # 85 < 90 < 96


def test_expected_return_filter():
    from memebot.risk.limits import expected_return
    from memebot.utils.money import round_trip_cost_pct

    rtc = round_trip_cost_pct(FEES)
    assert abs(rtc - 2 * (FEES.pumpportal_pct + FEES.pumpfun_curve_pct + FEES.base_slippage_pct)) < 1e-12
    assert expected_return(0.8, 0.40, 0.18, rtc) > 0      # high conviction + good R:R -> trade
    assert expected_return(0.3, 0.40, 0.18, rtc) < 0      # low conviction -> negative EV -> skip
    assert expected_return(5.0, 0.40, 0.20, 0.0) == 0.40  # conviction clamped to 1 -> tp only
    assert expected_return(-1.0, 0.40, 0.20, 0.0) == -0.20  # clamped to 0 -> -sl only


def test_reeval_action():
    from memebot.risk.limits import reeval_action
    assert reeval_action(None, 85.0, 40.0) == "hold"     # unknown -> don't act
    assert reeval_action(92.0, 85.0, 40.0) == "cut"      # too concentrated -> cut fast
    assert reeval_action(30.0, 85.0, 40.0) == "extend"   # broad distribution -> extend
    assert reeval_action(60.0, 85.0, 40.0) == "hold"     # middling -> no change


def _risk(**risk_over):
    from memebot.risk.limits import RiskManager
    from dataclasses import replace
    s = Settings()
    s = replace(s, risk=replace(s.risk, **risk_over))
    return RiskManager(s)


def test_rolling_pnl_halt_catches_alternating_bleed():
    # N3: the consecutive-loss counter RESETS on any win, so {win, big loss} alternation never trips
    # it — but the rolling realized-PnL sum does. window=4, cap=1.0 SOL.
    rm = _risk(max_rolling_loss_sol=1.0, rolling_loss_window=4, max_consecutive_losses=5)
    day = 100 * 86_400 + 100.0                              # a fixed mid-day 'now' (no rollover between calls)
    rm.on_win(0.2); rm.on_loss("m", -0.6, now=day)         # net -0.8 over 4 alternating closes
    rm.on_win(0.2); rm.on_loss("m", -0.6, now=day)
    assert not rm.halted                                   # -0.8 < 1.0 cap, and the streak never reached 5
    rm.on_loss("m", -0.3, now=day)                         # window now {-0.6,0.2,-0.6,-0.3} = -1.3 <= -1.0
    assert rm.halted and rm.halt_reason == "pnl_bleed"     # the bleed halt the streak counter missed


def test_rolling_pnl_halt_not_lifted_by_a_single_win_but_by_day_rollover():
    rm = _risk(max_rolling_loss_sol=1.0, rolling_loss_window=3, max_consecutive_losses=0)
    day = 200 * 86_400 + 100.0
    for _ in range(3):
        rm.on_loss("m", -0.5, now=day)                     # -1.5 <= -1.0 -> halt
    assert rm.halted and rm.halt_reason == "pnl_bleed"
    rm.on_win(2.0)                                          # a big win does NOT lift the bleed halt
    assert rm.halted and rm.halt_reason == "pnl_bleed"
    # a UTC-day rollover (next can_open on a new day) clears it and the window
    import types
    pf = types.SimpleNamespace(daily_loss=lambda pm, now: 0.0, has_position=lambda m: False,
                               open_count=lambda: 0, positions={})
    ok, _ = rm.can_open(pf, {}, "x", now=day + 86_400)     # next day
    assert ok and not rm.halted                            # bleed halt cleared on rollover


def test_rolling_pnl_halt_disabled():
    rm = _risk(max_rolling_loss_sol=0.0, rolling_loss_window=4, max_consecutive_losses=0)  # both off
    day = 300 * 86_400 + 100.0
    for _ in range(10):
        rm.on_loss("m", -5.0, now=day)
    assert not rm.halted                                   # disabled -> never trips on PnL


def test_dark_concentration_size_shrink():
    from memebot.main import Bot
    assert Bot._dark_conc_size_frac(0.8, False, 0.5) == 0.8   # live concentration -> no shrink
    assert Bot._dark_conc_size_frac(0.8, True, 0.5) == 0.4    # dark -> halved
    assert Bot._dark_conc_size_frac(0.8, True, 1.0) == 0.8    # mult 1.0 -> no-op even when dark


def test_cohort_full():
    import types
    from memebot.main import Bot
    P = lambda c: types.SimpleNamespace(creator=c)          # noqa: E731
    positions = {"a": P("X"), "b": P("X"), "c": P("Y")}
    assert Bot._cohort_full(positions, "X", 2) is True       # 2 open from X -> cap reached
    assert Bot._cohort_full(positions, "Y", 2) is False      # only 1 from Y
    assert Bot._cohort_full(positions, "Z", 2) is False      # none open from Z
    assert Bot._cohort_full(positions, "X", 0) is False      # cap 0 -> disabled
    assert Bot._cohort_full(positions, "", 2) is False       # unknown creator -> never capped


def test_giveback_halt_realized_basis_and_rollover_clears():
    # N10 (realized): the giveback halt arms on cumulative REALIZED day-PnL + its peak, NOT mark-to-
    # market — so it's driven by closes (on_win/on_loss), immune to a single open position's swing,
    # and works regardless of book fullness. initial=10, arm_frac=0.05 -> arms after peak >= +0.5 SOL.
    import types
    rm = _risk(daily_giveback_frac=0.5, giveback_arm_frac=0.05, daily_loss_cap_sol=100.0,
               max_consecutive_losses=0, max_rolling_loss_sol=0.0)
    pf = types.SimpleNamespace(daily_loss=lambda pm, now: 0.0, has_position=lambda m: False,
                               open_count=lambda: 0, positions={})
    day = 600 * 86_400 + 100.0
    rm.can_open(pf, {}, "init", now=day)                      # initialize rm._day to `day`
    rm.on_win(0.6)                                            # realized day-PnL peaks at +0.6 (>= 0.5 arm)
    ok, _ = rm.can_open(pf, {}, "x", now=day)
    assert ok                                                 # still at/near peak -> allowed
    rm.on_loss("m", -0.4, now=day)                            # give back 0.4: peak 0.6 - now 0.2 = 0.4 >= 0.5*0.6=0.3
    ok, reason = rm.can_open(pf, {}, "y", now=day)
    assert not ok and reason == "giveback" and rm.halted      # banked a winning day then gave it back -> halt
    ok2, _ = rm.can_open(pf, {}, "z", now=day + 86_400)       # next day -> rollover clears it + resets the HWM
    assert ok2 and not rm.halted


def test_giveback_not_armed_below_floor():
    # A small winning day (peak < arm floor) never arms -> the halt can't strangle thin flow.
    import types
    rm = _risk(daily_giveback_frac=0.5, giveback_arm_frac=0.05, daily_loss_cap_sol=100.0,
               max_consecutive_losses=0, max_rolling_loss_sol=0.0)
    pf = types.SimpleNamespace(daily_loss=lambda pm, now: 0.0, has_position=lambda m: False,
                               open_count=lambda: 0, positions={})
    day = 700 * 86_400 + 100.0
    rm.can_open(pf, {}, "init", now=day)
    rm.on_win(0.3)                                            # peak +0.3 < 0.5 arm floor
    rm.on_loss("m", -0.3, now=day)                            # gave it ALL back, but never armed
    ok, _ = rm.can_open(pf, {}, "x", now=day)
    assert ok and not rm.halted


def test_features_consistency_and_tristate():
    c = Candidate(mint="m")
    c.liquidity_usd = 1000.0
    c.mint_revoked = None
    f = build_features(c)
    v = feature_vector(c)
    assert len(v) == len(FEATURE_NAMES)
    assert v == [f[n] for n in FEATURE_NAMES]
    assert f["mint_revoked"] == -1.0     # unknown -> -1
    assert "smart_money_share" in FEATURE_NAMES                  # P8 copy-trade column is present
    assert f["smart_money_share"] == -1.0                        # absent on the candidate -> unknown sentinel
    c.features["smart_money_share"] = 0.4
    assert build_features(c)["smart_money_share"] == 0.4         # populated value flows through
    assert "buyer_growth" in FEATURE_NAMES and build_features(c)["buyer_growth"] == 0.0   # N13 absent -> neutral 0
    c.features["buyer_growth"] = -2.5                            # a signed delta (negative is real, not a sentinel)
    assert build_features(c)["buyer_growth"] == -2.5


def test_feature_coverage_and_dark():
    from memebot.filter.features import (FEATURE_DEFAULTS, FEATURE_NAMES, dark_features,
                                         feature_coverage)
    i_liq = FEATURE_NAMES.index("liquidity_usd")
    rows = []
    for liq in (1000.0, 2000.0, 3000.0):
        r = [FEATURE_DEFAULTS[n] for n in FEATURE_NAMES]        # everything at its serve default...
        r[i_liq] = liq                                          # ...except a VARYING, non-default liquidity
        rows.append(r)
    cov = feature_coverage(rows)
    assert cov["liquidity_usd"]["known"] == 3 and cov["liquidity_usd"]["coverage"] == 1.0
    assert cov["liquidity_usd"]["constant"] is False
    assert cov["bundle_share"]["known"] == 0 and cov["bundle_share"]["constant"] is True   # all -1 -> dark
    dark = dark_features(cov)
    assert "bundle_share" in dark and "smart_money_share" in dark   # G3-tape columns: dead weight here
    assert "liquidity_usd" not in dark                             # it varies -> real signal
    assert feature_coverage([])["liquidity_usd"]["coverage"] == 0.0   # empty -> no crash, 0 coverage


# ── P7: principal-recovery de-risk + trade-flow (sell-pressure) monitoring ──────────────
class _FakeSellExec:
    """Minimal ExecutionBackend stub: a sell returns net proceeds = tokens*price*(1-cost),
    using the SAME one-way cost the de-risk sizing assumes -> proceeds exactly recover principal."""
    name = "fake"

    def __init__(self, price: float, cost: float = 0.0475) -> None:
        self.price, self.cost = price, cost

    async def get_price_sol(self, mint):
        return self.price

    async def buy(self, mint, sol_in):
        raise NotImplementedError

    async def sell(self, mint, tokens, *, include_migration=False):
        net = tokens * self.price * (1.0 - self.cost)
        return Fill(mint, "sell", tokens, net, self.price, tokens * self.price * self.cost, self.cost)


def test_sell_cost_pct():
    from memebot.utils.money import round_trip_cost_pct, sell_cost_pct
    one_way = sell_cost_pct(FEES)
    assert abs(one_way - (FEES.pumpportal_pct + FEES.pumpfun_curve_pct + FEES.base_slippage_pct)) < 1e-12
    assert abs(round_trip_cost_pct(FEES) - 2 * one_way) < 1e-12   # round-trip == 2x one-way


def test_derisk_fraction_recovers_principal():
    from memebot.utils.money import sell_cost_pct
    cost = sell_cost_pct(FEES)                                    # 0.0475 one-way
    p = Position("m", "S", MODE_HOLD, qty=1000.0, cost_sol=0.5)   # avg 5e-4
    # +100%: selling f recovers the 0.5 principal; net proceeds(f) == cost_sol -> tail rides free
    f = p.derisk_fraction(1e-3, cost, max_frac=0.9)
    assert 0.50 < f < 0.55                                        # ~0.525 of qty banks the principal
    assert abs(1000 * f * 1e-3 * (1 - cost) - 0.5) < 1e-9         # proceeds == principal exactly
    # +8% (small gain): full recovery would need >90% -> clamp to max_frac (bank most, small tail)
    assert p.derisk_fraction(5.4e-4, cost, max_frac=0.9) == 0.9
    assert p.derisk_fraction(0.0, cost) == 0.0                    # no price -> 0
    assert Position("m", "S", MODE_HOLD, qty=0.0, cost_sol=0.5).derisk_fraction(1e-3, cost) == 0.0


def test_sell_pressure_guard():
    from memebot.main import Bot
    assert Bot._sell_pressure(0.4, -5.0, 0.7) is True     # sells dominate + price rolling over
    assert Bot._sell_pressure(0.4, 2.0, 0.7) is False     # price still rising -> no corroboration
    assert Bot._sell_pressure(1.2, -5.0, 0.7) is False    # buys still dominate -> not sell-pressure
    assert Bot._sell_pressure(0.0, -5.0, 0.7) is False    # bsr unknown (0) -> no signal
    assert Bot._sell_pressure(0.4, -5.0, 0.0) is False    # threshold 0 disables


def test_derisk_if_profitable_banks_principal():
    import asyncio
    from memebot.main import Bot
    bot = Bot(Settings(db_path=":memory:", llm_backend="off"))
    bot.storage.connect()
    bot.executor = _FakeSellExec(price=1e-3)
    bot.portfolio.apply_buy(Fill("M", "buy", 1000.0, 0.5, 5e-4, 0.01, 0.03),
                            symbol="M", mode="hold", features={"liquidity_usd": 18000.0}, score=0.6)
    start_bal = bot.portfolio.sol_balance                        # 10 - 0.5 = 9.5
    asyncio.run(bot._derisk_if_profitable("M", 1e-3, "derisk_flow"))   # +100% -> de-risk
    pos = bot.portfolio.positions.get("M")
    assert pos is not None and pos.derisk_taken is True           # P7 fix: tail rides, marked one-time via derisk's OWN latch
    assert pos.partial_taken is False                             # ...and the clean-partial latch is left FREE to compose
    assert pos.breakeven_armed is False                           # free-roll (NOT the tight arm-trail)
    assert abs(bot.portfolio.sol_balance - bot.portfolio.initial_sol) < 1e-6   # principal fully recovered to cash
    assert pos.qty > 0                                            # a house-money tail remains
    qty_after = pos.qty
    asyncio.run(bot._derisk_if_profitable("M", 1e-3, "derisk_flow"))   # one-time: no double
    assert bot.portfolio.positions["M"].qty == qty_after
    bot.storage.close()
    assert start_bal < bot.portfolio.initial_sol                  # (sanity: we were down the principal pre-de-risk)


def test_derisk_if_profitable_noop_when_flat():
    import asyncio
    from memebot.main import Bot
    bot = Bot(Settings(db_path=":memory:", llm_backend="off"))
    bot.storage.connect()
    bot.executor = _FakeSellExec(price=5.1e-4)
    bot.portfolio.apply_buy(Fill("M", "buy", 1000.0, 0.5, 5e-4, 0.01, 0.03),
                            symbol="M", mode="hold", features={"liquidity_usd": 18000.0}, score=0.6)
    asyncio.run(bot._derisk_if_profitable("M", 5.1e-4, "derisk_flow"))   # +2% < derisk_tp_pct(8%) -> no-op
    assert bot.portfolio.positions["M"].derisk_taken is False
    bot.storage.close()


def test_partial_then_derisk_composes():
    # P7 fix (regression): a clean P3 partial must NOT block a later risk-triggered de-risk. Before the
    # fix, derisk shared the partial_taken latch, so banking a partial permanently disabled principal
    # recovery — the exact survival case de-risk exists for. With its own latch they COMPOSE.
    import asyncio
    from memebot.main import Bot
    bot = Bot(Settings(db_path=":memory:", llm_backend="off"))
    bot.storage.connect()
    bot.executor = _FakeSellExec(price=1e-3)
    bot.portfolio.apply_buy(Fill("M", "buy", 1000.0, 0.5, 5e-4, 0.01, 0.03),
                            symbol="M", mode="hold", features={"liquidity_usd": 18000.0}, score=0.6)
    asyncio.run(bot._take_partial("M"))                            # clean P3 partial fires first (latch="partial")
    pos = bot.portfolio.positions.get("M")
    assert pos is not None and pos.partial_taken is True and pos.derisk_taken is False
    qty_after_partial = pos.qty
    asyncio.run(bot._derisk_if_profitable("M", 1e-3, "derisk_conc"))   # THEN a risk flag fires -> de-risk still runs
    pos = bot.portfolio.positions.get("M")
    assert pos is not None and pos.derisk_taken is True           # the fix: de-risk composed past the partial
    assert pos.qty < qty_after_partial                            # it sold more (recovered remaining principal)
    bot.storage.close()


def test_n3_scaled_out_winner_not_flagged_as_loss():
    # Regression for the review's MED finding: the N3 halt must be fed the TRADE-LEVEL realized PnL
    # (ct.pnl_sol, full round-trip incl. the banked partial), NOT the final slice's return. A trade
    # that banks principal at +100% then lets a small tail dump is a NET WINNER, yet its closing slice
    # is negative — the old code called on_loss on that slice, wrongly incrementing the loss streak.
    import asyncio
    from memebot.main import Bot
    bot = Bot(Settings(db_path=":memory:", llm_backend="off"))
    bot.storage.connect()
    bot.executor = _FakeSellExec(price=1e-3)                       # +100% -> de-risk banks the principal
    bot.portfolio.apply_buy(Fill("M", "buy", 1000.0, 0.5, 5e-4, 0.01, 0.03),
                            symbol="M", mode="hold", features={"liquidity_usd": 18000.0}, score=0.6)
    asyncio.run(bot._derisk_if_profitable("M", 1e-3, "derisk"))   # principal recovered, free-roll tail rides
    assert bot.portfolio.positions["M"].derisk_taken
    bot.executor.price = 1e-4                                      # the tail then dumps -80%
    asyncio.run(bot._close_position("M", "sl"))
    ct = bot.portfolio.closed[-1]
    assert "M" not in bot.portfolio.positions and ct.pnl_sol > 0   # NET winner: principal banked at 2x > tail loss
    assert bot.risk.consecutive_losses == 0                        # the fix: trade-level PnL -> on_win, streak intact
    assert sum(bot.risk._recent_pnls) > 0                          # the rolling window saw the NET win, not the slice
    bot.storage.close()


def test_observe_held_derisks_on_sell_pressure():
    import asyncio
    import json
    from memebot.main import Bot
    from memebot.data.token_state import TokenState
    from memebot.feed.dexscreener import PairSnapshot
    bot = Bot(Settings(db_path=":memory:", llm_backend="off"))
    bot.storage.connect()
    bot.executor = _FakeSellExec(price=1e-3)
    bot.portfolio.apply_buy(Fill("WIN", "buy", 1000.0, 0.5, 5e-4, 0.01, 0.03),
                            symbol="WIN", mode="hold", features={"liquidity_usd": 18000.0}, score=0.6)
    st = TokenState("WIN")
    # healthy pool (no liq_collapse), but the TAPE turned: sells dominate (bsr 0.2) + price m5 < 0
    snap = PairSnapshot(mint="WIN", symbol="W", pair_address="", dex_id="raydium",
                        price_usd=1e-4, price_native=1e-3, liquidity_usd=18000.0, volume_h1=5000.0,
                        volume_h24=0.0, buys_h1=10, sells_h1=50, price_change_m5=-5.0,
                        price_change_h1=-10.0, market_cap=50000.0, fdv=0.0, pair_created_at=0)
    asyncio.run(bot._observe_held([st], {"WIN": snap}))
    pos = bot.portfolio.positions.get("WIN")
    assert pos is not None and pos.derisk_taken is True and pos.breakeven_armed is False  # de-risked, free-rolling
    row = bot.storage._conn.execute("SELECT features FROM observations WHERE mint='WIN'").fetchone()
    feats = json.loads(row[0])                                    # the tape signal is logged for P4 to LEARN
    assert feats["sell_pressure"] == 1.0 and abs(feats["bsr_h1"] - 0.2) < 1e-9
    bot.storage.close()


def test_exit_params_validate_derisk():
    assert not any("derisk" in w or "sell_pressure" in w for w in Settings().validate())  # defaults clean
    bad = Settings(exit=ExitParams(derisk_max_frac=1.0))         # no tail would remain
    assert any("derisk_max_frac" in w for w in bad.validate())


# ── Layer B: parallel decide -> sequential buy (_decide_and_open) ────────────────────────
def _mk_candidates(scores):
    out = []
    for i, s in enumerate(scores):
        c = Candidate(mint=f"M{i}", symbol=f"S{i}")
        c.mode = MODE_HOLD
        c.score = s
        out.append(c)
    return out


def test_decide_and_open_respects_max_positions_and_order():
    import asyncio
    from memebot.agent.schema import Verdict
    from memebot.config import RiskLimits
    from memebot.main import Bot
    bot = Bot(Settings(db_path=":memory:", llm_backend="off", risk=RiskLimits(max_positions=2)))
    bot.storage.connect()
    opened = []

    class BuyBrain:
        async def decide(self, c, self_state=None):
            return Verdict(action="buy", mode=c.mode, conviction=0.9, size_pct=0.5, reasoning="x")
    bot.brain = BuyBrain()

    async def fake_open(c, v):
        opened.append(c.mint)
        bot.portfolio.positions[c.mint] = Position(c.mint, c.symbol, c.mode, qty=1.0, cost_sol=0.1)
    bot._try_open = fake_open

    ranked = _mk_candidates([0.9, 0.8, 0.7, 0.6, 0.55])          # already score-desc
    asyncio.run(bot._decide_and_open(ranked, {}))
    assert opened == ["M0", "M1"]                                # exactly max_positions, the two highest-scored
    bot.storage.close()


def test_decide_and_open_skips_held_and_sub_entry():
    import asyncio
    from memebot.agent.schema import Verdict
    from memebot.config import RiskLimits
    from memebot.main import Bot
    bot = Bot(Settings(db_path=":memory:", llm_backend="off", risk=RiskLimits(max_positions=3)))
    bot.storage.connect()
    opened = []

    class BuyBrain:
        async def decide(self, c, self_state=None):
            return Verdict(action="buy", mode=c.mode, conviction=0.9, size_pct=0.5, reasoning="x")
    bot.brain = BuyBrain()

    async def fake_open(c, v):
        opened.append(c.mint)
        bot.portfolio.positions[c.mint] = Position(c.mint, c.symbol, c.mode, qty=1.0, cost_sol=0.1)
    bot._try_open = fake_open

    # M0 already held; M3 below entry (0.5) -> dropped. Only M1, M2 should open.
    bot.portfolio.positions["M0"] = Position("M0", "S0", MODE_HOLD, qty=1.0, cost_sol=0.1)
    ranked = _mk_candidates([0.9, 0.8, 0.7, 0.30])
    asyncio.run(bot._decide_and_open(ranked, {}))
    assert opened == ["M1", "M2"]                                # held skipped, sub-entry filtered out
    bot.storage.close()


def test_decide_and_open_concurrency_bounded():
    import asyncio
    from memebot.agent.schema import Verdict
    from memebot.main import Bot
    bot = Bot(Settings(db_path=":memory:", llm_backend="off", llm_parallel=2))
    bot.storage.connect()

    class CountingBrain:
        def __init__(self): self.inflight = 0; self.peak = 0
        async def decide(self, c, self_state=None):
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            await asyncio.sleep(0.01)                            # hold the slot so peers can enter
            self.inflight -= 1
            return Verdict(action="skip", mode=c.mode, conviction=0.1, reasoning="x")
    brain = CountingBrain()
    bot.brain = brain

    ranked = _mk_candidates([0.9, 0.85, 0.8, 0.75, 0.7])         # 5 eligible, llm_parallel=2
    asyncio.run(bot._decide_and_open(ranked, {}))
    assert brain.peak == 2                                       # never more than llm_parallel concurrent
    bot.storage.close()


# ── P8: miss-learning (regret) + patience ───────────────────────────────────────────────
def _mk_obs(mint, ts, *, price=1.0, liq=10000.0, rule_passed=True, score=0.6):
    c = Candidate(mint=mint, symbol=mint)
    c.ts = ts; c.price_usd = price; c.liquidity_usd = liq
    c.rule_passed = rule_passed; c.score = score; c.mode = MODE_HOLD
    return c


def test_missed_winner_obs_id_dedupe():
    import asyncio
    from memebot.storage.db import Storage
    st = Storage(":memory:"); st.connect()

    async def run():
        kw = dict(mint="M", symbol="S", mode="hold", entry_ts=0.0, entry_price=1.0,
                  detect_price=2.0, fwd_return=1.0, horizon_s=300.0, rule_passed=1, score=0.6,
                  features_json="{}")
        a = await st.log_missed_winner(obs_id=1, **kw)
        b = await st.log_missed_winner(obs_id=1, **kw)          # same obs -> ignored
        assert a is not None and b is None
        assert st._conn.execute("SELECT COUNT(*) FROM missed_winners").fetchone()[0] == 1

    asyncio.run(run())
    st.close()


def test_mark_miss_judged_idempotent():
    import asyncio
    from memebot.storage.db import Storage
    st = Storage(":memory:"); st.connect()

    async def run():
        await st.mark_miss_judged(5, "winner")
        await st.mark_miss_judged(5, "flat")                   # INSERT OR IGNORE -> first verdict stays
        rows = st._conn.execute("SELECT verdict FROM miss_judged WHERE obs_id=5").fetchall()
        assert len(rows) == 1 and rows[0][0] == "winner"

    asyncio.run(run())
    st.close()


def test_miss_candidates_window_and_exclusions():
    import asyncio
    import time as _t
    from memebot.storage.db import Storage
    st = Storage(":memory:"); st.connect()
    now = _t.time()

    async def run():
        await st.log_observation(_mk_obs("INWIN", now - 600), '{"liquidity_usd":10000}')
        await st.log_observation(_mk_obs("FRESH", now - 100), "{}")     # too fresh
        await st.log_observation(_mk_obs("OLD", now - 7200), "{}")      # too old
        await st.log_observation(_mk_obs("NOLIQ", now - 600, liq=0.0), "{}")  # curve-only price noise
        await st.log_observation(_mk_obs("BOUGHT", now - 600), "{}")
        await st.log_observation(_mk_obs("JUDGED", now - 600), "{}")
        await st.log_trade(mint="BOUGHT", symbol="BOUGHT", side="buy", mode="hold",
                           sol=1.0, tokens=1.0, price_sol=1.0, fee_sol=0.0, slippage_pct=0.0)
        jid = st._conn.execute("SELECT id FROM observations WHERE mint='JUDGED'").fetchone()[0]
        await st.mark_miss_judged(jid, "flat")
        cands = await st.miss_candidates(min_age_s=300, max_age_s=3600, limit=30, now=now)
        assert {c["mint"] for c in cands} == {"INWIN"}          # window + liq + bought + judged all enforced

    asyncio.run(run())
    st.close()


def test_winners_traits_aggregates():
    import asyncio
    from memebot.storage.db import Storage
    st = Storage(":memory:"); st.connect()

    async def run():
        rows = [
            (0.6, 1, '{"liquidity_usd": 10000, "vol_h1": 5000, "bsr_h1": 2.0}'),
            (1.0, 1, '{"liquidity_usd": 20000, "vol_h1": 8000, "bsr_h1": 3.0}'),
            (2.0, 0, '{"liquidity_usd": 30000, "vol_h1": 9000, "bsr_h1": 1.5}'),   # gate-failed winner
        ]
        for i, (fwd, gp, feats) in enumerate(rows):
            await st.log_missed_winner(obs_id=i + 1, mint=f"M{i}", symbol=f"S{i}", mode="hold",
                                       entry_ts=0.0, entry_price=1.0, detect_price=1.0 + fwd,
                                       fwd_return=fwd, horizon_s=300.0, rule_passed=gp, score=0.6,
                                       features_json=feats)
        tr = await st.winners_traits()
        assert tr["n"] == 3
        assert abs(tr["median_fwd_pct"] - 100.0) < 1e-9         # median([0.6,1.0,2.0]) = 1.0 -> 100%
        assert abs(tr["gate_failed_frac"] - 1 / 3) < 1e-9       # one of three was gate-rejected
        assert tr["median_liq_usd"] == 20000.0

    asyncio.run(run())
    st2 = Storage(":memory:"); st2.connect()
    assert asyncio.run(st2.winners_traits()) == {}             # empty -> {}
    st.close(); st2.close()


def test_wallets_persistence():
    from memebot.storage.db import Storage
    st = Storage(":memory:"); st.connect()
    assert st.load_wallets() == []                             # empty before any save
    st.save_wallets([("W", 2.0, 4, 3), ("L", -1.0, 5, 1)])
    st.save_wallets([("W", 3.5, 6, 5)])                        # upsert: W's row is replaced, not duplicated
    got = {w: (pnl, cl, wn) for (w, pnl, cl, wn) in st.load_wallets()}
    assert got == {"W": (3.5, 6, 5), "L": (-1.0, 5, 1)}
    st.save_wallets([])                                        # empty save is a no-op
    assert len(st.load_wallets()) == 2
    st.close()


class _FakeDex:
    def __init__(self, price, cover=True):
        self.price, self.cover = price, cover

    async def snapshots(self, mints):
        from memebot.feed.dexscreener import PairSnapshot
        if not self.cover:
            return {}, set()
        out = {m: PairSnapshot(mint=m, symbol=m, pair_address="", dex_id="raydium",
                               price_usd=self.price, price_native=self.price * 1e-3, liquidity_usd=10000.0,
                               volume_h1=5000.0, volume_h24=0.0, buys_h1=50, sells_h1=25,
                               price_change_m5=1.0, price_change_h1=10.0, market_cap=50000.0,
                               fdv=0.0, pair_created_at=0) for m in mints}
        return out, set(mints)


def _miss_bot_with_obs(dex):
    import time as _t
    from memebot.main import Bot
    bot = Bot(Settings(db_path=":memory:", llm_backend="off", miss_win_threshold_pct=50.0))
    bot.storage.connect()
    bot.dex = dex
    now = _t.time()

    async def seed():
        await bot.storage.log_observation(_mk_obs("WIN", now - 600, price=1.0), '{"liquidity_usd":10000}')
    import asyncio
    asyncio.run(seed())
    return bot


def test_miss_learn_once_records_winner():
    import asyncio
    bot = _miss_bot_with_obs(_FakeDex(price=1.6))               # +60% >= +50% bar
    asyncio.run(bot._miss_learn_once())
    row = bot.storage._conn.execute(
        "SELECT fwd_return FROM missed_winners WHERE mint='WIN'").fetchone()
    assert row is not None and abs(row[0] - 0.6) < 1e-6
    j = bot.storage._conn.execute(
        "SELECT verdict FROM miss_judged WHERE obs_id=(SELECT id FROM observations WHERE mint='WIN')"
    ).fetchone()
    assert j[0] == "winner"
    bot.storage.close()


def test_miss_learn_once_flat_marks_judged_no_record():
    import asyncio
    bot = _miss_bot_with_obs(_FakeDex(price=1.1))               # +10% < +50% bar
    asyncio.run(bot._miss_learn_once())
    assert bot.storage._conn.execute("SELECT COUNT(*) FROM missed_winners").fetchone()[0] == 0
    assert bot.storage._conn.execute(
        "SELECT verdict FROM miss_judged").fetchone()[0] == "flat"
    bot.storage.close()


def test_miss_learn_once_uncovered_not_judged():
    import asyncio
    bot = _miss_bot_with_obs(_FakeDex(price=1.6, cover=False))  # fetch failed / not covered
    asyncio.run(bot._miss_learn_once())
    assert bot.storage._conn.execute("SELECT COUNT(*) FROM missed_winners").fetchone()[0] == 0
    assert bot.storage._conn.execute("SELECT COUNT(*) FROM miss_judged").fetchone()[0] == 0  # eligible next pass
    bot.storage.close()


def test_readiness_gate():
    from memebot.readiness import evaluate_gate
    ok = evaluate_gate({"n": 120, "net": 0.5, "profit_factor": 1.5}, min_trades=100, min_pf=1.3)
    assert ok["ready"] is True and all(p for _, p, _ in ok["checks"])
    assert evaluate_gate({"n": 120, "net": -0.5, "profit_factor": 1.5}, min_trades=100, min_pf=1.3)["ready"] is False  # net<0
    assert evaluate_gate({"n": 50, "net": 0.5, "profit_factor": 1.5}, min_trades=100, min_pf=1.3)["ready"] is False   # too few
    assert evaluate_gate({"n": 120, "net": 0.5, "profit_factor": 1.1}, min_trades=100, min_pf=1.3)["ready"] is False  # pf low
    assert evaluate_gate({"n": 120, "net": 0.5, "profit_factor": float("inf")}, min_trades=100, min_pf=1.3)["ready"]  # inf ok
    assert evaluate_gate({}, min_trades=100, min_pf=1.3)["ready"] is False                                            # empty -> no crash


def test_readiness_log_persistence():
    from memebot.storage.db import Storage
    st = Storage(":memory:"); st.connect()
    assert st.readiness_history() == []
    st.save_readiness({"n": 100, "net": -1.5, "win_rate": 0.25, "profit_factor": 0.6}, labelable=140)
    st.save_readiness({"n": 110, "net": -1.2, "win_rate": 0.27, "profit_factor": 0.7}, labelable=160)
    st.save_readiness({"n": 5, "net": 0.0, "win_rate": 0.0, "profit_factor": float("inf")}, labelable=10)  # inf -> stored
    hist = st.readiness_history()
    assert len(hist) == 3 and hist[0][1] == 100                # oldest-first; first n=100
    assert hist[-1][4] == 1e9                                  # inf profit_factor stored as the sentinel
    assert st.readiness_history(limit=1)[-1][1] == 5           # newest when limited to 1
    st.close()


def test_tracking_candidates_window_and_exclusions():
    import asyncio
    import time as _t
    from memebot.storage.db import Storage
    st = Storage(":memory:"); st.connect()
    now = _t.time()

    async def run():
        await st.log_observation(_mk_obs("DUE", now - 300), '{"liquidity_usd":10000}')      # in window, due
        await st.log_observation(_mk_obs("MULTI", now - 600), '{"liquidity_usd":10000}')
        await st.log_observation(_mk_obs("MULTI", now - 300), '{"liquidity_usd":10000}')     # still maturing -> keep
        await st.log_observation(_mk_obs("FRESH", now - 10), '{"liquidity_usd":10000}')      # latest too recent -> revisit guard
        await st.log_observation(_mk_obs("OLDFIRST", now - 1500), '{"liquidity_usd":10000}') # first past window
        await st.log_observation(_mk_obs("NOLIQ", now - 300, liq=0.0), "{}")                 # not indexed
        await st.log_observation(_mk_obs("BOUGHT", now - 300), '{"liquidity_usd":10000}')
        await st.log_trade(mint="BOUGHT", symbol="BOUGHT", side="buy", mode="hold",
                           sol=1.0, tokens=1.0, price_sol=1.0, fee_sol=0.0, slippage_pct=0.0)
        cands = await st.tracking_candidates(window_s=1200, revisit_s=45, limit=30, now=now)
        assert {c["mint"] for c in cands} == {"DUE", "MULTI"}   # bought/fresh/old/no-liq all excluded
        # most-stale-first ordering (fair rotation): MULTI's last obs (now-300) == DUE's, tie is fine

    asyncio.run(run())
    st.close()


def test_tracking_stats_funnel():
    import asyncio
    import time as _t
    from memebot.storage.db import Storage
    st = Storage(":memory:"); st.connect()
    now = _t.time()

    async def run():
        await st.log_observation(_mk_obs("ONE", now - 100), '{"liquidity_usd":10000}')       # single point
        await st.log_observation(_mk_obs("SPAN", now - 600), '{"liquidity_usd":10000}')
        await st.log_observation(_mk_obs("SPAN", now - 100), '{"liquidity_usd":10000}')       # span 500s >= 300 -> labelable
        await st.log_observation(_mk_obs("SHORT", now - 250), '{"liquidity_usd":10000}')
        await st.log_observation(_mk_obs("SHORT", now - 200), '{"liquidity_usd":10000}')       # span 50s < 300 -> multi but not labelable
        await st.log_observation(_mk_obs("CURVE", now - 600, liq=0.0), "{}")                   # not indexed -> excluded
        stats = await st.tracking_stats(horizon_s=300.0)
        assert stats["indexed_mints"] == 3        # ONE, SPAN, SHORT (CURVE excluded)
        assert stats["multi_obs"] == 2            # SPAN, SHORT
        assert stats["labelable"] == 1            # only SPAN spans the 5m horizon

    asyncio.run(run())
    st.close()


def test_track_once_logs_forward_point_and_skips_dead():
    import asyncio
    import time as _t
    from memebot.main import Bot
    now = _t.time()

    async def run(dex, expect_new):
        bot = Bot(Settings(db_path=":memory:", llm_backend="off"))
        bot.storage.connect()
        bot.dex = dex
        await bot.storage.log_observation(_mk_obs("TRK", now - 300, price=1.0), '{"liquidity_usd":10000}')
        before = bot.storage._conn.execute("SELECT COUNT(*) FROM observations WHERE mint='TRK'").fetchone()[0]
        await bot._track_once()
        after = bot.storage._conn.execute("SELECT COUNT(*) FROM observations WHERE mint='TRK'").fetchone()[0]
        assert after == before + (1 if expect_new else 0)
        if expect_new:
            price, liq, feats = bot.storage._conn.execute(
                "SELECT price_usd, liquidity_usd, features FROM observations WHERE mint='TRK' "
                "ORDER BY id DESC LIMIT 1").fetchone()
            assert abs(price - 1.5) < 1e-9 and liq > 0 and feats != "{}"   # later price, indexed, labelable row
        bot.storage.close()

    asyncio.run(run(_FakeDex(price=1.5), True))                  # covered + indexed -> a forward point is logged
    asyncio.run(run(_FakeDex(price=1.5, cover=False), False))    # uncovered (dead/de-indexed) -> nothing fabricated


def test_pumpportal_trade_sub_rejection_detect():
    from memebot.feed.pumpportal_ws import PumpPortalFeed
    r = PumpPortalFeed._is_trade_sub_rejected
    assert r({"errors": "Minimum balance not met for PumpSwap websocket data."}) is True
    assert r({"message": "'subscribeTokenTrade' and 'subscribeAccountTrade' methods are only available "
                         "when connecting with an API key funded with at least 0.02 SOL."}) is True
    assert r({"message": "Successfully subscribed to token creation events."}) is False
    assert r({"txType": "buy", "mint": "X"}) is False and r({}) is False


def test_supervisor_backoff_and_giveup():
    from memebot.supervisor import _next_backoff, _should_give_up
    # a HEALTHY run (uptime >= min_uptime) resets the backoff to base
    assert _next_backoff(120.0, 48.0, min_uptime=60.0, base=3.0, cap=300.0) == 3.0
    # a fast crash DOUBLES the backoff, capped
    assert _next_backoff(5.0, 3.0, min_uptime=60.0, base=3.0, cap=300.0) == 6.0
    assert _next_backoff(5.0, 200.0, min_uptime=60.0, base=3.0, cap=300.0) == 300.0   # capped
    # give-up only after the cap of consecutive fast crashes; 0 = never give up
    assert _should_give_up(8, 8) is True and _should_give_up(7, 8) is False
    assert _should_give_up(99, 0) is False


def test_instance_lock_mutex():
    import os
    import tempfile
    from memebot.utils.singleton import InstanceLock, SingleInstanceError
    path = os.path.join(tempfile.mkdtemp(), "x.lock")
    a = InstanceLock(path); a.acquire()
    raised = False
    b = InstanceLock(path)
    try:
        b.acquire()
    except SingleInstanceError:
        raised = True
    assert raised                                  # a 2nd instance on the same DB lock is refused
    a.release()
    c = InstanceLock(path); c.acquire(); c.release()   # after release, a new instance can acquire


def test_feed_stale_guard():
    from memebot.main import Bot
    assert Bot._feed_stale(1000.0, 1000.0 + 200, 120.0) is True      # silent 200s > 120s -> stale
    assert Bot._feed_stale(1000.0, 1000.0 + 60, 120.0) is False      # within threshold
    assert Bot._feed_stale(1000.0, 1000.0 + 9999, 0.0) is False      # 0 disables the watchdog


def test_patience_line():
    from memebot.main import Bot
    bot = Bot(Settings(db_path=":memory:", llm_backend="off"))
    bot._patience = {"observed": 100, "ranked": 10, "bought": 2}
    line = bot._patience_line()
    assert "observed=100" in line and "bought=2" in line and "skip_rate=98.0%" in line


class _FakeBuyExec:
    """A buy that always fills, so the REAL _try_open path (incl. the `bought` counter) runs."""
    async def buy(self, mint, sol_in):
        return Fill(mint, "buy", sol_in / 1e-3, sol_in, 1e-3, 0.0, 0.0)

    async def get_price_sol(self, mint):
        return 1e-3

    async def sell(self, mint, tokens, *, include_migration=False):
        return Fill(mint, "sell", tokens, tokens * 1e-3, 1e-3, 0.0, 0.0)


def test_decide_and_open_real_try_open_veto_keeps_slot_and_counts_bought():
    # the REAL _try_open: a scalp candidate is vetoed (lane off) and must NOT consume a slot, so the
    # two HOLD candidates after it still open; the `bought` patience counter counts only real opens.
    import asyncio
    from memebot.agent.schema import Verdict
    from memebot.config import RiskLimits
    from memebot.main import Bot
    bot = Bot(Settings(db_path=":memory:", llm_backend="off", trade_scalp=False,
                       require_data_backed_setup=True, risk=RiskLimits(max_positions=2)))
    bot.storage.connect()
    bot.executor = _FakeBuyExec()

    class BuyBrain:
        async def decide(self, c, self_state=None):
            return Verdict(action="buy", mode=c.mode, conviction=0.9, size_pct=0.5, reasoning="x")
    bot.brain = BuyBrain()

    def mk(mint, score, mode):
        c = Candidate(mint=mint, symbol=mint); c.score = score; c.mode = mode
        c.liquidity_usd = 12000.0; c.price_usd = 1e-4; c.price_sol = 1e-3
        return c
    from memebot.constants import MODE_SCALP as SC
    ranked = [mk("SCALP", 0.95, SC), mk("H1", 0.9, MODE_HOLD), mk("H2", 0.8, MODE_HOLD)]
    asyncio.run(bot._decide_and_open(ranked, {}))
    assert "SCALP" not in bot.portfolio.positions                # scalp lane off -> vetoed, no slot used
    assert bot.portfolio.open_count() == 2                       # both HOLDs filled the 2 slots
    assert bot._patience["bought"] == 2                          # counter = real opens only
    bot.storage.close()


def test_miss_learn_cross_pass_one_row_per_mint():
    # the same mint observed in two passes must record ONE missed_winner (token-level), but BOTH
    # observations get judged (no re-fetch). Exercises the `seen` pre-seed across passes.
    import asyncio
    import time as _t
    from types import SimpleNamespace
    import memebot.main as mainmod
    from memebot.main import Bot
    bot = Bot(Settings(db_path=":memory:", llm_backend="off", miss_win_threshold_pct=50.0))
    bot.storage.connect()
    bot.dex = _FakeDex(price=1.6)
    base = _t.time()

    async def seed():
        await bot.storage.log_observation(_mk_obs("WIN", base - 600, price=1.0), '{"liquidity_usd":10000}')
        await bot.storage.log_observation(_mk_obs("WIN", base - 100, price=1.0), '{"liquidity_usd":10000}')
    asyncio.run(seed())

    saved = mainmod.time
    try:
        mainmod.time = SimpleNamespace(time=lambda: base)        # pass A: obs1 in window, obs2 too fresh
        asyncio.run(bot._miss_learn_once())
        mainmod.time = SimpleNamespace(time=lambda: base + 520)  # pass B: obs2 matures into the window
        asyncio.run(bot._miss_learn_once())
    finally:
        mainmod.time = saved

    n = bot.storage._conn.execute("SELECT COUNT(*) FROM missed_winners WHERE mint='WIN'").fetchone()[0]
    assert n == 1                                                # one row per mint across passes
    nj = bot.storage._conn.execute("SELECT COUNT(*) FROM miss_judged").fetchone()[0]
    assert nj == 2                                               # both observations judged (no re-fetch)
    bot.storage.close()
