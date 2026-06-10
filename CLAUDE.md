# CLAUDE.md

## Project overview

memebot is a Solana pump.fun memecoin trading AI written in Python (asyncio). It discovers freshly launched / graduated tokens off the free PumpPortal WebSocket, enriches them with DexScreener (price/volume/liquidity) and Helius RPC (mint/freeze authority + top-5 holder concentration), runs them through a deterministic rule gate + a scorer (live rule scorer; a LightGBM scorer is wired but dormant), consults an advisory "brain" LLM (local Ollama or cloud Claude, with a transparent rule fallback) only when uncertain, and executes against a **paper** backend that models real Jupiter/curve pricing, stacked pump.fun fees, and slippage. Everything is logged to SQLite, alertable over Telegram, and the bot reflects each closed trade into persistent lessons it recalls before future decisions.

**HARD CONSTRAINT — PAPER ONLY.** The bot must never place real-money / live / metered orders without explicit user approval. `MODE=live` is intentionally unimplemented and raises at startup. The gated capabilities — G1 live execution, G3 metered PumpPortal trade stream (which **spends SOL**, `TRADE_STREAM_ENABLED=false` by default and also requires `PUMPPORTAL_API_KEY`) — stay off unless the user explicitly turns them on. Do not flip these defaults, remove the live-mode guard, or wire real fills as part of unrelated work.

## Commands (Windows / PowerShell)

```powershell
python -m memebot                  # run the paper-trading bot (reads .env, MODE=paper)
python -m memebot.supervisor       # AUTONOMY: keep the bot alive across crashes (auto-restart + backoff). Stop: Ctrl-C or a `memebot.stop` file. Add to Task Scheduler for run-on-boot.
python -m memebot.swing            # WL18 SWING mode (SEPARATE paper system): mean-reversion on established liquid memecoins — the WL17 net-positive edge. Reads SWING_* + SOLANATRACKER_* from .env; own loop/book/swing_state.json; never touches the sniper. PAPER-ONLY.
python run_tests.py                # test suite — dependency-free runner; also works under `pytest`
python demo.py                     # end-to-end demo: one token through the full pipeline (in-memory DB)
python -m memebot.status           # status dashboard: equity / funnel / verdicts / lessons
python -m memebot.backtest.simulate  # offline backtest over logged candidates (use -h for flags)
python -m memebot.backtest.replay    # logged-candidate funnel stats
```

The test suite is currently 257 green and must stay green. `run_tests.py` discovers every `test_*` function under `tests/` and needs no third-party deps; keep it that way (see Conventions).

Honest realized PnL (read THIS for the go-live gate, never the in-memory/`trade_outcomes` number — one pricing-glitch fill can be ~100% of reported profit):

```powershell
python -m memebot.backtest.pnl_reconstruct   # per-mint-net realized book from the durable trades table; flags + excludes glitch round-trips (>20x); reports ALL/CLEAN/WINSORIZED
python -m memebot.readiness                   # the single auditable GO/NO-GO: CLEAN-book gate (>=100 trades, net-positive, pf>=1.3) + GBM-stability / fill-divergence / labelable context. Read-only; NEVER flips live mode. (Currently NO-GO — net-negative, the honest loss-min ceiling.)
```

Data → ready-to-use classified dataset (P8, offline, no network — reads logged price history):

```powershell
python -m memebot.backtest.dataset --horizon 300 --out classified.csv  # classify observations -> winner/fade/loser/flat/rug/dead/glitch + a per-class feature-separation table
python -m memebot.backtest.exitlab --min-len 8                          # replay logged price paths through the REAL exit ladder; sweep ExitParams variants, rank by net return (the exit policy is the under-studied lever)
python -m memebot.backtest.separation                                  # RIGOROUS winner-vs-rug per-feature AUC (Mann-Whitney) — the honest "does any signal separate?" verdict (static features confirmed AUC~0.5; watch velocity features as data accrues)
python -m memebot.backtest.gate_attribution                            # spec-S7: the SAFETY GATE's rug-avoidance confusion matrix (rugs dodged vs winners lost) + per-named-reason dodge — the "is the gate catching rugs, and which check earns its keep?" read (per-reason needs `rule_reasons` logged, accruing post-deploy). Baseline: ~51% rug-dodge / ~41% winner-loss; A1/A2/B1 mechanism checks should lift dodge at ~0 winner cost.
python -m memebot.backtest.winner_loss                                 # WL1: which gate CHECK rejects WINNERS — re-derives the live reject's reason offline -> per-reason winners-lost vs rugs-dodged + a TOO-STRICT verdict (the calibration lever; found buy/sell_low was 48%-precision noise -> min_buy_sell_ratio 1.5->1.0). ADVISORY.
python -m memebot.backtest.conc_trajectory                             # WL3: does a top-5 concentration RISE WHILE HELD (the #1 free rug tell) separate rugs from winners? reads the hold_concentration time-series, peak-rise-per-mint vs the realized book, sweeps conc_rise_cut_pct candidates. ADVISORY; "no data yet" until the running bot accrues hold-time readings (needs a real Helius RPC).
python -m memebot.backtest.loss_decomp [--peak 0.30]                   # WL7: of our net-LOSSES, how much is EXIT-addressable (pumped >= +30% then faded -> bank principal earlier) vs a SELECTION problem (never pumped -> only NOT entering avoids it, the image/funnel work)? Sizes the exits-vs-selection split. Current: ~17% addressable / ~83% selection.
python -m memebot.backtest.image_separation                            # WL6 read-side: does the vision IMAGE scam-score separate rugs from winners on the realized book? AUC(loser>winner) + a veto-threshold sweep. "no data yet" until the scorer is enabled (`ollama pull llava`, IMAGE_SCAM_ENABLED=true) + buys accrue a score. The validation loop for the user's visual edge — the ONE untested modality (every on-chain free-data signal separates at AUC~0.5).
python -m memebot.backtest.signal_separation                           # WL9: the single read-out for ALL LOG-only winner/rug signals (image_scam_score, smart/dumper_buyer_count, early_buyers, name_reuse_count, entry_latency_s) — per-signal n + winner/loser medians + ORIENTED AUC over trade_outcomes. Run it as buys accrue: AUC>=0.6 = a real separator that earns a calibrated veto (the win-rate lever); ~0.5 = noise, stays LOG-only.
python -m memebot.backtest.st_separation                              # WL15: does the Solana Tracker / Axiom risk data (st_risk_score/top10/bundlers/dev/snipers/insiders) SEPARATE on our realized book? BACKFILLS current risk data for traded mints (cached in st_backfill, so re-runs don't re-spend) + per-signal AUC. NEGATIVE-screen only: the data is CURRENT not entry-time so look-ahead INFLATES separation — VERDICT (191 mints): ALL ~0.39-0.57 AUC, none >=0.6, i.e. even WITH look-ahead help the Axiom signals do NOT separate winners from rugs on our (already-gate-filtered) cohort. Confirms the structural wall; the WL14 veto stays rugged-only, no calibrated score/conc veto.
python -m memebot.backtest.swing_lab [--type 4h --fee 0.02 --clip 2.0]  # WL17 (path-C feasibility): does SWING-trading ESTABLISHED liquid memecoins (BONK/WIF-tier) beat the dead fresh-launch game? Pulls hourly/4h OHLCV (Solana Tracker /chart, cached in swing_cache.json) for a 15-token basket incl. a faded/high-drawdown survivorship stress-set, backtests buy-hold vs MA-cross/Donchian/mean-reversion net of fees. VERDICT: momentum/breakout UNDERPERFORM hold (no edge); MEAN-REVERSION (buy ~12% below the 24-bar SMA, sell on reversion) is net-POSITIVE + beats hold 15/15 (incl. faders), win ~60%, robust to fees (1.48x @3%) + glitch-clip (1.06x @1.5x/bar) — the project's FIRST real edge, a different game (no speed needed, low rug risk). Caveats: dip-slippage unmodeled, single window, regime-sensitive. Build + forward-test, not yet proven live.
```

GBM activation (only when there is labeled data — needs `lightgbm numpy pyarrow`):

```powershell
python -m memebot.backtest.label --min-age 3600 --out dataset.csv   # forward-outcome (binary) labels
python -m memebot.backtest.train_gbm --data dataset.csv             # -> gbm_model.txt
```

The bot auto-loads `gbm_model.txt` on next start (scorer flips to `gbm`); until then the rule scorer is live.

## Architecture

Pipeline (wired in `main.py` `Bot` as asyncio tasks): **feed → ingest → eval → manage → reeval → report → reflect → meta-reflect → miss-learn → archive.**

- `eval_loop` (every `eval_interval_s`, default 3s) snapshots active tokens, runs market gates then (within a per-cycle budget) Helius safety gates, scores survivors, classifies setup grade, ranks by score, then `_decide_and_open`: decides the top candidates **concurrently** (bounded by `llm_parallel`) and applies buys **sequentially** under the risk gate (the parallel decide is the throughput win; the sequential apply keeps `max_positions`/exposure honest). Gate failers and held positions are still logged as observations (P2/P4 dataset coverage, anti-survivorship-bias).
- `manage_loop` (every `manage_interval_s`, default 2s) checks exits per position (TP/SL/breakeven/trail/fade/partial, plus a no-price blackout force-close).
- `reeval_loop` (every `reeval_interval_s`, default 60s) re-fetches top-5 holder concentration on open positions and cuts / extends / de-risks accordingly.
- `miss_learn_loop` (every `miss_learn_interval_s`, default 600s, P8) replays recently-observed-but-not-bought mints, re-prices them once via DexScreener, and records / reflects on "missed winners" — the regret complement to per-trade reflection. Off the hot path; self-degrades without DexScreener (no-op) or an LLM (record-only).
- `calibrate_loop` (every `calibrate_interval_s`, default 3600s, P8) classifies our own observations into outcomes, derives ADVISORY threshold suggestions from what actually separates winners from rugs, and learns per-creator reputation (realized rug/win rate) — fed to the brain. Suggestions are logged + stored as a recalled lesson, **never auto-applied to live risk config**. Off the hot path (DB read in a worker thread); no-op below `calibrate_min_mints`.

Module map (`memebot/` package):

| Package | Role |
|---|---|
| `feed/` | `pumpportal_ws` (free discovery WS: subscribeNewToken/subscribeMigration), `dexscreener` (REST enrichment + paper-fill price; `PairSnapshot` has buys/sells/buy_sell_ratio h1, price_change m5/h1, liquidity_usd), `helius_rpc` (authority + top-5 holder concentration + `get_recent_buyers`/`get_asset_image`), `jupiter` (A2 honeypot quote), `rugcheck` (B1 risk cross-check), `solanatracker` (WL14: Axiom-style risk score + bundle/sniper/insider/top-10 data, free tier, OFF without a key), `watchlist` (metered trade stream, OFF by default) |
| `data/` | `token_state` (`TokenRegistry`/`TokenState`: rolling price trend, EMA/breakout technicals, unique buyers, buy/sell ratio), `candles`, `indicators`, `creator_history` (serial-rugger launch count), `smart_money` (per-wallet PnL, needs the G3 tape), `buyer_intel` (`BuyerIntel`: WL9 free-data smart-money — wallet reputation from our forward outcomes -> smart/dumper early-buyer confluence; OFF by default) |
| `filter/` | `rules` (hard safety/market gates + `scam_likelihood` + holder quality), `features` (**`FEATURE_NAMES`** — single source of truth, load-bearing column order), `gbm` (LightGBM scorer, dormant) |
| `signals/` | `scoring` (`Scorer`: GBM-or-rule, affine-rescaled rule score 0..1; `select_mode`), `setup` (`classify_setup`: survival-first good/marginal/bad grade) |
| `agent/` | `policy` (frozen CONSTITUTION system prompt), `schema` (`Verdict`/`ReflectionNote` + JSON schemas), `llm` (`AnthropicLLM`, Haiku→Sonnet escalation), `local_llm` (`OllamaLLM` single model + `DualLocalLLM` fast-screen→deep-confirm cascade), `backends` (`make_llm`: auto→local→cloud→rule), `memory` (`AgentMemory`: SQLite lessons), `brain` (`TradingBrain`: `decide`/`reflect`/`meta_reflect`/`reflect_miss`), `escalation`, `vision` (`ImageScamScorer`: WL6 vision 0..1 image scam-score, LOG-only, OFF by default) |
| `risk/` | `limits` (`RiskManager`: caps, daily-loss kill-switch, cooldowns, `kelly_fraction`, `expected_return`, `reeval_action`) |
| `execution/` | `base` (`ExecutionBackend` ABC + `Fill`), `paper` (`PaperBackend`: priced fills + stacked fees + slippage), `pricing` |
| `portfolio/` | `portfolio` (`Portfolio`/`Position`/`ExitParams`/`ClosedTrade`; `should_exit`, `should_take_partial`, `derisk_fraction`), `pnl` (`compute_stats`) |
| `storage/` | `db` (SQLite WAL: tokens, candidates, trades, equity, lessons, verdicts, observations, trade_outcomes, missed_winners, miss_judged, hold_concentration, wallets, buyer_reputations), `archive` (Parquet) |
| `alerts/` | `telegram` (async alert queue), `commands` (`/pnl` `/positions` `/status` `/stop` `/resume` `/help`) |
| `backtest/` | `replay`, `label`, `train_gbm`, `simulate`, plus study/sweep tools |
| `main.py` | `Bot`: wiring of all the loops above |

## Conventions that matter

- **Every threshold is config, never hardcoded.** All tunables live in `config.py` as a frozen `Settings` dataclass (with nested `SafetyGates`/`MomentumThresholds`/`RiskLimits`/`Fees`/`ExitParams`), each env-overridable via `Settings.load()`. When you add a knob: add the field, add its `_str/_float/_int/_bool` env read in `load()`, keep the dataclass default in sync with the `load()` default, and add a `validate()` warning if it has an invariant. These are **community heuristics / starting priors, not validated edges** — they must be calibrated from labeled outcomes (`backtest/`), not frozen.
- **`FEATURE_NAMES` column order is load-bearing.** `filter/features.py` is the single source of truth shared by the live GBM scorer and offline training. The model is trained on this exact order; adding/reordering changes both sides at once. Keep tri-state safety flags as `-1 = unknown`. Extra dataset-only keys (e.g. `shadow_score`, `creator_launches`, `bsr_h1`, `sell_pressure`, `entry_latency_s`, `name_reuse_count`, `image_scam_score`, `smart_buyer_count`, `dumper_buyer_count`, `st_risk_score`, `st_top10`, `st_snipers_pct`, `st_insiders_pct`, `st_bundlers_pct`, `st_dev_pct`) are deliberately *not* in `FEATURE_NAMES` so training/scoring ignore them — keep it that way.
- **LLM is advisory; the deterministic risk layer vetoes.** The brain returns a `Verdict`, but `RiskManager` caps/cooldowns/kill-switch, the concentration veto, the scalp-lane veto, the data-backed-setup gate, and the expected-return gate can all override a "buy". Gate on intrinsic `c.mode` (not the LLM-overridable `verdict.mode`) for the scalp veto. Never let the LLM relax a deterministic safety gate.
- **Phase-tag changelog convention.** Code comments carry tags that act as the de-facto changelog: Phase 1–5 (build), AI1–AI9 (intelligence layer), R1–R14 (autonomous roadmap), D5 (risk accounting), E3 (calibration), W2–W6 (win-rate), P1–P7 (survival/de-risk), P9 (production-harden: task-lifecycle/leak/timeout/migrate/config-override audit fixes), P10 (intelligence-layer: escalation-as-critique, base-rate/self-consistency prompt cues, calibration→brain feeds, risk_flags/MFE-MAE/primary_signal capture, miss-regret honesty), P10b (re-verify batch: verdict-source/SKIP/recalled-lesson logging + selectivity tool, sign-safe _WATCH + creator-rep decay + calibration audit table, meta-regret loop + skip-attribution, platitude filter + relevance/rug-protected recall, m5/sell-velocity/res-sup_dist signals, deterministic risky-setup size-down + scorer off-high/trend penalties), A1–A3 (free-data rug-avoidance from the Spec-v1 review: A1 Token-2022 extension hard-fails, A2 Jupiter quote-only honeypot check, A3 explicit 2x take-initial principal-recovery exit; B1 RugCheck.xyz risk cross-check as a last-line pre-buy veto), G1–G3 (gated real-money/live/metered). When you change behavior, tag the comment with the relevant phase and a one-line rationale, matching the existing style.
- **Dependency-free tests must stay green.** `tests/` imports only stdlib + the package; `run_tests.py` runs without pytest. Do not introduce a test that needs pandas/numpy/lightgbm/anthropic/etc. at import time — those deps are optional (Phase 2/3) and guarded behind `try/except` in runtime code.

## Gotchas

- **The goal is RUG-AVOIDANCE + honest measurement + learning, NOT profit (evidence-backed, see `RESEARCH.md` + `KNOWLEDGE.md`).** A 2026 multi-source literature review (arXiv 2602.14860 / 2602.13480 / 2603.24625 + Solidus Labs + Pine Analytics, adversarially verified — see `KNOWLEDGE.md` for the cited, folklore-flagged version) concludes: systematic free-data pump.fun trading is STRUCTURALLY net-negative — only **~0.6-1.4% graduate**, **~98.6% collapse** (a liquidity-collapse proxy, NOT a verified fraud rate; the often-cited "76% rug" FAILED verification), **>73% of migrated tokens fall below 40% of migration price within 20 minutes**; even the best ML screen only REDUCES loss (AUPRC ~0.573, ~56% loss reduction), never profits; the only profitable actors are deployer-funded same-block insider snipers (speed/MEV edge we lack). Our own methodology (leakage avoidance, stacked fees, honest PnL, the profit-factor gate) is exactly what the literature endorses, so our net-negative result + AUC~0.58 are CORRECT, not bugs. The realistic ceiling is LOSS-MINIMISATION. Do NOT tell the user there's an edge or promise profit. The #1 documented detectable signal is HOLDER CONCENTRATION + bundle/coordinated-wallet detection (validates our top-5 conc / conc_rise + BuyerIntel); profit likely needs speed/MEV infra we lack. Survival-first: defense is in size/exposure/selectivity + banking principal fast, not tighter stops.
- **SCALP buy lane is OFF** (`trade_scalp=False`) — full-history reconstruction showed it is structurally negative on free data (round-trip fees never cleared). Scalp candidates are still scored + observed; only the *buy* is vetoed. Don't re-enable it casually.
- **Holder concentration is the #1 free rug tell.** The catastrophic losses are whale dumps that no score/liquidity separated; top-5 (non-vault) concentration — and especially a *rise* in it while held — is the one signal that does. It needs a real Helius RPC (`HELIUS_RPC_URL`); on the public Solana RPC `getTokenLargestAccounts` is unsupported and the whole concentration safety layer goes dark.
- **Never commit `.env` or the Helius key.** `.env` (and `*.local`) is gitignored and holds secrets; `.env.example` is the only committed template. Do not paste keys into code, tests, comments, or commit messages.
- **Concentration / safety stay advisory in the scorer/dataset** even when they veto the buy — so a token is still scored/observed/labeled. Don't "fix" that by dropping vetoed tokens from logging.

## Where to look first

- **Change a threshold / add a knob:** `config.py` (`Settings` + nested dataclasses + `load()` + `validate()`).
- **Change entry logic / ranking / the buy path:** `main.py` `_evaluate_once` and `_try_open`; scoring in `signals/scoring.py`; setup grading in `signals/setup.py`.
- **Change safety / scam logic:** `filter/rules.py` (`evaluate`, `scam_likelihood`); concentration reads in `feed/helius_rpc.py`; re-eval in `main.py` `_reeval_loop`.
- **Change exit / sizing / risk:** `portfolio/portfolio.py` (`Position.should_exit`/`should_take_partial`/`derisk_fraction`, `ExitParams`); `risk/limits.py`.
- **Change the brain / prompts / memory:** `agent/policy.py` (CONSTITUTION), `agent/brain.py`, `agent/schema.py`, `agent/memory.py`; backend selection in `agent/backends.py`.
- **Add a feature for the model:** `filter/features.py` (`FEATURE_NAMES` — load-bearing), and keep `backtest/train_gbm.py` consistent.
- **Storage / logging schema:** `storage/db.py`. **Backtest / labeling:** `backtest/`.
- **Add a test:** `tests/test_*.py` (stdlib-only); run with `python run_tests.py`.
