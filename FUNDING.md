# FUNDING.md — what is built free, and what the ONLY remaining barrier (funding) unlocks

The system is left **funding-ready**: every free-buildable piece is done and running; the only thing
money would add is one specific data stream. Read this before spending anything.

## Already built + running on FREE data (no funding needed)

- **Swing mean-reversion mode** (`python -m memebot.swing`) — the project's one validated net-positive
  edge (WL17), forward-testing on PAPER right now. Sources free OHLCV from the Solana Tracker free tier.
- **Autonomous strategy-discovery loop** (WL19) — the agent re-runs walk-forward discovery weekly and
  records OOS-validated strategies. No funding.
- **Self-improving GBM retrain loop** (A) — daily, safe-gated. No funding.
- **The sniper** (`python -m memebot.supervisor`) — paper, with the full free-data stack now actually
  loaded (Helius concentration, the banded funnel, the Solana Tracker rugged-veto, buyer-intel, image).

The **swing edge needs ZERO funding** — it is the most promising direction and runs entirely free.

## What funding unlocks — exactly ONE thing: the G3 metered tape

The sniper's GBM is capped at ~0.57 AUC (< the 0.6 deploy floor) largely because its **coordination
features are DARK** — `sniper_share`, `bundle_share`, `smart_money_share`, `creator_dump_ratio` are all
empty on free data. The ONLY way to light them up is the **metered PumpPortal trade stream (G3)**, which
streams real per-token trades (order flow / fund flows).

- **Cost:** ~0.01 SOL per 10,000 websocket events; the PumpPortal api-key wallet must hold **≥ 0.02 SOL**.
- **It SPENDS SOL** — it is the gated G3 capability. **PAPER-ONLY still holds**: the tape is DATA; trades
  remain paper. No real orders are ever placed.
- **How to activate** (already wired — needs only funding):
  1. `TRADE_STREAM_ENABLED=true` — **already set** in `.env` (kept ready per the user's choice).
  2. `PUMPPORTAL_API_KEY=<key from a funded wallet>` — already set.
  3. **Fund that api-key wallet with ≥ 0.02 SOL.** That's the only missing step. On the next restart the
     watchlist subscribes and the tape flows; the dark coordination features start populating, and the
     daily GBM retrain (A) will deploy a better model **iff** it then clears the safe gate.

## Honest expectation (do not over-fund)

Per the literature + our own data, even WITH the tape the sniper's ceiling is **loss-REDUCTION, not
profit** — the only profitable sniping actors have same-block insider/MEV speed we cannot buy cheaply.
So funding the tape is a worthwhile experiment to see if the coordination features push the GBM past the
gate, but it is **not** a path to a profitable sniper. The **swing edge (free) remains the better bet.**

Bigger money (paid social/X velocity API, premium real-time data, co-located RPC / Jito MEV infra) is a
different tier entirely and only justified if a forward-proven edge demands it. Don't pay for it on spec.
