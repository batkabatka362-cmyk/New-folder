"""CLI status dashboard: pure render + load over a temp DB."""
from __future__ import annotations

import asyncio
import os
import tempfile

from memebot.status import load_summary, render_summary
from memebot.storage.db import Storage


def test_render_empty():
    out = render_summary({"equity": None, "candidates": 0, "passed": 0, "buys": 0, "sells": 0})
    assert "no snapshots yet" in out and "memebot status" in out


def test_render_populated():
    s = {
        "equity": {"equity_sol": 11.0, "sol_balance": 9.0, "realized_pnl": 1.0,
                   "open_positions": 2, "start_sol": 10.0},
        "candidates": 100, "passed": 40, "buys": 5, "sells": 3,
        "sell_reasons": {"tp": 2, "sl": 1},
        "recent_verdicts": [("TST", "buy", "scalp", 0.7, "ollama:gemma3:4b")],
        "recent_lessons": [("scalp", "thin liquidity scalps rug fast")],
    }
    out = render_summary(s)
    assert "+10.0%" in out               # 11/10 - 1
    assert "100 scored" in out and "tp:2" in out
    assert "TST" in out and "[scalp]" in out


def test_load_summary_roundtrip():
    d = tempfile.mkdtemp()
    db = os.path.join(d, "t.db")
    st = Storage(db); st.connect()

    async def seed():
        await st.log_equity(equity_sol=11.0, sol_balance=9.0, realized_pnl=1.0, open_positions=1)
        await st.log_trade(mint="m", symbol="X", side="buy", mode="scalp", sol=0.5,
                           tokens=1000, price_sol=5e-4, fee_sol=0.01, slippage_pct=0.03, reason="haiku")
        await st.log_lesson(lesson="be patient", tag="global", confidence=0.6)

    asyncio.run(seed())
    st.close()

    s = load_summary(db)
    assert s["equity"]["equity_sol"] == 11.0 and s["equity"]["open_positions"] == 1
    assert s["buys"] == 1 and s["sells"] == 0
    assert s["recent_lessons"] and s["recent_lessons"][0][0] == "global"
    # missing DB file -> all graceful defaults, no crash
    empty = load_summary(os.path.join(d, "nope.db"))
    assert empty["equity"] is None and empty["candidates"] == 0
