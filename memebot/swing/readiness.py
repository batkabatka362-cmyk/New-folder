"""WL27 — the swing FORWARD-PROOF gate: the single auditable GO/NO-GO on whether the mean-reversion edge
is holding on the LIVE (paper) book, not just in backtest.

The backtest validated the edge (OOS gmean ~1.5, win ~69%); this checks the REALIZED forward book against an
explicit bar before anyone considers real money. Mirrors the sniper's `readiness` discipline: read-only,
advisory, and it NEVER flips live mode — going live remains a deliberate human decision (the PAPER-ONLY
hard constraint). A GO here is necessary, not sufficient.

  python -m memebot.swing.readiness
"""
from __future__ import annotations

import json
import os

from ..config import get_settings

_STATE = "swing_state.json"
# the backtest yardstick the forward book is judged against (WL17/WL19, net of fees+slippage)
_BACKTEST_WINRATE = 0.69


def assess(closed: list, *, min_trades: int, min_pf: float, min_winrate: float) -> dict:
    """Pure: realized-book stats + a GO/NO-GO against the bar. closed = list of {pnl_sol, pnl_pct}."""
    n = len(closed)
    wins = [c for c in closed if float(c.get("pnl_sol", 0)) > 0]
    gross_win = sum(float(c["pnl_sol"]) for c in wins)
    gross_loss = -sum(float(c["pnl_sol"]) for c in closed if float(c.get("pnl_sol", 0)) <= 0)
    net = sum(float(c.get("pnl_sol", 0)) for c in closed)
    win_rate = (len(wins) / n) if n else 0.0
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    reasons = []
    if n < min_trades:
        reasons.append(f"only {n}/{min_trades} closed trades (insufficient sample)")
    if net <= 0:
        reasons.append(f"net {net:+.3f} SOL is not positive")
    if pf < min_pf:
        reasons.append(f"profit factor {pf:.2f} < {min_pf:.2f}")
    if win_rate < min_winrate:
        reasons.append(f"win-rate {win_rate*100:.0f}% < {min_winrate*100:.0f}%")
    return {"go": not reasons and n >= min_trades, "n": n, "win_rate": win_rate, "pf": pf, "net": net,
            "reasons": reasons}


def main() -> None:
    s = get_settings()
    if not os.path.exists(_STATE):
        print("no swing_state.json yet — start the forward test: python -m memebot.swing")
        return
    try:
        closed = json.load(open(_STATE)).get("closed", [])
    except (ValueError, OSError) as e:
        print(f"could not read {_STATE}: {type(e).__name__}")
        return
    r = assess(closed, min_trades=s.swing_ready_min_trades, min_pf=s.swing_ready_min_pf,
               min_winrate=s.swing_ready_min_winrate)
    print("=== SWING FORWARD-PROOF GATE (advisory; never flips live) ===")
    print(f"  closed {r['n']} | win-rate {r['win_rate']*100:.0f}% (backtest ~{_BACKTEST_WINRATE*100:.0f}%) "
          f"| pf {r['pf']:.2f} | net {r['net']:+.3f} SOL")
    if r["go"]:
        print("  VERDICT: GO — the forward book clears the bar. NECESSARY, not sufficient: going live is "
              "still a deliberate human decision (paper-only hard constraint).")
    else:
        print("  VERDICT: NO-GO — " + "; ".join(r["reasons"]))
        if r["n"] >= max(8, s.swing_ready_min_trades // 3) and r["win_rate"] < _BACKTEST_WINRATE - 0.20:
            print(f"  ⚠ realized win-rate {r['win_rate']*100:.0f}% is far below the backtest "
                  f"{_BACKTEST_WINRATE*100:.0f}% — watch for the edge NOT holding live (slippage/regime).")


if __name__ == "__main__":
    main()
