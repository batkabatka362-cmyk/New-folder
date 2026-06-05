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

---

## Measurement spine + holder funding-cluster (this session, post-commit)

- **Honest-measurement spine run + a new tool.** Established the rigorous baseline: CLEAN book
  -1.709 SOL (NO-GO, loss-min as expected); separation NULL (no feature separates winners from
  rugs); rug_model RUG-avoidance AUC 0.616 STABLE (the doctrine-aligned lever) vs winner 0.558
  inconclusive. Built `backtest/gate_attribution.py` (spec section 7): the SAFETY GATE's rug-
  avoidance confusion matrix -> baseline ~51% rug-dodge / ~41% winner-loss, with a per-named-reason
  breakdown that populates as `rule_reasons` (now logged into observations) accrues -- so the A1/A2/
  B1 deterministic-check lift (which should dodge rugs at ~zero winner cost) becomes measurable.
  Initial commit 25355e5 captured the build through here.
- **Holder funding-cluster (free-data concealed-concentration, RESEARCH.md's #1 missing signal via
  FUNDING not the G3 tape).** `helius_rpc.get_holder_owners` resolves the top non-vault holders'
  OWNER wallets (getTokenLargestAccounts returns token accounts, not wallets), `largest_funder_cluster`
  flags when several "independent" top holders share ONE funder = one entity behind many wallets.
  Wired into the safety path (cached, immutable funders), an advisory `scam_likelihood` penalty + a
  brain signal. OFF by default (`holder_cluster_check`) -- costs several extra Helius reads per
  survivor; enable when the RPC has headroom. Advisory (vault/CEX false-positive risk keeps it off the
  hard veto). This is the free-data path to the coordinated-wallet signal G3 would otherwise be needed
  for. (Post-commit; uncommitted.)

---

## WL1 — winner-loss attribution + buy/sell-gate calibration (this session)

The gate dodges ~51% of rugs but FALSE-rejects ~41% of winners (`gate_attribution`). That false-reject
is the #1 lever on the loss-min net — every winner the gate wrongly throws away is forgone upside that
costs ~zero rug to recover if the rejecting check doesn't actually separate.

- **`backtest/winner_loss.py` (new, offline/read-only/advisory).** For each mint the live gate ACTUALLY
  rejected (`observations.rule_passed` never True), reconstruct its candidate from logged features
  (`candidate_from_features`), re-run the REAL `rules.evaluate`, and read which named reason fired —
  then tally, per reason, the WINNERS wrongly rejected vs the RUGS correctly dodged, with a precision
  column (rugs/(rugs+winners)) and a TOO-STRICT verdict when winners_lost > rugs_dodged. Only the
  REASON is re-derived (the kept/lost split uses the gate's real live decision); scam_likelihood is
  slightly under-counted on old logs (can only miss a reason, never invent one).
- **Empirical finding.** Of 26 rejected winners / 38 rejected rugs: `buy/sell_low` (min_buy_sell_ratio)
  topped the table at **17 winners lost / 16 rugs dodged = 48% precision** — i.e. a HARD gate operating
  at coin-flip, exactly as expected since buy/sell ratio is the documented non-predictive feature
  (AUC~0.5; the scorer already downweights it). `vol/mcap_low` (16/25, 61%) and `buyers_low` (8/12,
  60%) are mixed-but-net-useful; `liquidity<3000` (2/5, 71%) earns its keep.
- **Calibration applied (advisory, human-reviewed — NOT auto-tuned).** `min_buy_sell_ratio` 1.5 → 1.0:
  the gate now vetoes only NET-SELLING flow (buys < sells), keeping the [1.0, 1.5) winners. Re-running
  the tool confirms buy/sell_low's winner-loss drops 17 → 9 and ~8 rejected winners (26 → 22 total) now
  re-derive to NO blocking reason — i.e. the new threshold would have kept them. STOPPED at 1.0 on
  principle: the tool still flags 9-vs-5, but loosening past parity means buying into net sell pressure,
  which contradicts survival-first — 1.0 is the meaningful break (buy/sell parity), not arbitrary.
  `vol/mcap`/`buyers` left as-is (net-useful at 60-61%; loosening them sheds real rug-dodge). Suite
  240 → 241 (`test_winner_loss_attribution_too_strict`, deterministic single-reason re-derivation).

---

## WL2 — exit-policy calibration + a de-risk latch correctness fix (this session)

A multi-agent workflow (5 levers × investigate→adversarial-verify→synthesize, 11 agents) swept the
win-rate / code-quality space. Two findings survived adversarial scrutiny and were adopted; three were
honestly rejected/deferred (recorded here because the NO is as load-bearing as the YES).

- **De-risk latch composition bug (correctness, the higher-priority fix).** `_derisk_if_profitable`
  gated on `pos.partial_taken` and called `_take_partial` with the DEFAULT `latch="partial"` — so a
  clean P3 partial permanently latched OUT the risk-triggered principal-recovery de-risk. A position
  that banked a partial and THEN saw sell-pressure (tape) or a concentration rise could never recover
  its principal — the exact survival case de-risk exists for. Fix: gave de-risk its OWN latch
  (`Position.derisk_taken`), mirroring take_initial's `latch="initial"`, so partial / initial / derisk
  all compose independently. Verified safe: `derisk_fraction` reads the REMAINING `cost_sol` (which
  `apply_sell` reduces proportionally), so firing after a partial recovers only the remaining principal,
  never over-sells; `arm=False` leaves the partial's `breakeven_armed` intact. Offline exitlab is
  unaffected (it replays the PRICE-only proactive derisk, which legitimately shares the partial latch;
  the LIVE risk-flag derisk is a separate path). New regression `test_partial_then_derisk_composes`.
- **Exit tuning: `partial_tp_frac` 0.5 → 0.7 (WL2, exitlab over 234→236 paths).** Banking more of the
  win on the partial lifts win-rate 52% → 56% (+9 winners, 0 lost) and median forward return +0.010 →
  +0.042 — both OUTLIER-ROBUST (the gain is spread across 92/234 paths, not 3 outliers). Net -13.23 →
  -12.90. Independently re-verified by a second agent. Deliberately did NOT move `trail_after_arm_pct`
  (0.04): its ~1 SOL net "gain" was 208% concentrated in 3 paths with zero median lift and a negative
  interaction with the partial — overfit, rejected. dataclass + load() defaults kept in sync; exitlab
  scale-out math tests updated for the new 0.7 weighting.
- **Rejected (honest, survival-first):** (1) a vol/mcap_low **second-chance lane** — re-admits ~1.67
  rugs per winner recovered (62.5% precision ≈ the gate's own 61%, within noise), and its proposed
  secondary discriminator (`top5_concentration_pct`) is UNKNOWN on all ~24 relevant tokens, so it
  cannot separate; (2) **raising `entry_threshold`** for selectivity — the entry score has ~ZERO
  correlation with realized outcome (Pearson −0.027; deciles non-monotonic; high-score WR 44.8% vs
  low 46.3%), so the +0.56 SOL counterfactual is pure survivorship on 134 trades. Both confirm the
  honest doctrine: no static feature separates, selectivity is a false lever on free data.
- **Deferred (needs data): `conc_rise_cut_pct` 8 → 5.** Mechanism is sound + survival-first, but the
  effectiveness is UNMEASURED — only 1 `conc_rise` exit observed and no hold-time concentration
  time-series is logged. The completeness critic's #1 next move falls straight out of this: instrument
  `_reeval_loop` to PERSIST per-position top-5 concentration at entry + each re-check, converting the
  #1 free rug tell (a concentration RISE while held) from reasoned-about to measurable. Suite 241 → 242.

---

## WL3 — hold-time concentration capture (the WL2 critic's #1 next move)

WL2's deferred lever (`conc_rise_cut_pct` calibration) and its completeness critic agreed on the same
gap: the #1 free rug tell — a top-5 holder concentration RISE WHILE HELD (dev/insiders consolidating =
distribution prep) — is the one signal the doctrine says separates the catastrophic whale-dump losses,
yet the live `_reeval_loop` ACTED on each reading and then THREW IT AWAY. With no persisted trajectory,
the threshold could never be calibrated from outcomes. This is a DATA-CAPTURE fix, not a threshold bet.

- **Persist the trajectory.** New `hold_concentration` table (ts, mint, symbol, entry_conc_pct,
  conc_pct, delta_pp, pnl_pct, action) + `Storage.log_hold_concentration`. `_reeval_loop` now computes
  the rise/action ONCE and logs EVERY real reading (delta vs entry + the action it drove: conc_rise /
  holders_cut / extend / derisk_conc) BEFORE acting — off the hot path, only when concentration is
  known. Refactor is behavior-preserving (the cut/extend/derisk branches are unchanged; both
  `reeval_action` and `_concentration_rose` already no-op on a None reading). The schema migration creates the
  table on any old DB on next connect.
- **Read it honestly.** New `backtest/conc_trajectory.py`: peak rise-above-entry per held mint, joined
  to the realized (glitch-excluded) `trade_outcomes` book, then a candidate-threshold sweep (losers
  caught vs winners wrongly cut, with precision) so `conc_rise_cut_pct` can be calibrated from data —
  ADVISORY, never auto-applied. Degrades to "not enough hold-time data yet" until the running bot
  accrues readings (the table ships empty; the signal needs a real Helius RPC + a known entry baseline).
- **Why this and not the deferred threshold change directly.** WL2 REJECTED moving `conc_rise_cut_pct`
  8→5 as unmeasured (1 observed firing, no hold-time series). The honest move is to instrument first,
  let the data accrue, then calibrate — converting a NO-GO/DEFER into a testable lever rather than
  guessing. Suite 242 → 244 (`test_conc_trajectory_analyze` + `test_hold_concentration_persistence`).

---

## WL4 — give-back levers swept (the WL2 critic's #2): an HONEST no-default-change + a dead-knob wired

Swept the two PRICE-ONLY (exitlab-replayable) give-back-mitigation levers the WL2 exit study skipped:
`take_initial_pct` (principal-recovery bar, default 1.0 = 2x) and `derisk_proactive_pct` (proactive
principal bank, default 0.0 = OFF). The honest verdict: **no default change is justified** — but the
sweep surfaced a real code gap worth fixing.

- **`take_initial_pct` 1.0 → 0.5: REJECTED (overfit).** Lowering the bar to 1.5x nudges win 55→56% and
  net +0.574, but **91% of that net gain is 3 paths** (net-minus-top-3 = +0.052) and it touches only
  31/238 paths — the same outlier-concentration red flag WL2 rejected `trail_after_arm_pct` for. Keep
  2x: a high, conservative bar that barely caps the rare big winner.
- **`derisk_proactive_pct`: the robust lever WAS a DEAD KNOB.** At 0.20 it was the sweep's best (net
  −12.88 vs −13.91, median +0.040→+0.047, and — unlike take_initial — net-minus-top-3 still +0.46 with
  63/23 paths improved/worsened, i.e. NOT outlier-driven). But it was modeled ONLY in `exitlab` and
  never wired into the live manage loop (A3 had found the same and added `take_initial` instead) — so
  setting it did nothing live. **Fix: wired it into `_manage_loop`** via a new
  `Position.should_take_proactive_derisk` (shares the P3-partial latch, mutually exclusive with the
  clean partial, exactly as exitlab models), inserted between the take-initial and partial branches.
- **Kept OFF by default (0.0) — wiring changes NO behavior.** The exitlab gain is net+median but
  WIN-RATE-NEUTRAL (the user's stated goal is win-rate) and it caps big-winner upside (give-back
  mitigation by design), and 55% of even its net edge is top-3-concentrated. So the disciplined call is
  to make the studied knob REAL (env-overridable, no longer misleading dead config) but leave enabling
  it to forward confirmation, never by feel. Honest outcome: a code-quality fix + a measurement that
  PREVENTED an overfit default change. Suite 244 → 245 (`test_proactive_derisk_gate_off_by_default_and_fires_when_enabled`).

---

## WL5 — entry-timing / fill-divergence: the leak is STRUCTURAL (an honest entry-side audit)

The WL2 critic's #3: "the leak is entries, not exits — look at discovery→buy latency + fill-divergence
before more exit micro-tuning." Done. The audit is the deliverable; it confirms the loss-min ceiling.

- **Latency adverse-selection (paper-vs-live) is MODEST, not the leak.** `fill_divergence` over 295
  matched fills: implied extra round-trip cost +0.81% at ~6s (eats 8% of the 9.5% cushion), and −0.20%
  at ~15s — and the per-leg MEDIANs are ~0/favorable, so the mean drag is a few big moves, not
  systematic. (Already fed into `readiness`.) A weak lower bound, but on the paper data latency is not
  the dominant cost.
- **No entry feature separates (re-confirmed).** `separation` winner-vs-rug AUC 0.44–0.56 across every
  feature (price_change_m5/h1 and bsr_h1 top out at 0.56 = noise). Entry-velocity features are DARK at
  entry anyway (they need a rolling history the first snapshot lacks). Entry AGE doesn't separate
  (age_min AUC 0.55; winners + rugs both entered young, median <1 min).
- **A discovery→buy-latency "edge" looked huge — and was adversarially PROVEN to be noise.** Scout
  found buys filled faster (≤~62s) were net +0.47 SOL / 43% win vs slower >62s at −0.96 / 36%, a ~+1.5
  SOL apparent gate. A focused adversarial workflow (7 agents: permutation tests, outlier-jackknife,
  confound check, implementation scout) REJECTED it: permutation p=0.66 on the win-rate gap (r²=0.024),
  the entire FAST-half net is **3 lucky fills** (FAST-minus-top-3 = −0.94 SOL), the threshold response
  is non-monotonic (p-hacking signature), and the single biggest book winner (+0.77 SOL) sits at 132s
  — squarely in the slow tail any "fast-is-better" gate would DELETE. No defensible threshold exists
  (T=180s bootstrap 95% CI [−2.49, +3.04]). This is the textbook overfit-on-tiny-n the doctrine warns
  about; inventing the gate was correctly refused.
- **DEFER-AND-LOG (the one code change).** Capturing the latency float is ~free and may calibrate
  forward at larger n (the WL3 play). `_try_open` now records `entry_latency_s = TokenState.age_s()`
  (first-seen→buy) onto the entry features as a DATASET-ONLY key (not in `FEATURE_NAMES`, like
  `bsr_h1`), riding the existing `trade_outcomes.entry_features` path. Logging only — it never gates a
  buy or touches `RiskManager`.
- **Bottom line:** the entry-side leak is STRUCTURAL — free data cannot separate winners from rugs at
  entry, and the latency cost is real but modest and not closeable without speed/MEV infra we lack.
  This is the loss-minimisation ceiling the literature predicts, now audited from the entry side too —
  not a bug to tune away. Suite 245 → 246 (`test_entry_latency_logged_on_open`).

---

## WL6 — the IMAGE / branding scam layer (the user's visual edge) — measured, fixed, and a vision scorer

The user pushed back hard: "I can avoid these rugs at a glance from the IMAGE / name / DUPLICATION, yet
the bot reads the backend data and still loses — the filter is bad." A fair, sharp point: every signal
the bot uses is on-chain/market; it had NEVER looked at the token's name/image. Investigated honestly.

- **The data CONFIRMS the duplication is massive** — 9,635 distinct name+symbol brandings are reused
  across **55,677 of 76,351 named mints (~73%)**, including blatant impersonations (metamask ×258,
  banana dad ×614, daddy ×249). The user is right that this is the texture of pump.fun.
- **But name+symbol reuse does NOT separate on the tokens we TRADE.** Branding-reuse AUC(rug>winner) =
  0.41 on the 250 classified mints (winners are reused MORE — popular memes pump), and on our 88 actual
  buys the LOSSES are the LOW-reuse cohort (reuse≤5: net −1.17 SOL / 38% win) while high-reuse buys are
  break-even (reuse>25: +0.00 / 50%). Why: our market gates (data-backed-setup, liquidity, DexScreener-
  indexing) already filter the launch-time dupe spam before it could ever reach the buy path — so a
  branding-reuse VETO would CUT the neutral cohort and KEEP the bleeders. Refused it.
- **Found + fixed a real bug:** `name_reuse_count` was computed onto `c.features` but never copied into
  `_features_json`, so it logged as dark (all-zero) for the entire history — the separation read it as a
  flat 0. Now carried (dataset-only key) so the relation is actually measurable going forward.
- **The IMAGE is the untested dimension a count can't capture** (a NOVEL-name token — our losing
  low-reuse cohort — can still have an obvious-scam IMAGE). Per the user's choice, built a VISION
  scam-scorer: `agent/vision.py` `ImageScamScorer` sends a GATE-PASSED candidate's image (resolved from
  the launch `uri` → metadata JSON → image, ipfs:// gateway-aware) to a vision model (local Ollama
  `llava` = FREE, or a cloud base URL) for a 0..1 scam_score (structured output). Threaded `uri` through
  `NewTokenEvent → TokenState → _try_open`; the score rides `entry_features` as `image_scam_score`.
- **LOG-ONLY (the user's call + the doctrine).** No veto: it's cost-gated to buys (a few/min), cached
  per mint, and FULLY defensive (any failure → None, never blocks a buy or raises). OFF by default
  (needs `ollama pull llava` or a cloud endpoint). The image→outcome separation is calibrated forward
  from real labels before any veto is even considered — measure first, like WL3/WL5. Config:
  `image_scam_enabled` / `image_scam_host` / `image_scam_model` / `image_scam_timeout_s` /
  `image_ipfs_gateway` / `image_scam_max_bytes`. Suite 246 → 249.

---

## WL7 — early principal-recovery (the user's "bank at 1-2x") + the exits-vs-selection decomposition

The user's strategy, stated plainly: ">95% of coins fail (scam/rug/dump/honeypot/freeze/sharp-drop) —
don't try to PREDICT the rug; the moment a position is at 1-2x, RECOVER the principal so an immediate
loss becomes impossible, then decide whether to ride the rest. That makes the loss probability drop
sharply." Survival-first to the core — and it exposed a question we hadn't sized: how much of our loss
is even FIXABLE by exits?

- **`backtest/loss_decomp.py` (new) — the honest split.** Replays every logged path through the REAL
  exit ladder, finds the net-losers, buckets them by the PEAK they reached. The verdict on 244 paths /
  109 losers: **only ~17% (18 losers, −5.5 SOL) PUMPED ≥+30% then GAP-faded to a loss** — those are
  EXIT-addressable (bank principal earlier). **~83% (91 losers, −45.6 SOL) NEVER pumped** — a SELECTION
  problem no exit can touch (you can't bank a gain that never happened). This is the load-bearing
  reframe: exits recover a small bounded slice; the bulk is the wall the IMAGE / branding / filter-funnel
  work (WL6 +) targets. "Don't tune exits to fix a selection problem."
- **`take_initial_pct` 1.0 (2x) → 0.3 (1.3x)** — the user's "recover principal at 1-2x", calibrated to
  the addressable cohort (peaks ≥+30%, so bank by +30% before the gap-down). exitlab over 243 paths:
  net + median improve MONOTONICALLY as the bar drops (2x −15.36/+0.040 → 1.3x −14.69/+0.043) — NOT the
  non-monotonic p-hacking signature WL4 rejected `trail` for. Honest caveat: the net gain is modest and
  partly outlier-driven (net-minus-top-3 +0.145, 55 improved / 19 worsened, win-rate flat at 55%), so
  this is a SURVIVAL-FIRST loss-avoidance choice (recover principal earlier on the faders), NOT a claimed
  net edge. Composes with the WL2 partial (70% banked at +15%) — together they bank most of the position
  early and recover the rest by +30%, leaving a thin house-money tail that still rides a 200x. Reconciles
  with WL4 (which rejected `take_initial`=1.5x on a single outlier-driven net point): the monotonic trend,
  the independent loss-decomposition, and the user's survival objective now justify the lower bar.
- **The strategic through-line:** WL7 confirms exits are largely DONE (WL2 partial + breakeven trail +
  WL7 early take-initial handle the ~17% that pump). The remaining ~83% is SELECTION — which is exactly
  the user's IMAGE/branding edge (WL6) and the filter-funnel/timing ideas still ahead. Suite 249 → 250.

---

## WL8 — the filter-funnel audit (on-chain is EXHAUSTED) + the image-validation loop

The user's selection thesis: a STACKED funnel (liquidity / volume / locked / + many stages) where
passing ALL stages = a good coin; plus a TIME-frame when "traders come alive." Audited the funnel's
safety stages honestly — and the answer reframes the whole project.

- **Most rug-avoidance safety data is DARK at the buy.** Over the actual buys: top-5 concentration known
  on 60%, mint/freeze authority on 76%, LP-burn ("locked") on just 25% (most buys are PRE-migration
  curve tokens with no LP yet — N/A, not a bug). So 40% of buys carry no whale-concentration check, the
  user's #1 rug tell. (Helius IS configured — it's the per-cycle read budget, not a missing RPC.)
- **But requiring the safety data would NOT help — it INVERTS.** Split the buys by whether concentration
  was known at entry: conc-KNOWN n=58 net −1.51/34% win vs conc-DARK n=35 net **+0.87/46%**. The
  fully-vetted buys LOSE MORE — concentration-known correlates with later/established (worse) entries,
  not with safety. So "don't buy without the safety data" (the strict-funnel intuition) would keep the
  losing cohort and drop the winning one. Refused it.
- **The honest meta-finding:** EVERY on-chain free-data signal we have now measured — concentration,
  authorities, branding-reuse, entry-latency, entry-timing/age, the scorer — separates winners from rugs
  at AUC ≈ 0.5 on our TRADED set, confirmed from ~6 independent angles. The on-chain free-data well is
  exhausted (RESEARCH.md's structural wall, now thoroughly evidenced). More on-chain filter stages won't
  separate; the doctrine's loss-minimisation ceiling holds.
- **So the ONE untested modality is the IMAGE** (WL6) — a different signal class (human visual judgment,
  not on-chain numbers). Built `backtest/image_separation.py`: the read-side that, once the scorer is
  enabled (`ollama pull llava`, `IMAGE_SCAM_ENABLED=true`) and buys accrue an `image_scam_score`, reports
  AUC(loser>winner) + a veto-threshold sweep — the loop that will DECIDE whether the user's eye encodes a
  real edge before any veto is wired. Degrades to "no data yet" until enabled. Suite 250 → 251.
- **Security note:** a Helius API key was briefly echoed to the session by a diagnostic (it lives in the
  gitignored `.env`, never committed) — recommended the user rotate it. Do not print secret env values.

---

## WL9 — the #1 winner-finding strategy (smart-money), free-data foundation (Phase 1)

The user: "find how GOOD traders find winners, then make it real." Researched it (Nansen / GMGN / a
2026 arXiv multi-agent study, cited): the consistently-cited winner signal is **SMART-MONEY CONFLUENCE**
— when ≥N wallets with a proven PnL track-record buy the same fresh low-cap token, follow them; plus
EARLY-BUYER / sniper quality (the first ~70 buyers "often predict the pump"). Not a silver bullet (the
literature is explicit), but it is the one documented winner-edge we have NEVER had working.

- **Why we never had it:** `smart_money_share` / `sniper_share` / `bundle_share` / `creator_dump_ratio`
  log as KNOWN 0% (always the −1 sentinel), and the `wallets` PnL table is EMPTY. They all need
  TRADE-LEVEL buyer data (WHO bought), which on PumpPortal means `subscribeTokenTrade` — CONFIRMED
  metered at 0.01 SOL / 10k events on a funded API-key wallet (the G3 tape). That spends SOL, so the
  HARD CONSTRAINT keeps it OFF — the smart-money layer was dark by construction.
- **The FREE path (Phase 1, built):** reconstruct buyers from standard RPC instead of the paid tape.
  `HeliusRPC.get_recent_buyers(mint)` pulls the mint's recent signatures and diffs each tx's pre/post
  TOKEN balances by OWNER to extract the wallets that NET-RECEIVED tokens (= buyers) — SPENDING NO SOL,
  on the Helius RPC we already have. Pure `_buyers_from_tx` split out + unit-tested. EXPENSIVE (~1 +
  max_sigs RPC calls), so it is built to be cost-gated to a few top candidates + cached (a token's early
  buyers are immutable).
- **Next (Phase 2):** feed these buyers into the `SmartMoney` wallet-PnL store so wallet reputations
  accrue across tokens, then surface a smart-money-confluence + early-buyer signal (cost-gated in eval,
  LOG-first like every other signal — validate that it separates before it ever gates a buy). Honest
  framing preserved: this is the best untested winner-signal, not a promised edge. Suite 251 → 252.

- **Phase 2 (built): the reputation engine, on FREE labels.** The orthodox SmartMoney store needs each
  wallet's external PnL (buys+sells+SOL across all tokens) — infeasible from per-token RPC parsing. So
  `data/buyer_intel.py` `BuyerIntel` learns wallet reputation from OUR OWN labels instead: `record_buyers
  (mint, wallets)` stashes a token's early buyers; `on_outcome(mint, won, rugged)` credits/debits them
  when the token resolves; a wallet whose early-bought tokens tend to WIN is `is_smart`, tends to RUG is
  `is_dumper`; `confluence(wallets)` returns the smart-vs-dumper counts for a fresh candidate — the
  research's confluence signal in free-data form. Pure + bounded (eviction like CreatorHistory), snapshot/
  load for cross-restart persistence, fully unit-tested. Suite 252 → 253.
- **Honest operational limit (Phase 3 ahead):** the wiring will gate the EXPENSIVE buyer read
  (`get_recent_buyers`, ~1+max_sigs RPC calls) behind a config flag (OFF by default, like the image
  scorer), cost-gated to a few top candidates/cycle. The free-data catch: reputations accrue SLOWLY
  (we can only afford buyer reads for a handful of tokens/cycle on the Helius tier), where the metered
  tape would stream every trade — so the smart-money signal is real but SLOW to mature on free data. That
  is the honest ceiling, surfaced to the user before wiring the hot-path reads.

- **Phase 3 (built, the user chose the free path): full integration, gated OFF.** Wired `BuyerIntel` end
  to end: a new `_buyer_intel_scan` runs after ranking for the top-`buyer_intel_top_n` candidates (when
  `buyer_intel_enabled`), reconstructs buyers via `get_recent_buyers` (cached per mint, fully defensive),
  `record_buyers` for later resolution, and attaches `smart_buyer_count` / `dumper_buyer_count` /
  `early_buyers` to the candidate — logged into both `observations` (via `_features_json`) and the
  trade's `entry_features` so the confluence is validatable against realized outcomes. The calibrate loop
  resolves recorded buyers (`on_outcome`: winner→credit, rug/dead→debit) and persists the reputations to
  a new `buyer_reputations` table (seeded at startup). OFF by default + `top_n=1` so it spends no SOL and
  barely touches the Helius budget until the user opts in (`BUYER_INTEL_ENABLED=true`). LOG-first: the
  confluence counts never gate a buy until `image_separation`-style validation shows they separate.
  Honest limit unchanged — reputations mature slowly on free data. Suite 253 → 254.
