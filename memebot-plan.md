# Solana Memecoin Trading AI — Implementation Plan

> **Монгол тойм:** pump.fun дээр суурилсан, эхлээд PAPER trading (симуляци) хийдэг, Local шүүлт + Claude (Cloud) шийдвэртэй, Python дээр бичигдсэн memecoin trader.
> **Гол үнэн:** pump.fun-д Python-д зориулсан албан ёсны API байхгүй → **PumpPortal** (үнэгүй websocket) ашиглана. **Axiom-д bot API огт байхгүй** → автоматжуулахгүй, зөвхөн гар хяналтын UI болгож үлдээнэ.

**Locked choices:** pump.fun (+Axiom where feasible) on Solana · PAPER first, clean path to live · LOCAL fast filter + CLOUD (Claude) decision layer · Python · supports both scalps (seconds–minutes) and longer holds.

---

## 1. Reality check on APIs — what's actually allowed vs. what we'll use

**pump.fun**
- No official public HTTP/REST trading or data API. Official integration is on-chain only (program ID `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`) via TS `@pump-fun/pump-sdk` or `pump-rust-client`. Neither is Python.
- The website is powered by unofficial, Cloudflare-gated frontend endpoints (`frontend-api-v3.pump.fun`, `advanced-api-v2.pump.fun`). Read-only enrichment only, never mission-critical.
- **What we'll actually use:** **PumpPortal** as the primary feed. `subscribeNewToken` + `subscribeMigration` WebSocket streams are **free**. `subscribeTokenTrade`/`subscribeAccountTrade` are metered (~0.01 SOL / 10,000 messages, requires API key + linked wallet ≥0.02 SOL as of May 1, 2026).

**Axiom (Axiom Pro)**
- **No official API or websocket exists** for bots. Features are UI-only. "Axiom API" libraries are unofficial reverse-engineered clients (email/password + OTP) that break on frontend changes, plausibly violate ToS (ban risk), and require handing credentials to third-party code.
- **Decision:** We do NOT integrate Axiom for automation. It is not on the critical path — at most a manual UI for human cross-checking.

**Real providers we'll actually wire in:**

| Need | Provider | Cost | Notes |
|---|---|---|---|
| Discovery firehose (new mints, migrations) | **PumpPortal WS** (`subscribeNewToken`, `subscribeMigration`) | Free | One persistent socket, never one-per-token |
| Per-token live trade stream | **PumpPortal** `subscribeTokenTrade` | Metered ~0.01 SOL/10k | Enable only for a small watchlist |
| Market enrichment (price, liq, vol, txns, mcap, FDV) | **DexScreener REST** | Free, no key | 300 rpm tier for `/token-pairs`, `/tokens`, `/search` |
| Exact bonding-curve progress / mcap | **Solana RPC** `getAccountInfo` on BondingCurveAccount | Free | Authoritative; `complete==true` = graduated |
| Safety: mint/freeze authority, LP burn % | **Free Helius RPC key** (`getAccountInfo` jsonParsed) | Free tier (10 req/s, 1M credits/mo) | Most decisive, cheapest checks |
| Holder count / concentration | **Helius DAS** `getTokenAccounts` (paginate, de-dupe by owner) | Free tier | `getTokenLargestAccounts` = quick top-20 only |
| LP add/remove (rug) events, deep history | **Bitquery** (GraphQL/streams) | Free tier ~10 req/min — tight | Cache aggressively |
| Quick momentum / top-holders / snipers | **Moralis** REST | Free 40k CU/day | Not a source of truth for authority |
| **Paper fill pricing** | **Jupiter `/quote`** + DexScreener price | Free | Model paper fills against a real route so live switch is trivial |
| Live execution (later) | **PumpPortal Local Tx API** (`/api/trade-local`, 0.5%, self-custody) and/or **Jupiter `/quote`+`/swap`** | Fees stack | Avoid Lightning custodial mode |

**Fees stack (model all of these, even in paper):** PumpPortal 0.5% (Local) or 1% (Lightning) + pump.fun on-curve **1.25%** + **0.015 SOL** migration + Solana network/priority fees + Jito tip. Stock-market 0.05% slippage defaults are unrealistic for thin bonding-curve liquidity.

---

## 2. High-level architecture

```
                          SOLANA / pump.fun ecosystem
  ┌──────────────────────────────────────────────────────────────────────┐
  │ PumpPortal WS    DexScreener REST   Helius RPC      Bitquery/Moralis   │
  │ (new/migration)  (price/liq/vol)    (authority,     (LP events,        │
  │                                      LP burn,        snipers,          │
  │                                      holders, curve) momentum)         │
  └────┬───────────────────┬──────────────────┬────────────────┬──────────┘
       │ (free firehose)   │ (enrich, poll)    │ (safety reads) │ (deep)
       ▼                   ▼                   ▼                ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  TIER -1: DATA INGEST  (feed/)                                         │
  │  one WS reader  ──►  asyncio.Queue (bounded)  ──►  enrichment workers  │
  │  normalize events; build per-mint OHLCV candles (data/candles.py)     │
  └───────────────────────────────┬──────────────────────────────────────┘
                                   ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  TIER 0: LOCAL FAST FILTER  (filter/)         per tick, ALL tokens     │
  │  (a) RULE GATE  — hard safety/eligibility vetoes (µs, deterministic)   │
  │  (b) GBM SCORER — LightGBM/XGBoost, nthread=1, <1ms/row → ranked prob  │
  └───────────────────────────────┬──────────────────────────────────────┘
                                   ▼ survivors, ranked
  ┌──────────────────────────────────────────────────────────────────────┐
  │  SIGNAL & SCORING  (signals/)  scalp-score vs hold-score, per mode     │
  │  confidence band?  novelty/dedupe?  per-minute top-N cap?              │
  └───────────────────────────────┬──────────────────────────────────────┘
              │ slam-dunk yes/no            │ genuinely ambiguous + within budget
              ▼ (act locally)               ▼
  ┌────────────────────────┐   ┌──────────────────────────────────────────┐
  │  (optional TIER 1)      │   │  TIER 2: CLAUDE DECISION  (decision/)     │
  │  local Ollama LLM       │   │  Haiku 4.5 default (cached strategy       │
  │  fuzzy/text check on    │   │  prompt + structured JSON verdict);       │
  │  short list only        │   │  escalate hard/large to Sonnet 4.6        │
  └────────────────────────┘   └───────────────────┬──────────────────────┘
                                                    ▼ structured verdict
  ┌──────────────────────────────────────────────────────────────────────┐
  │  RISK LAYER  (risk/)  — deterministic; enforces hard limits, kill-switch│
  │  (LLM advises; THIS layer decides and can veto)                        │
  └───────────────────────────────┬──────────────────────────────────────┘
                                   ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  PAPER EXECUTOR  (execution/ → PaperBackend)   fills @ Jupiter quote   │
  │  + slippage + stacked fees → updates virtual portfolio                │
  │  [pluggable: PaperBackend | PumpPortalBackend | JupiterBackend later] │
  └───────────────────────────────┬──────────────────────────────────────┘
                                   ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  LOGGING & ALERTS  — SQLite(WAL) trade/event log; Parquet export;      │
  │  Telegram alert queue (own coroutine, off the hot path)               │
  └──────────────────────────────────────────────────────────────────────┘

  OFFLINE / ASYNC (not in live loop): Claude Batches API (50% off) for
  post-trade review, GBM training-label generation, nightly strategy review.
```

**Key architectural decision:** LLMs are deliberately **off the per-token hot path** — only rules+GBM can score "many tokens sub-second". The Claude tier is gated to the top **3–10 candidates/minute**.

---

## 3. Module / file layout

```
memebot/
  __init__.py
  config.py                 # .env: PUMPPORTAL_API_KEY, HELIUS_RPC_URL, BITQUERY/MORALIS,
                            #   ANTHROPIC_API_KEY, TELEGRAM_TOKEN/CHAT_ID, thresholds,
                            #   slippage/fee params, initial_sol, MODE
  constants.py              # PUMP_PROGRAM_ID, BondingCurve layout, fee consts
  main.py                   # async entrypoint: TaskGroup wires queues + tasks

  feed/                     # ── TIER -1 ingest (isolate all third-party APIs here)
    pumpportal_ws.py        # ONE websockets conn + auto-reconnect; subscribe*; normalize -> Queue
    dexscreener.py          # httpx: /token-pairs, /tokens (batch<=30), /search; 429 backoff
    helius_rpc.py           # getAccountInfo(jsonParsed) authority+LP burn; getTokenAccounts paginate+dedupe
    bonding_curve.py        # decode BondingCurveAccount; progress/mcap; complete flag
    bitquery.py             # OPTIONAL GraphQL: LP add/remove events, deep history
    moralis.py              # OPTIONAL REST: quick top-holders/snipers/swaps
    axiom_readonly.py       # OPTIONAL, OFF by default, flagged-unsafe (unofficial)

  data/
    candles.py              # resample trade ticks -> per-mint OHLCV (pandas)
    indicators.py           # pandas-ta-classic / ta: EMA/RSI/breakout/vol-spike
    token_state.py          # per-mint rolling state: reserves, holders, velocity buffers

  filter/                   # ── TIER 0 local fast layer
    rules.py                # deterministic hard gates (veto) — see §7
    gbm.py                  # LightGBM/XGBoost loader; predict(nthread=1) <1ms/row
    features.py             # build feature vector from token_state (shared w/ training)

  signals/
    scoring.py              # scalp_score() vs hold_score(); weights; mode selection
    escalation.py           # confidence band + per-minute top-N cap + dedupe gate

  decision/                 # ── TIER 2 Claude (and optional Tier 1)
    claude_agent.py         # AsyncAnthropic; cached strategy prompt; messages.parse
    schema.py               # Pydantic Verdict(action, confidence, size_pct, tp/sl, reason)
    prompts.py              # frozen STRATEGY_RULES (cached prefix) + per-candidate template
    local_llm.py            # OPTIONAL Ollama qwen2.5-3B Q4 shortlist check / offline fallback

  risk/
    limits.py               # position sizing, max exposure, daily loss cap, cooldowns
    rug_filters.py          # composite rug checks reused by rules.py + hold mode
    killswitch.py           # global halt; trips on drawdown / repeated errors / feed loss
    manager.py              # final deterministic gate over the verdict; overrides LLM

  execution/                # ── pluggable backends behind one interface
    base.py                 # ExecutionBackend ABC: quote(), buy(), sell(), get_price()
    paper.py                # PaperBackend: fills @ Jupiter quote + slippage + stacked fees
    jupiter.py              # (later/live) /quote + /swap, sign, broadcast
    pumpportal.py           # (later/live) /api/trade-local self-custody + Jito
    pricing.py              # Jupiter /quote wrapper used by paper fills AND live

  portfolio/
    portfolio.py            # virtual SOL, positions{mint: qty, avg_cost, opened_at, mode}
    pnl.py                  # realized/unrealized, equity, win-rate, drawdown, R-multiple
    position_mgr.py         # per-position TP/SL/trailing/timeout loop (scalp vs hold)

  storage/
    db.py                   # SQLite (WAL): tokens, candidates, verdicts, trades, equity
    archive.py              # periodic Parquet export (pyarrow) by day/token

  alerts/
    telegram.py             # telegram.Bot async send via own Queue (off hot path)
    commands.py             # OPTIONAL /pnl /positions /stop /mode handlers

  backtest/
    replay.py               # replay archived ticks through filter+scoring (no Claude)
    label.py                # label pump/rug outcomes -> GBM training data (Batches API)
    train_gbm.py            # train/calibrate LightGBM; export model artifact

  utils/
    logging.py
    rate_limit.py           # token-bucket per provider (300rpm DexScreener, etc.)
    money.py                # lamports/SOL/USD conversions, fee math

requirements.txt:
  solana, solders, websockets, httpx[http2], pandas, pandas-ta-classic (or ta),
  numpy, lightgbm (or xgboost), anthropic, pydantic, pyarrow,
  python-telegram-bot>=21,<23, python-dotenv
  optional: ollama, bitquery/moralis SDKs or raw httpx
tests/  + .env.example + README.md
```

---

## 4. Signal & scoring design

Two-layer: **hard safety gates (veto)** then a **weighted momentum/quality score**. Scalp and hold differ in gate strictness and weight mix. **All thresholds are config constants** — community heuristics, not validated edges; backtest/calibrate.

### Safety gates (binary veto — `filter/rules.py`, both modes)
| Gate | Reject if |
|---|---|
| Mint authority | non-null (print-supply risk) |
| Freeze authority | non-null (honeypot — can't sell) |
| LP burn/lock | <90% burned AND not locked (check lock **expiry**, not existence) |
| Bundled supply | >50% of total |
| Snipers | >50% of initial volume |
| Creator in bundle | true |
| Top-5 concentration | >90% (ex-LP/burn/CEX) |
| Single wallet (ex-LP) | >35% |
| Live dev dump | creator selling >20–30% of allocation now → **abort both modes** |

Concentration computed on **unique owners** (de-dupe token accounts), **excluding LP vault, burn/dead, known CEX/program addresses**.

### Momentum / quality signals (weighted 0–1)
| Signal | Healthy / buy threshold |
|---|---|
| Volume spike | current > **2–3×** 10-period avg |
| Volume/mcap | **5–15%+** healthy; <10% = dead |
| Buy/sell ratio | **>1.5–2:1** buys, from **many distinct wallets** |
| Holder velocity | accelerating new unique holders (leads pump 1–2 days) |
| Bonding-curve fill | fast steady fill from **many** buyers |
| Liquidity/mcap | **≥10–15%** |
| Social velocity | mention-velocity rising faster than price = buy; price up while mentions decelerate = top/rug (weight low, require corroboration) |

### Scalp vs hold scoring (the load-bearing difference)
```
scalp_score = 0.70 * momentum_velocity_block   # vol spike, buy/sell burst, curve fill, holder velocity
            + 0.20 * liquidity_tradability      # liq/mcap, slippage headroom
            + 0.10 * social_velocity
   gates: keep honeypot/freeze + live-dev-dump; RELAX long-term concentration tolerance.
   exit: TIGHT auto TP/SL + short max-hold timeout (seconds-minutes).

hold_score  = 0.40 * safety_quality_block       # ALL gates pass + broadening distribution
            + 0.35 * sustained_momentum          # sustained volume, healthy vol/mcap, buy breadth over hours
            + 0.15 * liquidity_health             # liq/mcap >=10-15%
            + 0.10 * social_corroboration
   gates: ALL safety gates STRICT (authority revoked, LP burned/locked, dev <20-30%, top-10 <30-40% ex-LP).
   exit: wider trailing stop; distribution-tightening or mention-deceleration = reduce/exit.
```

---

## 5. Local-filter vs Claude-decision split

**Tier 0 — local, every tick, all tokens (`filter/`)**
- **Rule gate first**: microsecond deterministic vetoes. Free, auditable.
- **GBM scorer**: LightGBM/XGBoost on survivors → ranked probability. **Pin `nthread=1`** (≈1ms/row). Vectorize across tokens per tick.

**Tier 1 — optional local LLM (`decision/local_llm.py`)**
- Ollama `qwen2.5-3B` Q4, only on the short candidate list. Fuzzy/text sanity check or **offline fallback**. Default **off** in MVP.

**Tier 2 — Claude, top-N/minute only (`decision/claude_agent.py`)**
- Escalate only when all three hold: (a) GBM score in **uncertain confidence band**, (b) within **per-minute top-N cap** (e.g. 5/min), (c) **not a near-duplicate**.
- **Model routing:** default **Haiku 4.5** (`claude-haiku-4-5`); escalate to **Sonnet 4.6** (`claude-sonnet-4-6`) only when Haiku confidence low or position large. **Never Opus on hot path.**
- **Cost/latency:** prompt caching (frozen `STRATEGY_RULES` system prompt before `cache_control`; volatile data after); pre-warm cache at startup; small `max_tokens` (~100–400); structured output via `messages.parse(output_format=Verdict)`; `AsyncAnthropic` to fan out concurrently.
- Verdict is **advisory** — `risk/manager.py` decides and can veto.

**Offline/async (`backtest/`):** Batches API (50% off) for post-trade review, GBM labeling, nightly review. Never in live path.

---

## 6. Paper-trading engine design

`execution/paper.py` (`PaperBackend`) behind the same `ExecutionBackend` ABC as future live backends — switching to live = swapping the backend, not rewriting strategy.

```
account = { sol_balance, positions{mint: {qty, avg_cost, opened_at, mode, tp, sl, trail}},
            realized_pnl, equity_curve[] }
equity = sol_balance + Σ(qty * current_price)
```

**Fills — model pump.fun reality:**
- Reference price from **Jupiter `/quote`** (or DexScreener) so paper fills mirror a live order.
- Buy fill: `price * (1 + slippage_pct)`; Sell: `price * (1 - slippage_pct)`.
- **Slippage:** thin liquidity → several %+, ideally **size-dependent price impact** vs available liquidity.
- **Stacked fees on every fill:** PumpPortal 0.5% + pump.fun 1.25% + network/priority fee + (post-grad) DEX fee; +0.015 SOL one-time at migration.

**Position management:** scalp = tight TP/SL + short timeout; hold = wider trailing stop, exit on distribution tightening / gate breach.

**Metrics:** realized/unrealized PnL, **win rate**, avg R-multiple, profit factor, max drawdown, exposure, fee drag, per-mode breakdown, time-in-trade. No keypair/signing/broadcast in paper.

---

## 7. Risk controls & rug filters

- **Rug filters:** §4 gates — authority via free **Helius RPC** (Moralis not authoritative for authority); LP burn via LP-mint supply (`burn% = (max−actual)/max*100`); distinguish burned vs locked (check expiry); concentration via Helius DAS de-duped owners excluding LP/burn/CEX; LP-removal via Bitquery; sniper/bundle via Moralis. Add **creator-wallet blacklist** (1% of creators ≈ 50% of launches).
- **Position/portfolio risk:** per-trade max size (scaled down as liq/risk worsen); max concurrent positions; max total exposure; per-mode caps; **daily loss cap**; per-token cooldown after loss; min liquidity floor; max modeled slippage.
- **Kill-switch — independent of LLM:** global halt on drawdown breach, repeated errors, or **WS feed loss**; manual `/stop` via Telegram.

---

## 8. Phased build plan

**Phase 1 — MVP: data + paper loop, no AI (build first)**
1. `config.py`, `.env`, `constants.py`, `storage/db.py`, `utils/`.
2. `feed/pumpportal_ws.py` — single WS, free `subscribeNewToken`+`subscribeMigration`, auto-reconnect.
3. `feed/dexscreener.py` + `feed/bonding_curve.py` + `feed/helius_rpc.py` (authority + LP burn).
4. `data/candles.py`, `data/token_state.py`; producer→bounded `asyncio.Queue`→consumers.
5. `execution/base.py` + `execution/paper.py` + `execution/pricing.py`; `portfolio/` + `pnl.py`.
6. `filter/rules.py` (safety gates) + simple threshold scorer stub (no GBM yet).
7. `alerts/telegram.py`. **Deliverable:** end-to-end paper trades on rule-based signals, logged, with PnL.

**Phase 2 — Local fast filter (GBM):** `backtest/replay.py` + `label.py` + `train_gbm.py`; `filter/features.py` + `filter/gbm.py`; `signals/scoring.py` + `escalation.py`.

**Phase 3 — Claude decision layer:** `decision/schema.py`, `prompts.py`, `claude_agent.py` (caching, Haiku default + Sonnet escalation); `risk/manager.py` + `killswitch.py`; optional `local_llm.py`.

**Phase 4 — Hardening & metered data:** enable PumpPortal trade stream for watchlist; Bitquery/Moralis enrichment; Parquet export; richer metrics + Telegram commands; calibrate thresholds.

**Phase 5 — Live path (only after sustained paper edge):** `execution/pumpportal.py` (Local Tx, self-custody, Jito) and/or `execution/jupiter.py`; wallet/keystore; flip `MODE=live` behind same interface; start tiny.

---

## 9. Key risks & honest caveats

- **No official pump.fun/Axiom automation surface.** Core data depends on PumpPortal (third-party) and DexScreener/frontend endpoints that can change/rate-limit. Isolate every provider behind `feed/`.
- **Axiom out of scope for automation** (ToS ban + credential-theft risk).
- **Every numeric threshold is a community heuristic, not a validated edge** — backtest and calibrate against your own labeled outcomes.
- **All signals are gameable** (split wallets, wallet fleets, fake renounce, freeze transfer). Only composite, cluster-aware scoring is robust.
- **Migration is the most fragile moment** — insiders dump at graduation.
- **Fees stack and are large** — undermodeling makes paper PnL look better than live.
- **LLM adds non-determinism + latency tail** — keep advisory; deterministic risk layer + kill-switch halt independently. Use exact model IDs.
- **Free-tier ceilings are real** (Bitquery ~10/min, Moralis 40k CU/day, PumpPortal metering, public RPC ~100/10s).
- **Paper ≠ live.** Cannot reproduce fill competition, MEV/sandwiching, block-landing failure, priority-fee auctions. Clean backend swap de-risks the *code*, not the *market*.
- **Not financial or legal advice.** Programmatic memecoin trading carries substantial financial and regulatory risk.

---

**First thing to build:** Phase 1, steps 1–7 — a running paper loop on free PumpPortal discovery streams + DexScreener/Helius enrichment, rule-gate signals, and a realistic fee/slippage paper executor logging to SQLite.
