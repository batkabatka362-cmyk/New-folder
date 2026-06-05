"""Go-live READINESS report — the explicit, auditable GO/NO-GO for real money.

Aggregates every honest instrument into ONE verdict so a single lucky number can't green-light real
money. The GATE is the HONEST (CLEAN, glitch-excluded) realized book: >= go_live_min_trades closed
round-trips, NET-POSITIVE, AND profit factor >= go_live_min_profit_factor. Supporting context
(advisory, never gates on its own): the GBM signal-stability verdict, the paper-vs-live fill-divergence
lower bound, and the labelable-mint count. NO-GO is the expected, honest answer on free data
(loss-minimisation is the ceiling) — this report makes WHY explicit instead of trusting a vibe.

This is read-only and advisory; it NEVER flips live mode (that stays a deliberate, gated user action).

  python -m memebot.readiness [db_path]
"""
from __future__ import annotations

import sqlite3
import sys

from .config import get_settings


def evaluate_gate(book: dict, *, min_trades: int, min_pf: float, require_net_positive: bool = True) -> dict:
    """Pure go/no-go on the CLEAN realized book. ALL checks must pass. Returns
    {ready: bool, checks: [(name, passed, detail), ...]}."""
    n = int(book.get("n", 0))
    net = float(book.get("net", 0.0))
    pf = book.get("profit_factor", 0.0)
    pf_str = "inf" if pf == float("inf") else f"{pf:.2f}"
    checks = [
        (f"closed trades >= {min_trades}", n >= min_trades, f"{n} closed"),
        ("CLEAN book net-positive", (net > 0.0) if require_net_positive else True, f"{net:+.3f} SOL"),
        (f"profit factor >= {min_pf}", (pf >= min_pf), pf_str),
    ]
    return {"ready": all(p for _, p, _ in checks), "checks": checks}


def _clean_book(db: str) -> dict:
    try:
        from .backtest.pnl_reconstruct import _load_trades, reconstruct, summarize
        return (summarize(reconstruct(_load_trades(db))) or {}).get("clean") or {}
    except Exception:  # noqa: BLE001
        return {}


def _history(db: str, limit: int = 6) -> list:
    """Recent readiness_log snapshots (oldest-first) — the durable trajectory from the monitor loop."""
    try:
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute("SELECT n,net,profit_factor,labelable FROM readiness_log "
                                "ORDER BY ts DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
        finally:
            conn.close()
        return list(reversed(rows))
    except Exception:  # noqa: BLE001
        return []


def _labelable(db: str, horizon_s: float = 300.0) -> int:
    """Distinct INDEXED mints whose forward path spans the label horizon (the GBM's binding input)."""
    try:
        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "SELECT SUM(CASE WHEN span >= ? THEN 1 ELSE 0 END) FROM "
                "(SELECT mint, MAX(ts)-MIN(ts) AS span FROM observations "
                " WHERE price_usd>0 AND liquidity_usd>0 GROUP BY mint)", (horizon_s,)
            ).fetchone()
        finally:
            conn.close()
        return int(row[0] or 0) if row else 0
    except Exception:  # noqa: BLE001
        return 0


def _fill_divergence(db: str, s) -> dict | None:
    try:
        from .backtest.fill_divergence import _load_series, _load_trades, fill_divergence, summarize
        from .utils.money import round_trip_cost_pct
        div = fill_divergence(_load_trades(db), _load_series(db), polls=2)
        if div["matched"] == 0:
            return None
        return summarize(div, round_trip_cost_pct(s.fees))
    except Exception:  # noqa: BLE001
        return None


def _stability(db: str, s) -> dict | None:
    try:
        from .backtest.gbm_stability import fold_cuts, summarize_stability, _fold_auc
        from .backtest.label import _load_all_obs, fixed_horizon_labels
        from .filter.features import row_from_feats
        rows = _load_all_obs(db)
        labeled = fixed_horizon_labels(rows, 300.0, 0.5, 900.0, death_drop=0.9, indexed_only=True)
        cuts = fold_cuts(len(labeled), 5)
        if not cuts:
            return None
        xs = [row_from_feats(f) for _m, f, _l, _fwd in labeled]
        ys = [int(lbl) for _m, _f, lbl, _fwd in labeled]
        aucs = []
        for cut, end in cuts:
            r = _fold_auc(xs[:cut], ys[:cut], xs[cut:end], ys[cut:end], s.gbm_entry_threshold, s.gbm_balance_classes)
            if r is not None:
                aucs.append(r[0])
        return summarize_stability(aucs)
    except Exception:  # noqa: BLE001
        return None


def render(db: str, s) -> str:
    book = _clean_book(db)
    gate = evaluate_gate(book, min_trades=s.go_live_min_trades, min_pf=s.go_live_min_profit_factor)
    pf = book.get("profit_factor", 0.0)
    pf_str = "inf" if pf == float("inf") else f"{pf:.2f}"
    out = ["== memebot GO-LIVE readiness =="]
    out.append(f"HONEST book (CLEAN, glitch-excluded): net {book.get('net', 0.0):+.3f} SOL  "
               f"win {book.get('win_rate', 0.0) * 100:.0f}%  pf {pf_str}  over {book.get('n', 0)} closed mints")
    out.append("")
    out.append("GATE — ALL must pass before real money is even considered:")
    for name, passed, detail in gate["checks"]:
        out.append(f"  [{'PASS' if passed else 'FAIL'}] {name}   ({detail})")

    out.append("")
    out.append("supporting context (advisory — does NOT gate):")
    out.append(f"  labelable mints @5m: {_labelable(db)}  (GBM needs a few hundred with both classes)")
    st = _stability(db, s)
    if st and st["folds"]:
        out.append(f"  GBM signal stability: {st['verdict']}  (mean AUC {st['mean']:.3f} +/- {st['std']:.3f} "
                   f"over {st['folds']} folds)")
    fd = _fill_divergence(db, s)
    if fd:
        out.append(f"  paper-vs-live fill divergence: +{fd['extra_round_trip_cost'] * 100:.2f}% extra round-trip "
                   f"(~{fd['eats_cushion_frac'] * 100:.0f}% of the cushion; weak LOWER bound)")

    hist = _history(db)
    if len(hist) >= 2:
        out.append("")
        out.append("trajectory (oldest -> newest readiness snapshots):")
        for n, net, pf, lab in hist:
            pfh = "inf" if pf >= 1e9 else f"{pf:.2f}"
            out.append(f"    n={n:>4}  net {net:+.3f}  pf {pfh:>5}  labelable {lab}")

    out.append("")
    if gate["ready"]:
        out.append("VERDICT: GO — the honest book clears the gate. (Live execution is STILL a deliberate, "
                   "separately-gated user action; verify stability + fill-divergence first.)")
    else:
        fails = [name for name, passed, _ in gate["checks"] if not passed]
        out.append("VERDICT: NO-GO — real money stays OFF. Failing: " + "; ".join(fails))
        out.append("This is the expected honest result on free data (the ceiling is loss-minimisation). "
                   "Keep accruing; re-run as the book + data grow.")
    return "\n".join(out)


def main() -> None:
    s = get_settings()
    db = sys.argv[1] if len(sys.argv) > 1 else s.db_path
    print(render(db, s))


if __name__ == "__main__":
    main()
