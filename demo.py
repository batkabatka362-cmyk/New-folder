"""End-to-end demo of the full pipeline with the REAL local brain.

    python demo.py

Drives ONE token through every stage on a scripted price path so you can watch:
rule gate -> score -> brain verdict (Ollama) -> expected-return filter ->
compounding size -> paper buy (fees+slippage) -> price pump -> breakeven/TP exit
-> reflection -> learned lesson. Uses an in-memory DB (no conflict with a
running collector).
"""
from __future__ import annotations

import asyncio
import sys
import time

try:                                  # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from memebot.agent.backends import make_llm
from memebot.agent.brain import TradingBrain
from memebot.agent.memory import AgentMemory
from memebot.agent.schema import Verdict
from memebot.config import get_settings
from memebot.constants import MODE_SCALP
from memebot.execution.paper import PaperBackend
from memebot.execution.pricing import Quote
from memebot.filter import rules
from memebot.models import Candidate
from memebot.portfolio.portfolio import ExitParams, Portfolio
from memebot.risk.limits import RiskManager, expected_return
from memebot.signals.scoring import Scorer
from memebot.storage.db import Storage
from memebot.utils.money import round_trip_cost_pct


class FakePrices:
    """Scripted price source so the demo controls the entry/exit path."""
    def __init__(self, price_sol: float, liq_sol: float = 50.0) -> None:
        self.price_sol = price_sol
        self.liq_sol = liq_sol

    async def quote(self, mint: str) -> Quote:
        return Quote(mint, self.price_sol, self.liq_sol, self.price_sol * 150, "demo")


def hr() -> None:
    print("-" * 64)


async def main() -> None:
    s = get_settings()
    st = Storage(":memory:"); st.connect()
    mem = AgentMemory(st); await mem.load()
    llm = make_llm(s)
    brain = TradingBrain(s, mem, llm)
    scorer = Scorer(s, None)
    risk = RiskManager(s)
    pf = Portfolio(s.risk.initial_sol)
    ep = ExitParams()
    prices = FakePrices(price_sol=1e-6)
    exe = PaperBackend(prices, s.fees, s.risk.max_modeled_slippage_pct)

    print(f"brain: {llm.name if llm else 'rule-fallback'} | scorer: {scorer.kind} | "
          f"start equity: {pf.sol_balance:.2f} SOL")
    hr()

    # 1) a realistic candidate tuned to land in the 'uncertain' band -> the LLM decides
    c = Candidate(mint="DEMOpepe1111111111111111111111111111111111", symbol="PEPE2")
    c.mode = MODE_SCALP
    c.price_sol, c.price_usd = 1e-6, 1.5e-4
    c.liquidity_usd, c.market_cap_usd = 8000, 60000
    c.vol_to_mcap_pct, c.buy_sell_ratio, c.unique_buyers = 12, 1.6, 18
    c.mint_revoked = c.freeze_revoked = True
    c.lp_burned_pct = 96
    c.features["vol_spike"] = 1.0   # tuned so the calibrated score lands in the LLM band, not slam-dunk
    c.features["vol_h1"] = 1500.0   # R2-a: 1h volume (the #1 winner signal); ~winner-median so the score lands in the LLM band
    c.price_change_m5, c.price_change_h1 = 9.0, 22.0     # short/medium momentum the AI now reads
    c.features["trend"] = {"n": 12, "dir": "rising", "chg_pct": 14.0, "off_high_pct": 3.0}  # a constructive uptrend
    c.features["tech"] = {"n": 12, "ema_signal": "bull", "breakout": "up",                 # AI4 technicals
                          "res_dist_pct": -1.5, "sup_dist_pct": 18.0}
    print(f"[1] CANDIDATE {c.symbol}: liq=${c.liquidity_usd} vol/mcap={c.vol_to_mcap_pct}% "
          f"buy/sell={c.buy_sell_ratio} buyers={c.unique_buyers} mint/freeze=revoked lp_burn={c.lp_burned_pct}%")

    # 2) rule gate
    rules.evaluate(c, s)
    print(f"[2] rule gate: {'PASS' if c.rule_passed else 'FAIL ' + str(c.rule_reasons)}")

    # 3) score
    scorer.score(c)
    slam = s.rule_high_conf_threshold if scorer.kind == "rule" else s.high_conf_threshold
    print(f"[3] score: {c.score:.2f} (band: entry {s.entry_threshold} .. slam-dunk {slam})")
    hr()

    # 4) the brain decides — REAL local Ollama, with the FULL AI context (trend + indicators +
    #    the bot's own book). This exercises AI1/AI2/AI4 end-to-end.
    self_state = {"equity_pct": -1.2, "open": 1, "max_positions": 3, "daily_loss": 0.2,
                  "daily_cap": 1.5, "streak": 1, "recent_n": 6, "recent_win": 0.33}
    print("[4] brain deciding (local Ollama) with trend + indicators + YOUR BOOK...")
    t0 = time.time()
    v = await brain.decide(c, self_state)
    print(f"    VERDICT ({time.time() - t0:.1f}s): {v.action.upper()} [{v.mode}] conviction={v.conviction:.2f} "
          f"size={v.size_pct:.2f} tp={v.tp_pct} sl={v.sl_pct} via {v.source}")
    print(f"    reasoning: {v.reasoning[:240]}")
    hr()

    if v.action != "buy":
        print("    (brain chose not to buy — its disciplined default. Forcing a demo BUY to show the lifecycle.)")
        v = Verdict(action="buy", mode=MODE_SCALP, conviction=max(0.6, c.score),
                    size_pct=0.8, reasoning="demo lifecycle")

    # 5) expected-return filter + compounding size + paper buy
    mode = v.mode or c.mode
    tp = v.tp_pct or ep.scalp_tp_pct
    sl = v.sl_pct or ep.scalp_sl_pct
    er = expected_return(v.conviction, tp, sl, round_trip_cost_pct(s.fees))
    print(f"[5] expected return: {er:+.3f}  (min {s.min_expected_return}) -> "
          f"{'ENTER' if er >= s.min_expected_return else 'SKIP'}")
    equity = pf.equity({})
    liq_sol = c.liquidity_usd * c.price_sol / c.price_usd
    size = risk.position_size_sol(pf, equity, frac=(v.size_pct or 0.5), liquidity_sol=liq_sol)
    print(f"    size: {size:.4f} SOL  (equity {equity:.2f} × {s.risk.risk_per_trade_frac} × size_pct, "
          f"capped by liq {liq_sol:.0f} SOL × {s.risk.max_liquidity_frac})")
    prices.price_sol = c.price_sol
    fill = await exe.buy(c.mint, size)
    pf.apply_buy(fill, symbol=c.symbol, mode=mode,
                 tp_override=(v.tp_pct or None), sl_override=(v.sl_pct or None), note=v.reasoning)
    pos = pf.positions[c.mint]
    print(f"    BOUGHT {fill.tokens:,.0f} {c.symbol} for {fill.sol:.4f} SOL @ {fill.price_sol:.8f} "
          f"(slip {fill.slippage_pct:.1%}, fee {fill.fee_sol:.4f}) | cash {pf.sol_balance:.4f}")
    hr()

    # 6) scripted pump -> manage -> exit (shows breakeven arming, then TP)
    print("[6] price path:")
    for mult, label in [(1.08, "+8%"), (1.30, "+30%"), (1.20, "+20%"), (1.62, "+62%")]:
        prices.price_sol = c.price_sol * mult
        price = await exe.get_price_sol(c.mint)
        do_exit, reason = pos.should_exit(price, ep)
        print(f"    {label:>5}: pnl={pos.pnl_pct(price) * 100:+.1f}%  breakeven_armed={pos.breakeven_armed}  "
              f"-> exit={do_exit} {reason}")
        if do_exit:
            sell = await exe.sell(c.mint, pos.qty)
            pnl = pf.apply_sell(sell, reason=reason)
            roi = (pf.equity({}) / s.risk.initial_sol - 1.0) * 100.0
            print(f"    SOLD for {sell.sol:.4f} SOL | realized PnL {pnl:+.4f} SOL | "
                  f"cash {pf.sol_balance:.4f} | equity {roi:+.2f}%")
            break
    hr()

    # 7) reflection -> lesson (REAL local Ollama)
    if pf.closed:
        print("[7] reflecting on the closed trade (local Ollama)...")
        note = await brain.reflect(pf.closed[-1])
        if note:
            print(f"    LESSON [{note.tag}, conf {note.confidence:.2f}]: {note.lesson}")
        print(f"    memory now holds {len(mem._cache)} lesson(s) for future decisions")

    # 8) AI3 meta-learning: distill an AGGREGATE strategy lesson from recent trades
    from memebot.portfolio.pnl import compute_stats
    recent = pf.closed[-30:] or pf.closed
    if recent:
        print("[8] meta-reflection (AI3) over recent performance...")
        mnote = await brain.meta_reflect(compute_stats(recent), recent)
        if mnote:
            print(f"    META-LESSON [{mnote.tag}]: {mnote.lesson}")

    if hasattr(llm, "aclose"):
        await llm.aclose()
    print("\ndemo complete ✅")


if __name__ == "__main__":
    asyncio.run(main())
