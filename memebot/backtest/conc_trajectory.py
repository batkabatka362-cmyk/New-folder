"""WL3: hold-time concentration-trajectory analysis — does a top-5 concentration RISE while held
actually separate rugs/losers from winners, and what `conc_rise_cut_pct` best calibrates the cut?

The honest doctrine names a holder-concentration RISE WHILE HELD (dev/insiders consolidating =
distribution prep) as the #1 free rug tell — the one signal that separates the catastrophic whale-dump
losses no static score caught. The live `_reeval_loop` already ACTS on it, but until WL3 the per-cycle
readings were never PERSISTED, so the threshold could not be calibrated from realized outcomes. This
reads the `hold_concentration` time-series, takes each held mint's PEAK rise above its entry baseline,
joins it to the realized `trade_outcomes` book, and asks the honest question: do losers/rugs rise more
than winners? Then it sweeps candidate thresholds (losers-caught vs winners-wrongly-cut, with a
precision column) so `conc_rise_cut_pct` can be calibrated from data — never by feel.

Offline, read-only, ADVISORY (it NEVER auto-tunes live risk). Honest by construction: it reports
"not enough hold-time data yet" until the running bot accrues readings (the table ships empty, and the
signal only exists for mints whose ENTRY concentration was known — needs a real Helius RPC).

  python -m memebot.backtest.conc_trajectory
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict

from ..config import get_settings

_GLITCH_RET_MULT = 20.0          # same pricing-glitch ceiling as pnl_reconstruct / brain_audit
_CANDIDATE_THRESHOLDS = (3.0, 5.0, 8.0, 10.0, 15.0, 20.0)


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def peak_rise_by_mint(rows: list[tuple]) -> dict[str, float]:
    """rows = [(mint, delta_pp)] from hold_concentration -> per-mint PEAK (max) rise above entry.
    delta_pp None (entry concentration unknown) is ignored — only mints with a known entry baseline
    carry a measurable rise (the signal is the TREND, which needs a reference)."""
    peak: dict[str, float] = {}
    for mint, delta in rows:
        if delta is None:
            continue
        d = float(delta)
        if mint not in peak or d > peak[mint]:
            peak[mint] = d
    return peak


def win_by_mint(rows: list[tuple]) -> dict[str, bool]:
    """rows = [(mint, pnl_sol, pnl_pct)] from trade_outcomes -> mint -> won? Sums per-mint slice PnL
    (a mint can close over several legs) and excludes pricing-glitch round-trips (the same >20x ceiling
    the honest book drops), so the win/lose split matches the CLEAN realized book."""
    agg: dict[str, float] = defaultdict(float)
    glitch: set[str] = set()
    for mint, pnl_sol, pnl_pct in rows:
        if pnl_pct is not None and (1.0 + float(pnl_pct)) > _GLITCH_RET_MULT:
            glitch.add(mint)
            continue
        agg[mint] += float(pnl_sol or 0.0)
    return {m: v > 0 for m, v in agg.items() if m not in glitch}


def analyze(peak: dict[str, float], wins: dict[str, bool],
            thresholds: tuple[float, ...] = _CANDIDATE_THRESHOLDS) -> dict:
    """Pure: join peak-rise to realized win/lose, then sweep candidate cut thresholds. For each
    threshold: how many LOSERS it would have caught vs WINNERS it would have wrongly cut, with a
    precision = losers / (losers + winners) it cuts. Returns {} fields zeroed on no paired data."""
    paired = [(peak[m], wins[m]) for m in peak if m in wins]
    win_rises = [r for r, w in paired if w]
    lose_rises = [r for r, w in paired if not w]
    sweep = []
    for t in thresholds:
        caught = sum(1 for r in lose_rises if r >= t)
        cut = sum(1 for r in win_rises if r >= t)
        denom = caught + cut
        sweep.append({"threshold": t, "losers_caught": caught, "winners_cut": cut,
                      "precision": (caught / denom) if denom else 0.0})
    return {"n": len(paired), "n_win": len(win_rises), "n_lose": len(lose_rises),
            "winner_med": _median(win_rises), "loser_med": _median(lose_rises), "sweep": sweep}


def load(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        try:
            hc = conn.execute("SELECT mint, delta_pp FROM hold_concentration").fetchall()
        except sqlite3.OperationalError:
            hc = []
        try:
            to = conn.execute("SELECT mint, pnl_sol, pnl_pct FROM trade_outcomes").fetchall()
        except sqlite3.OperationalError:
            to = []
    finally:
        conn.close()
    return analyze(peak_rise_by_mint(hc), win_by_mint(to))


def main() -> None:
    s = get_settings()
    a = load(s.db_path)
    print("=== HOLD-TIME CONCENTRATION TRAJECTORY (WL3) — does a rise-while-held separate rugs? ===")
    if not a["n"]:
        print("  not enough hold-time concentration data yet. The table ships empty and only fills as the")
        print("  running bot re-checks OPEN positions (needs a real Helius RPC + a KNOWN entry concentration).")
        print("  Re-run as readings accrue; this stays read-only/advisory.")
        return
    wm = "n/a" if a["winner_med"] is None else f"{a['winner_med']:+.1f}pp"
    lm = "n/a" if a["loser_med"] is None else f"{a['loser_med']:+.1f}pp"
    print(f"  {a['n']} held mints with a known entry baseline + a realized outcome "
          f"({a['n_win']} winners / {a['n_lose']} losers)")
    print(f"  median PEAK concentration rise while held:  winners {wm}   losers {lm}"
          + ("   <- losers rise MORE (the signal separates)" if (a["loser_med"] is not None
             and a["winner_med"] is not None and a["loser_med"] > a["winner_med"]) else ""))
    print(f"\n  {'cut @ rise':>11}  {'losers_caught':>13}  {'winners_cut':>12}  {'precision':>10}")
    for r in a["sweep"]:
        print(f"  {r['threshold']:>9.0f}pp  {r['losers_caught']:>13}  {r['winners_cut']:>12}  {r['precision']*100:>9.0f}%")
    print("\n  -> a threshold that catches many losers at few winners-cut (high precision) is the calibrated")
    print("     conc_rise_cut_pct. ADVISORY — confirm forward + apply by hand; the live cut is never auto-tuned.")


if __name__ == "__main__":
    main()
