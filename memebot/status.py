"""CLI status dashboard — read-only snapshot of the bot's SQLite log.

    python -m memebot.status [db_path]

Shows latest equity / realized PnL / ROI, the trade funnel (candidates ->
passed -> buys/sells with exit-reason breakdown), recent verdicts, and the
lessons the brain has learned. Safe to run while the bot is live (WAL reads).
"""
from __future__ import annotations

import sqlite3
import sys

from .config import get_settings


def _scalar(conn: sqlite3.Connection, sql: str, default=0):
    try:
        r = conn.execute(sql).fetchone()
        return r[0] if r and r[0] is not None else default
    except sqlite3.OperationalError:
        return default


def _rows(conn: sqlite3.Connection, sql: str):
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.OperationalError:
        return []


def load_summary(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        s: dict = {}
        latest = _rows(conn, "SELECT equity_sol,sol_balance,realized_pnl,open_positions "
                             "FROM equity ORDER BY ts DESC LIMIT 1")
        first = _rows(conn, "SELECT equity_sol FROM equity ORDER BY ts ASC LIMIT 1")
        if latest:
            eq, bal, rpnl, opos = latest[0]
            s["equity"] = {"equity_sol": eq, "sol_balance": bal, "realized_pnl": rpnl,
                           "open_positions": opos, "start_sol": (first[0][0] if first else eq)}
        else:
            s["equity"] = None
        # P8: the HONEST realized book (CLEAN, glitch-excluded) from the durable trades table — the
        # win-rate to trust (the `equity` snapshot above resets on restart + can include a pricing glitch).
        try:
            from .backtest.pnl_reconstruct import reconstruct, summarize
            s["honest"] = summarize(reconstruct(_rows(conn, "SELECT mint,side,sol,tokens FROM trades")))
        except Exception:  # noqa: BLE001
            s["honest"] = None
        s["candidates"] = _scalar(conn, "SELECT COUNT(*) FROM candidates")
        s["passed"] = _scalar(conn, "SELECT COUNT(*) FROM candidates WHERE rule_passed=1")
        s["buys"] = _scalar(conn, "SELECT COUNT(*) FROM trades WHERE side='buy'")
        s["sells"] = _scalar(conn, "SELECT COUNT(*) FROM trades WHERE side='sell'")
        s["sell_reasons"] = {(r[0] or "unknown"): r[1] for r in
                             _rows(conn, "SELECT reason,COUNT(*) FROM trades WHERE side='sell' GROUP BY reason")}
        # N1: forward-tracking funnel — distinct INDEXED mints, how many have a multi-point forward
        # path, and how many span the 5m label horizon (the labelable count that gates the GBM).
        s["tracking"] = _rows(conn,
            "SELECT COUNT(*), SUM(CASE WHEN n>=2 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN span>=300 THEN 1 ELSE 0 END) FROM "
            "(SELECT mint, COUNT(*) n, MAX(ts)-MIN(ts) span FROM observations "
            "WHERE price_usd>0 AND liquidity_usd>0 GROUP BY mint)")
        s["recent_trades"] = _rows(conn, "SELECT side,symbol,mode,sol,reason FROM trades "
                                         "ORDER BY id DESC LIMIT 8")
        s["recent_verdicts"] = _rows(conn, "SELECT symbol,action,mode,conviction,source FROM verdicts "
                                           "ORDER BY id DESC LIMIT 8")
        s["verdict_sources"] = _rows(conn, "SELECT source, COUNT(*) FROM verdicts GROUP BY source")  # brain-usage rate
        s["recent_lessons"] = _rows(conn, "SELECT tag,lesson FROM lessons ORDER BY id DESC LIMIT 6")
        return s
    finally:
        conn.close()


def render_summary(s: dict) -> str:
    out: list[str] = ["== memebot status =="]
    eq = s.get("equity")
    if eq:
        roi = (eq["equity_sol"] / eq["start_sol"] - 1.0) * 100.0 if eq["start_sol"] else 0.0
        out.append(f"equity: {eq['equity_sol']:.4f} SOL ({roi:+.1f}%)  bal: {eq['sol_balance']:.4f}  "
                   f"realized: {eq['realized_pnl']:+.4f}  open: {eq['open_positions']}")
    else:
        out.append("equity: (no snapshots yet)")

    hb = (s.get("honest") or {}).get("clean")
    if hb and hb["n"] > 0:
        pf = "inf" if hb["profit_factor"] == float("inf") else f"{hb['profit_factor']:.2f}"
        out.append(f"HONEST book (CLEAN, glitch-excluded): net {hb['net']:+.3f} SOL  "
                   f"win {hb['win_rate'] * 100:.0f}% ({hb['wins']}W/{hb['losses']}L)  pf {pf}  "
                   f"over {hb['n']} closed mints  <- the number to trust")
        ng = len((s.get("honest") or {}).get("glitches", []))
        if ng:
            out.append(f"  ({ng} pricing-glitch round-trip(s) excluded; the equity line above includes them)")

    # the candidates table holds only gate-passed+scored rows (rejects aren't logged),
    # so a pass-rate would be a meaningless 100% — report the scored count plainly.
    out.append(f"funnel: {s.get('candidates', 0)} scored (gate-passed) -> "
               f"{s.get('buys', 0)} buys / {s.get('sells', 0)} sells")
    if s.get("sell_reasons"):
        reasons = " ".join(f"{k}:{v}" for k, v in sorted(s["sell_reasons"].items()))
        out.append(f"exits: {reasons}")

    # AI-brain usage (the vision check): of every DECIDED verdict, how many did the LLM brain drive vs
    # the deterministic rule fallback? A tiny share = the bot is trading as a rule-bot (see brain_audit).
    vs = s.get("verdict_sources") or []
    total = sum(n for _, n in vs)
    if total:
        brain = sum(n for src, n in vs if src and str(src).lower() != "rule")
        frac = brain / total
        out.append(f"AI brain usage: {brain}/{total} decisions = {frac * 100:.1f}% LLM-driven"
                   + ("  <- mostly RULE-driven; LLM up + accruing (see `brain_audit`)" if frac < 0.05 else ""))

    tr = s.get("tracking")
    if tr and tr[0] and tr[0][0]:
        mints, multi, labelable = tr[0][0] or 0, tr[0][1] or 0, tr[0][2] or 0
        out.append(f"tracking (N1): {mints} indexed mints  {multi} multi-point  "
                   f"{labelable} labelable@5m  <- the count that gates the GBM/separation")

    if s.get("recent_verdicts"):
        out.append("recent verdicts:")
        for sym, action, mode, conv, src in s["recent_verdicts"]:
            out.append(f"  {sym or '?':8} {action:5} [{mode}] conv={conv:.2f} ({src})")
    if s.get("recent_lessons"):
        out.append("lessons:")
        for tag, lesson in s["recent_lessons"]:
            out.append(f"  [{tag}] {lesson[:90]}")
    return "\n".join(out)


def main() -> None:
    db = sys.argv[1] if len(sys.argv) > 1 else get_settings().db_path
    print(render_summary(load_summary(db)))


if __name__ == "__main__":
    main()
