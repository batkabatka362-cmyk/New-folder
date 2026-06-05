# DEVLOG — memebot

A chronological engineering journal for **memebot**, a Solana pump.fun memecoin
**paper-trading AI** (Python / asyncio). Reconstructed from in-code phase-tag
comments, `memebot-plan.md`, `ROADMAP.md`, and project memory — git history was
empty when this log was written. Dates are noted only where the source records
them.

Doctrine that frames everything below: **paper only** (real money is gated and
unapproved); **survival-first** (capital preservation comes from size / exposure
/ selectivity, not tighter stops); **every threshold is config**, a community
heuristic to be calibrated from labeled outcomes, never a proven edge; **LLMs are
advisory, the deterministic risk layer vetoes**; and the binding ceiling is
**DATA, not code** — net edge is not yet proven on free data.

---

## Build — Phase 1-5 (data + paper loop foundation)

- **Phase 1** — End-to-end paper loop: PumpPortal free discovery WS
  (`subscribeNewToken`/`subscribeMigration`) → DexScreener enrichment → Helius
  authority checks → rule gate + threshold scorer → fee/slippage paper executor →
  SQLite (WAL). Why: a running, honest paper loop on free feeds before any AI.
  (Reviewed, 14 fixes.)
- **Phase 2 (plumbing)** — `filter/features.py` (FEATURE_NAMES single source of
  truth), `filter/gbm.py` (LightGBM scorer, dormant), `signals/scoring.py` +
  `escalation.py`, `backtest/` replay/label/train. Why: lay the GBM rails so the
  rule scorer can be swapped for a calibrated model once data exists.
- **Phase 3** — Autonomous brain: frozen CONSTITUTION (`agent/policy.py`),
  `Verdict`/`ReflectionNote` schemas, SQLite-backed `AgentMemory` (lessons
  recalled before decisions), per-trade reflection. Risk layer still decides.
  (Reviewed, 7 fixes.)
- **Local AI** — Ollama backend (gemma3:4b live); `LLM_BACKEND=auto` prefers
  local (free, no key) → cloud Claude → rule fallback. Why: an always-available
  free brain. (Reviewed, 7 fixes.)
- **Phase 5** — Deferred and gated: `MODE=live` raises `SystemExit`; live
  execution backends (Jupiter / PumpPortal Local Tx) are G1, real-money, unapproved.

---

## Intelligence layer — AI1-AI9 ("AI trader, NOT a bot")

User steer: make the AI the reasoning core (reads price action, aware of its own
book, learns), not a mechanical rule-bot.

- **AI1** — Price-action / trendline context in the brain prompt: DexScreener
  5m/1h % change (was fetched then discarded) + a per-token polled trendline
  (`TokenState.price_obs`/`record_price`/`price_trend`). Trend kept OUT of
  FEATURE_NAMES so scorer/GBM/archival are untouched.
- **AI-doctrine** — Encoded the user's operating doctrine into the constitution:
  survive first, be decisive, HOLD only when durable safety converges else
  SCALP-and-exit-fast, compound but never at the cost of survival.
- **AI2** — Self-awareness: a "YOUR BOOK" prompt block (equity vs start,
  open/max positions, daily loss vs cap, loss streak, recent win-rate) from
  `Bot._account_state()` so the brain reasons on its own performance.
- **AI3** — Meta-learning loop: `_meta_reflect_loop` distills aggregate recent
  performance into ONE strategy-level lesson stored in memory (advisory, never
  edits config).
- **AI4** — Deeper technicals: `TokenState.technicals()` adds EMA-cross,
  breakout/breakdown, support/resistance distance to the prompt.
- **AI5** — Winner-characteristics study (`backtest.winners`): on 5m labels,
  **1h volume is the dominant tell** (winner/loser lift ~2.05×); buy/sell ratio
  does NOT separate (~1.02×). Drove the R2-a scorer reweight.
- **AI6** — Scam "sixth sense": `rules.scam_likelihood()` grades 0..1 danger
  (authority / LP / concentration / unknown) shown to the brain, distinct from
  the hard veto. Part 2 (retrain GBM on real data) folded into the E/P phases.
- **AI7** — Holder-quality → hold-vs-scalp: `rules.holder_quality()`
  (healthy/concentrated/mixed/unknown) leans the brain at entry; complements
  R13's in-flight re-eval.
- **AI8** — No blind scalp: `_has_momentum()` requires a real signal (m5/h1>0, a
  rising trend, or vol_spike≥1.5) before a SCALP buy — stops paying the
  round-trip fee on zero-signal curve entries.
- **AI9** — No data-blind trades: `require_data_backed_setup` skips curve-only
  tokens with no DexScreener pool (liquidity≤0) — they only bleed fees.
- **Launch-catch reality (verified, 39k candidates):** on free tier we do NOT
  catch the launch second — 99.6% of candidates are brand-new curve tokens with
  no real-time data; all early trades were already-indexed HOLD tokens. True
  launch-moment trading needs the metered trade stream (G3, gated).

---

## Autonomous roadmap — R1-R14 (self-advancing queue)

- **R1** — Dependency-free test runner (also pytest-compatible).
- **R2** — On-chain top-5 holder concentration via Helius (vault-excluded);
  activates the previously-dead concentration gate for migrated tokens.
- **R3** — Telegram commands (`/pnl /positions /status /stop /resume /help`) via
  getUpdates long-poll, chat-id restricted; `RiskManager.resume()`.
- **R4** — Parquet archival (`storage/archive.py`); graceful skip if pyarrow missing.
- **R5** — Trade-stream watchlist plumbing (`feed/watchlist.py`), bounded top-N.
  OFF by default — the stream is metered/spends SOL (never auto-enabled).
- **R6** — Backtest sim (`backtest/simulate.py`): logged candidates → gate +
  scorer + entry threshold → net return vs forward price minus fees.
- **R7** — CLI status dashboard (`python -m memebot.status`): equity/ROI, funnel,
  exit-reason breakdown, verdicts, lessons. Read-only / WAL-safe.
- **R8** — Hardening: `Settings.validate()` startup warnings, graceful-degradation
  audit, expanded edge tests, README sync.
- **R9** — GBM activation attempt #1: 171 labeled, **AUC 0.53, no edge** →
  correctly NOT deployed (kept the transparent rule scorer). (Later revived in E.)
- **R10** — Compounding sizing: position size scales with equity ×
  risk_per_trade_frac × brain size_pct, capped by per-trade / balance / liquidity.
- **R11** — Breakeven stop: arm once up ≥arm_pct, then exit at "breakeven" if it
  falls back — never round-trip a winner to a loss.
- **R12** — Min expected-R entry filter: skip a buy unless
  `expected_return(...) ≥ MIN_EXPECTED_RETURN`.
- **R13** — Dynamic holder-driven re-eval (`_reeval_loop`): re-check open
  positions' concentration — cut fast if it worsens, extend scalp→hold if it
  broadens. Sell path refactored into shared `_close_position`.
- **R14** — Local-AI comprehension fix: gemma3:4b mis-read "revoked authority" as
  bad → constitution now states SAFE/DANGER polarity explicitly + few-shot;
  candidate prompt labels each authority "REVOKED → SAFE" / "ACTIVE → DANGER".

### Supporting strands (tuning / defense / data / robustness / research)
- **T1/T2** — Entry-threshold sweep + synthetic-scenario exit simulator; a tight
  SL whipsaws winners and does NOT shrink the rug worst-case.
- **D1-D6** — Defensive risk posture (multi-agent designed, sim-grounded):
  ExitParams wired into Settings; defensive profile applied; exposure guardrails
  + consecutive-loss breaker ENFORCED; EV-margin sweep; **D5** hard per-trade
  equity-fraction ceiling (5%) + `sol_at_risk` survival accounting logged at
  startup; **D6** scorer calibration (dropped dead social term, affine-rescale
  raw rule sum to [0,1] — was stuck at max 0.56, so entry 0.55 ≈ the ceiling and
  the bot barely traded; now entry 0.50 admits ~375 mints, slam-dunk stays rare).
- **E1-E3** — Data pipeline: fixed-horizon labeling; data-quality report (median
  token tracked only ~41s — only the **5-minute** horizon has data); **GBM
  retrained, val AUC ~0.72-0.79, signal real not leakage** — but the **live edge
  did NOT transfer (6/6 losing scalp timeouts)** due to survivorship bias
  (trained on tokens that survived ≥5m, can't price the live population) →
  reverted to the rule scorer; E3 adds a safe retrain cadence that NEVER deploys
  a worse model.
- **F2/F3** — Robustness: defaults-consistency audit (catches the dual-default
  ENTRY_THRESHOLD footgun); `x or default` audit fixing 5 `now=0.0` bugs.
- **R2-a..c** (deep-research, 106 agents): **VOLUME now leads the scorer** (#1
  corroborated signal; buy/sell cut to 0.10 as non-predictive); graded
  concentration penalty; "revoked is necessary, not sufficient" prompt note.
  Honest meta-finding: on free data there is no new alpha — the edge is
  avoidance/discrimination, which corroborates the existing survival-first design.

---

## Win-rate — W1-W6 (more demo volume + higher win rate, 2026-06-02)

Goal: raise paper-trade VOLUME (faster in-distribution data → unblock P4 GBM)
WITHOUT degrading win rate, all grounded in collected data.

- **W1 — Diagnose (8-agent).** Honest finding that overturned the optimistic
  headline: reconstructing the FULL `trades` table (not the recency-biased
  `trade_outcomes`) shows the bot is **net-NEGATIVE** — SCALP 0/39 wins (round-trip
  9.5% never cleared); HOLD losses are RUGS that gap past the −35% price SL at
  ~3% slippage (realized −90%+). Entry score and entry liquidity both fail to
  separate winners from rugs → fix the EXIT side, not the entry. Volume is
  SUPPLY-bound (DexScreener-indexed mints), so lowering entry_threshold adds zero
  tradeable mints. **Always reconstruct PnL from `trades`.**
- **W2 — Applied the data-grounded changes** (config-favored, reversible; 12-agent
  review, 5 fixes incl. 1 HIGH): **SCALP buy lane OFF** (`trade_scalp=False`,
  vetoed on intrinsic `c.mode` so the LLM can't relabel a scalp as hold);
  **liquidity-collapse exit** (close a HOLD when its pool drains <50% of entry —
  the rug the price SL gaps past); **observe HELD positions** (`_observe_held` so
  their 5m P4 label completes); more HOLD concurrency (max_positions 3→5,
  exposure 0.15→0.20); wider intake; faster blackout cut. Measured ~6h post-W2:
  flipped net-NEGATIVE → **net-POSITIVE (+5.3%, win 43%, pf 1.92, n=14, thin)**.
- **W3 — Concentration blind spot.** The 3 catastrophic SL losses are the entire
  drag; rug vs winner entry features (score/liq/bsr/mcap) fully overlap. The one
  separating tell — top-5 concentration — was **0/2000 KNOWN**: the configured RPC
  was the PUBLIC mainnet-beta, which returns EMPTY for `getTokenLargestAccounts`.
  Added `supports_largest_accounts()` + an honest "ENABLED/DISABLED" startup
  probe. The #1 free win-rate lever became a USER action (set a Helius key).
- **W4 — Helius key wired → concentration LIVE + entry whale-rug VETO.** User
  supplied a free Helius key (`.gitignore` created first). Live concentration
  returns real values (e.g. 97.6%). But the measured distribution forced an
  honest correction: fresh graduated tokens are **uniformly high-concentration
  (82-96%, median ~89)** — static concentration does NOT cleanly separate rugs
  from winners. Recalibrated all thresholds onto the live distribution
  (max_top5 90→96, warn 30→90, holder_cut 85→96, holder_good 40→85) so they stop
  cutting/blocking the median winner. Built `concentration_study.py` (joins entry
  concentration → realized PnL; verdict INCONCLUSIVE until labeled data accrues).
- **W5 — Creator rug-history filter** (user's #1 pick, behavioral/free):
  `data/creator_history.py` tallies launches per creator wallet (worst spam
  600-726). SOFT/graded, not a hard veto — launch count is real-but-noisy (a
  +237% winner came from a 20-launch creator) → `scam_likelihood` +0.15 when
  launches > 40. Logged for later threshold calibration.
- **W6 — Concentration-TREND cut + EV/Kelly skeleton.** Static concentration
  didn't separate, but a RISE while held = consolidation = rug prep →
  `_concentration_rose` cuts "conc_rise" when top-5 climbs ≥8pp above entry.
  Added fractional-Kelly sizing skeleton (`kelly_fraction`, `use_kelly_sizing`)
  **OFF by default** — Kelly on a miscalibrated p amplifies losses; gated until
  P4 supplies a calibrated win-probability. P4 data accelerating:
  labelable_5m_indexed 17 → 77 (need ~hundreds; did NOT train on 77).

---

## Survival / de-risk — P1-P7 (trade only what we can read; survive late)

Reframe (5-agent design): we can't catch launches on free tier → trade only
DexScreener-indexed tokens, classify GOOD setups, and survive even if late. Root
cause of the bleed: a momentum-only score ranks already-pumped/fading tokens
highest right before they dump.

- **P1** — Deterministic GOOD-SETUP classifier (`signals/setup.py`): hard vetoes
  for thin liquidity / ghost liq-mcap / far-off-high / post-pump fade / downtrend /
  breakdown / wash spike / unsafe → grade good/marginal/bad; drop 'bad' before
  ranking; size scaled by setup quality (marginal risks half).
- **P2** — Fixed data collection at the source (the survivorship cure):
  observation logging DECOUPLED from the rule gate — log every indexed snapshot
  (gate-failers included) once at its most-resolved point, matching what the live
  scorer sees (no train/serve skew). New `observations` + `trade_outcomes` tables.
  Killer finding the moment it shipped: the honest in-distribution
  labelable_5m_indexed was **5** (vs an inflated legacy 658) — that gap IS the
  survivorship bias, now measured.
- **P3** — Survival-first exits: breakeven arm 0.20→0.12; arm-trail (exit 6% off
  peak once armed, scalp included); loss-aware scalp stall; fade exit on a
  confirmed live trend break (never overrides the hard SL); **partial
  take-profit** wired (bank half above the round-trip cost, free-roll the rest
  under the breakeven-trail). Fixed a LATENT partial-sell accounting bug — full
  round-trip now booked at close (a single full sell stays byte-identical).
- **P4** — In-distribution model: **PLUMBING DONE, training still DATA-GATED.**
  `retrain.py --population observations --indexed-only --death-drop 0.9` (unbiased
  set); precision-over-base-rate deploy gate (AUC alone hides a useless entry
  point on a rare-event imbalance); **shadow mode** (a candidate model scores into
  each observation's `shadow_score`, never sizes a trade) to validate against real
  forward outcomes before promotion. Do NOT train on ~15-77 mints.
- **P5/P6** — (No standalone P5/P6 items; the P-series carries P1-P4 build and
  jumps to the P7 de-risk work below.)
- **P7 (this session)** — De-risk + sell-pressure (user doctrine: when RISKY,
  recover the principal as profit and free-roll the rest at zero downside):
  - **Principal-recovery DE-RISK partial** — `Position.derisk_fraction()` solves
    for the fraction whose net proceeds recover the remaining cost basis, capped
    so a house-money tail always rides; `_take_partial(arm=False)` free-rolls it.
  - **Sell-pressure tape monitoring** — `_sell_pressure()` (DexScreener h1
    buy/sell ratio below a floor AND m5 rolling over) in `_observe_held`; de-risks
    if in profit, opt-in cut otherwise. The trader's "about to get dumped" feel,
    made into data.
  - **Concentration warn-zone de-risk** — in `_reeval_loop`, when held
    concentration is in the warn zone (concentrated but below the cut line),
    de-risk if in profit.
  - **Logs `sell_pressure` / `bsr_h1` into observations** (non-FEATURE_NAMES keys)
    so P4 can LEARN the rug-from-tradeflow separation before it's promoted to a
    model feature.

---

## Faster catch + regret-learning + patience — P8 (this session)

User asks: use 2 local models in parallel to catch new coins faster; let the AI
WAIT (not trade everything) and LEARN from misses ("why did we fail to get in?").

- **Dual local model (fast screen → deep confirm).** `DualLocalLLM` composes two
  Ollama models — a FAST screener (gemma3:4b) on every `decide()` + a DEEPER
  confirm model (qwen3:8b) consulted ONLY when the brain escalates a low-conviction
  buy — the LOCAL analogue of the cloud Haiku→Sonnet cascade (`supports_escalation`
  now true for local). Degrades gracefully to a single `OllamaLLM` when the confirm
  model is empty / unpulled / identical. Backends wire it for `local` + `auto`.
- **Backend-correct source labels.** Fixed a latent mislabel: `brain.decide`
  hardcoded `"haiku"` whenever `supports_escalation` — which would tag every local
  fast verdict as haiku. Each backend now names its own tiers
  (`base_source`/`escalated_source`); a `getattr` fallback preserves the cloud
  literals (and any backend without the attrs).
- **Parallel decide → sequential buy (the real throughput win).** `_decide_and_open`
  decides the top candidates CONCURRENTLY (bounded by `llm_parallel`, default 3) so
  a fresh launch no longer waits behind earlier candidates' slow LLM round-trips,
  then applies buys SEQUENTIALLY under the live risk gate (re-checking
  `max_positions`/`has_position` per buy — no double-open). Honest caveat: a single
  Ollama instance largely serializes generation, so the local speedup is mostly
  head-of-line-blocking removal; the win is near-linear only against the cloud.
- **Regret-learning (`_miss_learn_loop`, off hot path).** The counterfactual
  complement to per-trade reflection: replay recently-OBSERVED-but-NOT-bought mints
  from `observations`, re-price once via DexScreener, and when a skipped token's
  forward return clears `miss_win_threshold_pct` (+50%) record a "missed winner"
  (new `missed_winners` table) and queue a `brain.reflect_miss` lesson ("which
  filter wrongly rejected it?"). Bounded: `miss_judged` makes each observation
  re-priced at most once (cost O(observations) not O(obs×passes)); `obs_id` UNIQUE
  dedupes; reflections capped/pass; lesson confidence damped ≤0.7. The prompt
  hard-reaffirms Default-to-SKIP so the loop can't drift into over-trading
  (survivorship is the #1 risk — a recorded miss may have pumped-then-died).
- **Measured patience.** `_patience` counters (observed / ranked / bought) →
  a `skip_rate` line in the report, paired with `winners_traits().gate_failed_frac`
  (HIGH ⇒ the hard gates wrongly reject winners; LOW ⇒ the entry/brain is too
  strict). Makes "the AI can WAIT" a measurable, tunable property, not a vibe.

### P8 (cont.) — data → ready-to-use → self-improving (user: "classify well, learn by itself")

- **Ready-to-use classified dataset** (`backtest/dataset.py`): a 6-way outcome
  classifier (winner / fade / loser / flat / rug / dead) over the unbiased
  `observations` population (earliest-per-mint, group-safe), reconstructing each
  mint's forward path OFFLINE from its own logged prices; exports a clean CSV
  (full features + P7/P8 signals + peak/trough/final returns + class) and a
  per-class median-feature SEPARATION table. **Live (106 mints): winner 28% /
  rug 25%, and the WINNER vs RUG entry-feature medians OVERLAP** — re-confirms,
  honestly, that static entry features don't separate rugs.
- **Self-calibration loop** (`backtest/calibrate.py` + `_calibrate_loop`, hourly,
  off hot path, DB read in a worker thread): classifies our own outcomes, derives
  ADVISORY threshold suggestions ONLY from features whose winner/rug medians
  diverge cleanly (overlap ⇒ no suggestion — it won't fabricate from noise), and
  logs them + stores the strongest as a recalled lesson. **NEVER auto-applies to
  live risk config.** Live: "no feature cleanly separates yet" — the correct,
  honest output; data_ready flips true once ≥30 winners AND ≥30 rugs.
- **Creator-reputation skill** (the new "skill"): learns each creator's REALIZED
  rug/win rate from our own classified tokens (`creator_reputation`, joined via
  `tokens.creator`), cached on the bot, surfaced to the brain prompt and a graded
  `scam_likelihood` penalty (≥`creator_rep_min_tokens` samples, ≥`creator_rep_rug_rate`).
  Live: 3 creators rated, two at a 50% rug-rate — the bot now remembers who burned
  it. Free, from our own data; grows as data accrues.

---

## Gated — G1-G3 (NOT autonomous; require explicit user approval)

- **G1** — Phase 5 live execution (Jupiter / PumpPortal Local Tx, self-custody):
  **real money, irreversible. NOT approved.** `MODE=live` raises `SystemExit`.
- **G2** — Any remote push / deploy / publish / outward-facing action: **gated.**
- **G3** — Metered PumpPortal trade stream (real-time curve trades / tick
  features): **USER-APPROVED then DEFERRED (2026-05-31).** Cost ~0.01 SOL/10k
  events + a funded api-key (≥0.02 SOL wallet). Fully WIRED (key attached only
  when `trade_stream_enabled`, redacted from logs, watchlist bounded) but kept OFF
  — stay on paper + free feeds until the edge is proven; one env change enables it.
  Trades stay PAPER regardless (real-money execution is the still-gated G1).

---

## Current status

- **138 tests green** (`python run_tests.py`, dependency-free, also runs under pytest).
- **Net edge is NOT proven on free data** — and is honestly net-uncertain. W2
  flipped a reconstructed ~6h window net-positive (+5.3%, n=14, thin, leaning on
  one big winner), but the pre-W2 full history was net-negative. The binding
  ceiling is DATA, not code.
- **P4 GBM is DATA-GATED** — plumbing, deploy gate, and shadow mode are ready; the
  rule scorer is live. In-distribution labels are ~accumulating (labelable_5m
  jumped 17 → 77; need ~hundreds before training).
- **SCALP buy lane is OFF** (`trade_scalp=False`) — 0/39 wins, structurally
  negative round-trip on free data. Candidates are still scored + observed.
- **This session (P7):** principal-recovery de-risk partial, sell-pressure tape
  monitoring, and concentration warn-zone de-risk are LIVE; sell_pressure / bsr_h1
  are logged into observations for P4 to learn the rug-from-tradeflow signal.
- Concentration safety is LIVE on a free Helius key but is **not obviously
  discriminative as a static filter** (graduated tokens are uniformly
  concentrated); the rising-while-held TREND cut is the more promising bet,
  pending labeled data.

## Next / open

- **P4 calibrated-p → Kelly:** once in-distribution labels reach ~hundreds, train
  + validate the in-distribution GBM through the precision-gate / shadow-mode
  path; a calibrated win-probability then powers EV sizing and flips on
  fractional-Kelly (`use_kelly_sizing`, currently OFF on purpose).
- **Dual local model + miss-learning + patience (DONE this session, P8):** live;
  next is to MEASURE — does the dual cascade improve marginal-buy quality, and do
  the miss-lessons + gate_failed_frac point at a specific over-tight filter to
  calibrate? Watch the new report's `skip_rate` + `missed_winners` lines.
- **G3 needs a funded key:** the metered trade stream is wired and approved-then-
  deferred; real-time curve data (the route to true launch-moment trading) unlocks
  with a funded `PUMPPORTAL_API_KEY` + `TRADE_STREAM_ENABLED=true`. Real-money
  execution (G1) remains separately gated and unapproved.
- **Calibrate the soft thresholds from labeled outcomes:** creator-launch count
  (W5), concentration veto / TREND cut (W4/W6), and sell-pressure floor (P7) are
  all heuristics awaiting a winner/loser study on accruing labeled trades.

---

## Hardening + intelligence (this session — P9 / P10 / P10b)

Two production-hardening audits and two intelligence-layer assessments (multi-agent,
adversarially verified), then implemented + tested. Suite 212 → **232 green**.

- **P9 production-harden audit (13 fixes):** task-lifecycle (`gather`→`FIRST_COMPLETED`
  so a dead loop exits nonzero and the supervisor restarts it), LLM timeout, config
  env-override wiring (SafetyGates/Momentum were silently non-overridable), 4
  unbounded-dict leaks capped, generic SQLite forward-migration (`PRAGMA table_info`
  reconciler) + `idx_obs_ts`, DexScreener/Helius `JSONDecodeError` catch, lazy Ollama
  client. Repo also made committable: 57 untracked source files staged, bytecode/db/
  lock/log un-staged + gitignored.
- **P10 intelligence (12 fixes):** escalation = critique-of-screener (not blind re-roll)
  + risk-aware escalation predicate; CONSTITUTION base-rate + size/flag self-consistency
  cues; `primary_signal` + `risk_flags` + MFE/MAE capture into reflection; calibration→
  brain feeds (AUC lesson, creator win_rate, measured outcome base-rate); miss-regret
  honesty (glitch-cap, sustained-move guard).
- **P10b re-verify batch (26 fixes):** verdict source/conviction + SKIP-verdict + recalled-
  lesson logging + `backtest/selectivity.py`; sign-safe `_WATCH` + creator-rep recency
  decay + `calibration_suggestions` audit table; gate_failed_frac→meta-reflect + skip-
  reason attribution + miss verdict-mix; platitude filter + relevance/rug-protected recall
  + exit-reason lesson keying; m5/sell-velocity/vol-conc signals + `res_dist_pct`/
  `sup_dist_pct` appended to FEATURE_NAMES; deterministic risky-setup size-down (rule+LLM)
  + scorer off-high/trend ranking penalties.
- Deployed once under `memebot.supervisor` (the P9 autonomy fix verified live: a
  legitimately-completing watchlist task did NOT tear the bot down).

---

## Spec v1 alignment + the free-data rug-avoidance roadmap (next)

A detailed external "Solana memecoin bot" technical spec was reviewed against the build.
Honest coverage: the spec's **free-data + paper-compatible backbone is ~85-90% already
built** — measure + deterministic gate + paper harness + labeled-dataset backtest +
risk/exit + multi kill-switch (the spec's Phase 1-2 + §7-§14). Remaining work splits three
ways, and we attack ONLY the first group now (free, paper-safe, pure rug-avoidance):

**A. FREE-DATA rug-avoidance gaps — DOING NOW, highest first:**
- **A1 — Token-2022 extension hard-fails.** `helius_rpc` detects the token-2022 program
  but does NOT inspect its extensions. Parse the mint's extensions and HARD-FAIL the rug
  vectors most bots miss: `transferHook` (arbitrary code on transfer = stealth honeypot /
  sell-block), `transferFeeConfig` above a cap (stealth tax), `permanentDelegate` (dev can
  move/burn your tokens), `defaultAccountState=frozen` (new accounts frozen). Free Helius
  read; pure deterministic safety; paper-compatible. **First.**
- **A2 — Honeypot sell-route test (Jupiter quote sim).** A quote-only check (no real money):
  small buy-quote → sell-quote; no sell route or excessive implied tax ⇒ honeypot HARD-FAIL.
  Degrades to a no-op when Jupiter is unreachable. **Second.**
- **A3 — Explicit "recoup principal at 2x" asymmetric exit.** We have partial-TP/derisk/
  trail; add the exact rule (sell the original principal's worth at ~2x, ride the rest as
  house money) so a later rug can't zero a winner. EV-math backbone. **Third.**
- After each: MEASURE the lift on labeled data via `backtest/rug_model.py` (dodge vs
  winner-loss) — the spec's §7 loop, which we already have. Not guesswork.

**B. G3-tape-gated (needs a funded PUMPPORTAL_API_KEY ≥ 0.02 SOL):** bundle/sniper
detection, smart-money copy-trade, real-time creator-dump. Architecture is built
(`data/smart_money.py`, the `sniper_share`/`bundle_share`/`creator_dump_ratio` features);
only the metered data feed is OFF. This is RESEARCH.md's "#1 documented missing signal".

**C. Infra we structurally lack — NOT now (paper-only + the profit barrier):** Yellowstone
gRPC Geyser, Jito bundles, pre-warmed tx, regional/co-located servers, MEV protection, live
snipe execution. These are exactly the speed/MEV infra RESEARCH.md says is required for
profit and that we do not have; live execution is gated (`MODE=live` raises) and unapproved.
The spec's own closing warning agrees: free-data memecoin trading is structurally hostile and
the realistic ceiling is **loss-minimisation**, not a promised edge.

---

## Reference capture — Spec v1 (full, nothing dropped)

Verbatim-faithful capture of the external spec + research notes so none of it is lost.
Status tags: built · partial · GAP (do now) · G3-gated · infra-we-lack.

### Design principles
1. Loss-avoidance > profit-capture — memecoin upside is capped, loss = -100% (rug); one
   rug wipes ~10 winners, so the system's #1 job is dodging harmful trades. [built — our thesis]
2. Deterministic gate first, model later — don't trust a weak LLM on critical calls;
   safety = explicit rules, model only for fine separation. [built]
3. Data is the moat — log every launch, build a labeled dataset, backtest filters;
   measure, don't guess. [built — observations + backtest spine]
4. Asymmetric exits — at 2x pull the principal, ride the rest as house money. [partial — A3]

### Launch detection (real-time)
- Yellowstone gRPC Geyser (Helius LaserStream / Triton / Shyft), NOT RPC polling (polling
  lags 100s of ms). Subscribe: Raydium/PumpSwap pool-creation; pump.fun token-create +
  migration (PumpPortal convenient). Emit LaunchEvent{mint,pool,creator,ts,source,initial_lp}.
  [infra-we-lack — we use PumpPortal free WS, higher latency]
- Latency stack (where snipe is won): regional server near validators (Frankfurt/Amsterdam);
  multi-RPC + multi-Jito-region simultaneous send; pre-warmed tx (cache a recent blockhash,
  pre-build the swap skeleton, fill only the mint at fire time, sign+send). [infra-we-lack]

### SAFETY GATE — 3a HARD FAILS (any one => auto-reject)
- Mint authority != null — read SPL Mint account — dev mints infinite supply. [built]
- Freeze authority != null — read mint account — dev freezes your tokens (honeypot). [built]
- LP not burned/locked — LP token mint supply burned, or locked in a known locker — dev pulls
  liquidity. [partial — pump.fun auto-burns LP on migration]
- Honeypot (not sellable) — Jupiter buy-quote -> sell-quote sim — can't exit. [GAP — A2]
- Token-2022 malicious extension — inspect program owner + extensions — hidden rug vector. [GAP — A1]
- Dev wallet on blacklist — local serial-rugger DB — repeat offenders. [built+ — learned, not static]
- (pump.fun pre-migration: mint/freeze are standardized — focus on dev-buy % + bundle.)

### SAFETY GATE — 3b SOFT SIGNALS (weighted score, calibrated by backtest)
score = w1*top10_concentration_penalty (>25% top10 = bad) + w2*liquidity_size + w3*token_age
(older survived = safer) + w4*bundle_penalty (many same-block buyers) + w5*dev_buy_pct_penalty
+ w6*funding_cluster_penalty (holders funded from one source) + w7*smartmoney_bonus.
Weights NOT guessed — start equal, tune via backtest. [built mostly — top-5 conc; bundle/sniper/
smart-money are G3-gated]

### Token-2022 awareness (most bots miss this) — GAP A1
If token program == Token-2022, inspect extensions:
- TransferHook — dev runs arbitrary code on transfer (block sells / add tax). Unverified hook
  => HARD FAIL unless whitelisted.
- TransferFee — high/variable fee = stealth honeypot. Cap acceptable (e.g. <5%).
- PermanentDelegate — dev can move/burn your tokens at will. HARD FAIL.
- DefaultAccountState=frozen — new accounts frozen by default. HARD FAIL.
Parse via mint-account extensions (@solana/spl-token getMintExtensions, or raw TLV / Helius
jsonParsed mint.extensions).

### Bundle & wallet-clustering (the same-entity tell)
- Bundle detection: count distinct buyers in the launch/creation block (first few slots); flag
  if bundled_supply_pct > 50% across newly-created wallets = dump setup. [G3-gated — needs tape]
- Funding-source tracing: for top-N holders trace SOL origin (parent funder); all from one
  wallet / one CEX-withdrawal pattern => single entity = penalty. Use Helius enhanced tx
  history / getSignaturesForAddress, walk 1-2 funding hops. [partial/built — N12 funder cluster, 1-hop]

### Smart-money module (higher EV than blind snipe) — G3-gated, architecture built
1. Watchlist of consistently-profitable wallets (scan historical realized PnL via Birdeye/
   Helius; keep sustained-positive, reasonable trade count, survivorship). 2. Subscribe to
   their tx via Geyser. 3. On a watched BUY of a new token -> run SAFETY GATE -> mirror entry
   within seconds. 4. Mirror exits or apply own rules. Refresh weekly; drop degraded wallets.
   (We have data/smart_money.py PnL ledger + is_smart; fed by G3 tape, currently OFF.)

### Backtest & labeled dataset (highest-leverage non-flashy work) — built, our spine
1. Record every launch via Geyser with all gate features at t0/t+10s/t+1m/t+5m -> DB.
2. Label outcomes after 1-24h: rugged (LP pulled / -90%+ / unsellable) vs ran vs flat.
3. Backtest the gate vs labeled history; per-check precision/recall for catching rugs + how
   many good tokens each check wrongly rejects (false-positive cost).
4. Tune soft weights to maximize rug-recall at acceptable good-token rejection. Re-run weekly.
Target metric: loss-avoidance rate (the "38%") — track every iteration on paper.

### Exit & risk rules
ENTRY: fixed small size/trade. EXIT (evaluate continuously):
- TAKE-INITIAL: at +100% (2x) sell the original principal worth; remainder rides as house
  money. (the most important rule) [partial — A3]
- HARD STOP: price <= -35% from entry -> market sell. [built]
- LIQUIDITY-DROP: pool LP shrinks > X% suddenly -> market sell NOW (rug in progress). [built]
- WHALE/DEV DUMP: a top holder / dev sells large % -> exit. [built]
- MOMENTUM TIMEOUT: no upward move within N s and volume dies -> exit. [built]
- TRAILING: after 2x, trail stop on remainder (e.g. -25% from local high). [built]
All exits route through Jito (live) with slippage caps to avoid sandwich.

### Position sizing & EV
Fixed fraction/trade (1-2% of session bankroll); never all-in. With high rug rate, EV is
positive only if exits are asymmetric -> the take-initial rule does the heavy lifting (recoup
principal on winners before rugs zero you). Avoid full-Kelly (brutal variance); fractional/
fixed small size. [built — fixed-frac + half-Kelly OFF]

### OpSec & kill switch
- Hot wallet holds only loss-affordable funds; main capital in a SEPARATE wallet.
- Optionally a fresh wallet per session (avoid self-clustering; one mistake != total loss).
- Kill switch: daily loss >= X% or M consecutive losses -> halt ALL activity. [built — multiple]
- Private keys ONLY in env/secret — never hardcoded/logged. [built]
- RPC failover; check blockhash expiry; idempotent orders (no double-buys); rate-limit +
  backoff. [partial — paper; P9 network-resilience fixes landed]

### Data sources / APIs (Solana)
- Real-time launch/tx stream — Yellowstone gRPC Geyser (Helius LaserStream / Triton / Shyft) — [infra-we-lack]
- pump.fun realtime feed + trade API — PumpPortal — [built discovery; trade = G3]
- RPC + enhanced tx history — Helius — [built]
- Price / liquidity / holders / security — Birdeye — [gap; we use DexScreener]
- Pairs / liquidity — DexScreener — [built]
- Token risk score — RugCheck.xyz — [gap; optional enrich]
- Extra token security — GoPlus / SolSniffer — [gap; optional enrich]
- Swap exec + honeypot quote sim — Jupiter — [A2 quote-only / infra-we-lack for exec]
- Fast / MEV-protected tx landing — Jito — [infra-we-lack]

### Strategy depth notes (research)
- Smart-money following > blind 10-second snipe (which is a co-located-bot speed race most
  lose). Tracking profitable wallets and mirroring (after the gate) is higher-EV, more
  repeatable, lower rug risk because they pre-filtered. [G3-gated]
- Latency engineering decides snipe wins (Geyser not polling; Jito tip; regional; pre-warmed
  tx; multi-send race). [infra-we-lack]
- Token-2022 is a new rug vector (transfer hook / fee / permanent delegate / default-frozen) —
  stealth honeypot beyond classic freeze. [GAP — A1]
- Bundle/cluster analysis — count same-block buyers; 60-70% bundled to fresh wallets = dump
  setup; trace top-holder funding to a common source. [G3-gated / built]
- Backtest on labeled data is how 38% actually rises — precision/recall per check; our own
  recorded data is the moat. [built]
- Asymmetric sizing math — recoup principal at 2x so a -100% rug can't zero a winner; risk
  management > profit capture. [partial — A3]
- OpSec — hot vs main wallet split; per-session wallet; kill switch; keys in secrets. [built/partial]
- Sandwich/MEV risk — your own buy can be sandwiched; slippage caps + Jito-private + no huge
  single order. [infra-we-lack — live only]
- Honest caveat (theirs and ours): even with all of the above, memecoin sniping is not
  guaranteed profitable long-term — the field is brutal, the edge thin. Biggest realistic
  lifts: smart-money following (G3) + labeled-data backtest (built). Demo until consistently
  positive; real money only in loss-affordable size.

### Phased roadmap (theirs) vs ours
1. Phase 1 measure (Geyser logger + labeled dataset + paper engine, no trading). [built — via
   PumpPortal/observations, not Geyser]
2. Phase 2 gate (hard fails + soft score + backtest). [built except A1 Token-2022 + A2 honeypot]
3. Phase 3 smart money (watchlist + copy entries through the gate). [G3-gated]
4. Phase 4 execution (Jito + pre-warmed tx + latency). [infra-we-lack]
5. Phase 5 tiny live (loss-affordable funds, kill switch). [gated/unapproved]

### Acceptance criteria (per module)
- Safety gate: on labeled backtest catches >X% of rugs (e.g. 80%+) at acceptable good-token
  rejection.
- Paper engine: trustworthy loss-avoidance / profit-capture metrics.
- Kill switch: provably halts on threshold breach (unit-tested).
- No private key in code / logs / version control.

---

## Spec-v1 free-data rug-avoidance — IMPLEMENTED (A1-A3 + B1)

The "A" group (free-data, paper-safe, pure rug-avoidance) + the first "optional enrich" item, built
+ tested. Suite -> 237 green. Each is survival-first (only ever vetoes a buy or recovers principal;
never relaxes a gate, never spends real money) and degrades gracefully on any external failure.

- **A1 — Token-2022 extension hard-fail.** `helius_rpc.get_token2022_risk` parses a Token-2022 mint's
  extensions and HARD-vetoes transferHook (non-null) / permanentDelegate / defaultAccountState=frozen
  / transferFee above a cap. New `Candidate.token2022_risk`, `rules.py` veto, SafetyGates knobs
  (`require_token2022_safe`, `token2022_max_transfer_fee_pct`), wired in the safety path + logged to
  the dataset. Pure parse split out for unit tests.
- **A2 — Jupiter quote-only honeypot check.** New `feed/jupiter.py`: buy-quote -> sell-quote; no sell
  route or instant round-trip recovering < `honeypot_min_roundtrip_keep` of the SOL in -> veto. Last-
  line in `_try_open` (buy candidates only). Quote-only, no key, no real money; no-op on failure.
- **A3 — Explicit 2x take-initial.** Found `derisk_proactive_pct` (N5) was modeled in `backtest/exitlab`
  but NEVER wired into the live manage loop (a dead knob). Added an OWN-latch take-initial
  (`take_initial_pct=1.0`=2x, `Position.initial_taken`, `should_take_initial`): at 2x recover the full
  principal via the existing `derisk_fraction`, ride the rest as house money so a rug can't zero a
  winner. Composes with the P3 partial (independent latches); `_take_partial` generalized with a
  `latch` arg; exitlab updated to replay it faithfully.
- **B1 — RugCheck.xyz cross-check.** New `feed/rugcheck.py`: a free, read-only second opinion right
  before a buy; veto on a RugCheck 'danger'-level risk (catches novel scam-DB patterns our own gate
  misses). Defensive parse (tolerant of schema drift); no-op on any failure so an external service
  can't strangle the bot. Config: `rugcheck_enabled` / `rugcheck_veto_on_danger`.

MEASUREMENT (pending data): the lift of A1/A2/A3/B1 on the rug-dodge rate must be measured via
`backtest/rug_model.py` (dodge vs winner-loss) once the bot runs the new code and accrues labeled
outcomes — the spec's §7 loop. Deploy -> accumulate -> measure; do NOT claim a lift before the data.
