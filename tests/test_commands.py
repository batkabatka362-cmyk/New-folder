"""Telegram command dispatch (pure render_command — no network)."""
from __future__ import annotations

from memebot.alerts.commands import render_command
from memebot.config import Settings
from memebot.portfolio.portfolio import Portfolio, Position
from memebot.risk.limits import RiskManager


def _ctx():
    s = Settings()
    pf = Portfolio(10.0)
    pf.positions["m"] = Position("m", "TST", "scalp", qty=1000, cost_sol=0.001)  # avg 1e-6
    return dict(portfolio=pf, risk=RiskManager(s), settings=s,
                price_map={"m": 1.5e-6}, scorer_kind="rule",
                backend="ollama:gemma3:4b", tracked=42)


def test_pnl_command():
    out = render_command("/pnl", **_ctx())
    assert "equity" in out.lower() and "realized" in out.lower()


def test_positions_command():
    out = render_command("/positions", **_ctx())
    assert "TST" in out and "+50.0%" in out          # 1.5e-6 vs 1e-6 avg = +50%


def test_status_command():
    out = render_command("/status", **_ctx())
    assert "scorer" in out and "ollama:gemma3:4b" in out and "active" in out


def test_stop_and_resume_mutate_risk():
    ctx = _ctx()
    rm = ctx["risk"]
    assert "halt" in render_command("/stop", **ctx).lower() and rm.halted
    assert render_command("/status", **ctx).count("HALTED") == 1
    assert render_command("/resume", **ctx) and not rm.halted


def test_help_and_unknown():
    ctx = _ctx()
    assert render_command("/help", **ctx).startswith("*memebot commands*")
    assert render_command("hello there", **ctx) is None     # not a command
    assert render_command("/pnl@mybot", **ctx) is not None   # @botname tolerated


def test_empty_positions():
    ctx = _ctx()
    ctx["portfolio"].positions.clear()
    assert render_command("/positions", **ctx) == "No open positions."
