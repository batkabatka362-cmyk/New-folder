"""Swing forward-test status — read the paper book and compare it to the WL17 backtest expectation.

  python -m memebot.swing.status

Reads swing_state.json (no network), prints the realized book (equity, open positions, closed trades,
win-rate, profit factor, realized PnL) and the backtest yardstick to judge it against. Run it as the
forward test accrues to see whether the live mean-reversion edge tracks the backtest (net-positive,
~60% win, beats hold) or diverges.
"""
from __future__ import annotations

import json
import os

_STATE = "swing_state.json"


def _fmt_age(secs: float) -> str:
    if secs <= 0:
        return "?"
    h = secs / 3600.0
    return f"{h:.0f}h" if h < 48 else f"{h/24:.1f}d"


def main() -> None:
    if not os.path.exists(_STATE):
        print("no swing_state.json yet — start the forward test: python -m memebot.swing")
        return
    try:
        st = json.load(open(_STATE))
    except (ValueError, OSError) as e:
        print(f"could not read {_STATE}: {type(e).__name__}")
        return
    cash = float(st.get("cash", 0.0))
    positions = st.get("positions", [])
    closed = st.get("closed", [])

    print("=== SWING forward-test (paper) ===")
    n = len(closed)
    if n:
        wins = [c for c in closed if c.get("pnl_sol", 0) > 0]
        realized = sum(c.get("pnl_sol", 0.0) for c in closed)
        gw = sum(c["pnl_sol"] for c in wins)
        gl = -sum(c["pnl_sol"] for c in closed if c.get("pnl_sol", 0) <= 0)
        pf = (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0)
        avg = realized / n
        print(f"  closed trades : {n}  | win-rate {len(wins)/n*100:.0f}%  | profit factor {pf:.2f}")
        print(f"  realized PnL  : {realized:+.3f} SOL  (avg {avg:+.3f}/trade)")
        # quick distribution of exit reasons
        reasons = {}
        for c in closed:
            reasons[c.get("reason", "?")] = reasons.get(c.get("reason", "?"), 0) + 1
        print(f"  exits         : " + ", ".join(f"{k}:{v}" for k, v in reasons.items()))
    else:
        print("  closed trades : 0  (no reversion completed yet — entries fire only on an ~18% dip)")

    print(f"  open positions: {len(positions)}")
    for p in positions:
        print(f"    {p.get('symbol','?'):10} entry {p.get('entry_price',0):.6g}  "
              f"dip@entry {p.get('dip_at_entry',0)*100:.0f}%  in {p.get('sol_in',0):.2f} SOL  "
              f"held {p.get('bars_held',0)} bars")
    print(f"  cash          : {cash:.3f} SOL")
    print("\n  Backtest yardstick (WL17): net-POSITIVE, ~60% win, profit factor > 1, beats buy-and-hold.")
    print("  Diverging hard from that as trades accrue = the live edge isn't holding (slippage/regime).")


if __name__ == "__main__":
    main()
