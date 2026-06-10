"""WL18 — the SWING mode: mean-reversion on established, liquid Solana memecoins.

A DIFFERENT game from the pump.fun fresh-launch sniper (which is structurally dead for us). The swing_lab
feasibility (WL17) found the project's first net-positive edge: buying liquid survivors (BONK/WIF-tier)
when they dip well below their SMA and selling the reversion beats buy-and-hold 15/15 and survives every
stress test (survivorship, fees, glitches, param sweep). This package is the PAPER, forward-testing
implementation of that edge — fully separate from the sniping bot (it never touches main.py), reusing the
config + storage conventions. PAPER-ONLY, like everything else.

KNOWN MODELLING GAP (honest, by design): the backtest (backtest/swing_lab.py) is PER-TOKEN and stateless —
it measures the edge on each token independently. The LIVE engine runs a shared PORTFOLIO: max_positions,
shared cash, the aggregate-exposure cap, and the rolling-loss kill-switch all bind ACROSS tokens. So the
walk-forward gmean validates the per-token EDGE, but live portfolio behaviour (which 5 of the N dipping
tokens you actually hold, the halt firing) can diverge from the sum-of-per-token backtest. The portfolio
guards are deliberately loose so they rarely bind, but a true basket-level backtest is the next rigor step
if the forward test diverges. Compare swing.status realized to the backtest with this caveat in mind.
"""
