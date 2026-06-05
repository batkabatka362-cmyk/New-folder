# memebot — Solana pump.fun memecoin **PAPER**-trading AI

An autonomous, self-measuring, self-improving paper-trader for the pump.fun ecosystem (Python /
asyncio). It discovers fresh/graduated tokens off the **free** PumpPortal WS, enriches them
(DexScreener price/volume/liquidity + Helius mint/freeze authority, holder concentration, creator
holding, funder clustering), runs a deterministic safety gate + scorer, consults an advisory LLM
"brain" (local Ollama or cloud Claude, with a rule fallback) only when uncertain, and executes
against a **paper** backend that models real curve/Jupiter pricing, stacked fees, and slippage.
Everything is logged to SQLite, and every closed trade is reflected into persistent, self-curating
lessons. Full design in [memebot-plan.md](memebot-plan.md); the running build journal in
[ROADMAP.md](ROADMAP.md); the evidence base in [RESEARCH.md](RESEARCH.md).

> ⚠️ **Эрсдэл:** memecoin арилжаа маш өндөр эрсдэлтэй. Энэ нь санхүүгийн зөвлөгөө биш. Зөвхөн **paper
> (симуляци)** горимоор ажиллана. `MODE=live` зориуд хэрэгжүүлээгүй; live/metered руу зөвхөн гараар,
> тогтвортой edge баталгаажсаны дараа шилжинэ.

## The honest verdict (read this first)

This is a **loss-minimisation + honest-measurement** program, **not a profit program**. A 2026
multi-source, adversarially-verified literature review ([RESEARCH.md](RESEARCH.md)) concludes that
systematic **free-data** pump.fun trading is **structurally net-negative** (~76% rug, ~92% dump,
~0.5-2% graduate); even the best model only *reduces* loss; and our own honest results (net-negative,
no static feature separating winners from rugs at AUC ~0.5-0.62) are **correct and realistic, not
bugs**. The realistic ceiling is **loss-minimisation**; real profit would need speed/MEV infra
(co-located RPC, Jito bundles) we do not have. The project's value is rug-avoidance, rigorous honest
measurement, and a system that keeps improving its own knowledge — **not** a promised edge.

## Pipeline

```text
PumpPortal WS (free discovery)   ┌─ safety gate (hard vetoes) ─┐
+ DexScreener + Helius      ──►   │  scam-likelihood (~12 signals)│ ──► setup_type ──► uncertain? ──► 🧠 brain
(discover + enrich)              │  + scorer (rule, GBM dormant) │     (gold/hold/momentum/risky)   (Ollama/Claude)
                                 └───────────────────────────────┘                                      │ Verdict
                       deterministic RISK layer (caps · kill-switches · vetoes) ◄───────────────────────┘
                                 │
                       PaperBackend (curve/Jupiter price + slippage + stacked ~9.5% round-trip fees)
                                 │
                       SQLite → reflect / meta-reflect / miss-learn / calibrate → self-curating lessons
```

Loops (asyncio): **feed → ingest → eval → manage → reeval → report → reflect → meta-reflect →
miss-learn → track (forward-label) → funder (cluster) → calibrate → readiness.**

## Run

```powershell
python -m memebot.supervisor       # AUTONOMY: keeps the bot alive across crashes (auto-restart + backoff). Stop: Ctrl-C or a `memebot.stop` file. Add to Task Scheduler for run-on-boot.
python -m memebot                  # run the bot directly (one process)
python run_tests.py                # 212-test suite (dependency-free; pytest also works)
python -m memebot.status           # equity / honest book / tracking funnel / verdicts / lessons
python -m memebot.readiness        # the single auditable GO/NO-GO: CLEAN-book gate + stability + fill-divergence
```

Local AI works out of the box with Ollama (`ollama pull gemma3:4b qwen3:8b`) — no key. Optional in
`.env`: `HELIUS_RPC_URL` (the #1 free rug tell — holder concentration; dark on the public RPC),
`ANTHROPIC_API_KEY` (cloud brain), `TELEGRAM_*` (alerts + `/pnl /positions /status /stop /resume`).
`TRADE_STREAM_ENABLED` (⚠️ G3 — metered, needs a PumpPortal api-key wallet funded ≥0.02 SOL) is OFF by
default; it's the one lever that could surface the missing bundle/smart-money tape signals.

## Honest measurement (the project's spine)

```powershell
python -m memebot.backtest.pnl_reconstruct   # the CLEAN realized book (glitch-excluded) — the number to trust
python -m memebot.backtest.separation        # per-feature winner-vs-rug AUC (does anything separate? ~0.5-0.62)
python -m memebot.backtest.gbm_stability     # AUC distribution across forward folds (real edge vs one-split noise)
python -m memebot.backtest.rug_model         # rug-vs-winner separability + the rug-VETO dodge/winner-loss trade-off
python -m memebot.backtest.exitlab           # replay logged paths through the REAL exit ladder (the under-studied lever)
python -m memebot.backtest.fill_divergence   # paper-vs-live latency cost — the G1 prerequisite
python -m memebot.backtest.coverage          # which FEATURE_NAMES are DARK (constant) and must not be read as signal
```

## Layout (`memebot/`)

| Package | Role |
|---|---|
| `feed/` | PumpPortal WS (free discovery + metered trade stream), DexScreener, Helius RPC (authority/concentration/creator-holding/funder), watchlist |
| `data/` | per-mint state + tape signals, creator/funder/metadata histories, smart-money (copy-trade) ledger |
| `filter/` | safety + scam-likelihood gate, `FEATURE_NAMES` (load-bearing), GBM scorer (dormant) |
| `signals/` | `Scorer` (rule-or-GBM), `classify_setup` (grade) + `setup_type` (situation/playbook) |
| `agent/` | brain (decide/reflect/meta-reflect/miss-reflect), frozen constitution, Ollama+Claude backends, self-curating memory |
| `risk/` | deterministic caps, daily-loss + rolling-PnL + giveback + loss-streak kill-switches, cohort cap, Kelly skeleton |
| `execution/` | `paper` backend (priced fills + stacked fees + slippage); live is gated, unimplemented |
| `portfolio/` | positions, exits (partial/trail/breakeven/derisk), honest PnL |
| `storage/` | SQLite (WAL) log + Parquet archive |
| `alerts/` | Telegram send + command listener |
| `backtest/` | the measurement + study tools listed above |
| `supervisor.py` · `readiness.py` | autonomy (keep-alive across crashes) · the go-live GO/NO-GO report |

## Status

The free-data architecture is **complete**: rug-avoidance signals (concentration + RISE-while-held,
creator holding/launch-count/funder-cluster/reputation, name-duplication, LP-burn-on-graduation,
tape signals when G3 is funded), survival-first risk (size/exposure/cohort caps + multiple
kill-switches), a setup-type orchestration layer with per-type playbooks, a self-curating
reflection memory, an autonomy supervisor, and a full honest-measurement suite. The dormant GBM
flips on automatically once a stable, gate-clearing edge appears — which, on free data, the evidence
says it won't. **Current honest book: net-negative (loss-minimisation, as expected).** The remaining
real lever is funding **G3** (metered tape → bundle/smart-money signals); profit itself needs
infrastructure we don't have. See [ROADMAP.md](ROADMAP.md) for the full, dated build journal.
