"""WL18 — the SWING mode: mean-reversion on established, liquid Solana memecoins.

A DIFFERENT game from the pump.fun fresh-launch sniper (which is structurally dead for us). The swing_lab
feasibility (WL17) found the project's first net-positive edge: buying liquid survivors (BONK/WIF-tier)
when they dip well below their SMA and selling the reversion beats buy-and-hold 15/15 and survives every
stress test (survivorship, fees, glitches, param sweep). This package is the PAPER, forward-testing
implementation of that edge — fully separate from the sniping bot (it never touches main.py), reusing the
config + storage conventions. PAPER-ONLY, like everything else.
"""
