# KNOWLEDGE.md — sourced memecoin-trading knowledge base

The bot's external-knowledge store: quantified, **cited**, adversarially-verified facts about Solana
pump.fun memecoin trading, so the brain reasons from evidence — not folklore. Built from a deep-research
sweep (5 angles → 20 sources → 96 claims → 3-vote adversarial verification → 8 confirmed findings).
Every number carries its source + confidence; **folklore is explicitly flagged and quarantined** at the
bottom. The headline conclusion **confirms `RESEARCH.md` and the whole codebase thesis: the realistic
ceiling is LOSS-MINIMISATION, not profit.**

Primary sources (credible quant/academic preprints + a transparent compliance report + a named analytics
firm — NOT peer-reviewed; 2024–2025 data windows, point-in-time, drift over time):
- **arXiv 2602.14860** (Lillo et al.) — graduation odds + the velocity predictor.
- **arXiv 2602.13480** (MELT/MemeTrans, Georgia Tech, 40k+ migrated, 30.8M+ pre-migration txns) — post-migration dumps + holder/bundle concentration + the ML loss-reduction ceiling.
- **arXiv 2603.24625** (SolRugDetector) — rug on-chain signatures + the holder-decline trigger.
- **Solidus Labs 2025 Rug Pull Report** — 98.6% collapse, 93% Raydium soft-rug (compliance vendor; mild promotional framing, transparent on-chain methodology).
- **Pine Analytics "Exit Liquidity Machines"** + BeInCrypto — the insider-sniper profitable edge.

---

## 1. The bottom line (HIGH confidence) — this governs everything

**A free-data systematic edge is structurally unachievable on pump.fun; the realistic ceiling is
loss-MINIMISATION, not profit.** [2602.13480; Pine Analytics; BeInCrypto]
- The best ML screen reaches **AUPRC ≈ 0.573**; a top-100 risk-ranked selection **still loses 26.64%**
  (vs ~60.71% for random) — i.e. ML **reduces loss by up to 56.1%, never turns positive.**
- **Our own data confirms the ceiling:** every single free-data signal separates at AUC ~0.5 on our
  realized book (authority, LP, concentration, creator, branding, latency, scorer, image, buyer-intel,
  AND the full Solana Tracker / Axiom risk suite — WL15), and a LightGBM **combining all free-data
  features reaches only val AUC ~0.56–0.58** (stable across 180s/300s horizons, 514 labeled rows; the
  velocity/volume features — vol/mcap, buy/sell, vol_per_min — carry what little signal there is). That
  matches the cited ~0.573 and is
  the **loss-reduction (not profit) ceiling**: the combined model is the last free-data lever and it,
  too, only reduces loss. (Thin/noisy at this n — retrain as data accrues before relying on it live.)
- The **only documented profitable edge** belongs to **deployer-funded, same-block insider snipers**
  (87% of snipes profitable, 15,000+ SOL extracted in ~1 month, 4,600+ sniper wallets, 10,400+ deployers).
  That edge is **speed/advance-notice** — a free-data bot lacking MEV/co-location/speed infra **cannot
  replicate it.** ⇒ Our paper-only, free-data bot's job is rug-AVOIDANCE + honest measurement, exactly as
  the constitution already says. Do **not** promise the user profit.

## 2. Lifecycle & mechanics (HIGH)

- A token launches on a **bonding curve**; it **graduates** to a DEX when the curve completes:
  **~85 SOL raised** (real) on top of **30 SOL virtual** = **115 SOL total reserve**, ≈ **$69,000 market
  cap**. The $69K is a USD approximation of the fixed 85-SOL invariant and **floats with SOL price**
  ($68–73K band). [2602.14860; Bitget]
- **Graduation destination is PumpSwap** (since **March 2025**), not Raydium — keep mental model current.
- **Migration is the single most dangerous moment** — insiders/snipers dump into the new DEX liquidity.

## 3. The odds (HIGH) — the hostile prior, quantified

- **Graduation is rare and declining: ~0.6–1.4%.** 0.63% (4,338 / 655,770, Sep–Oct 2025); 1.4% (Jan
  2025); ~1.1% (Feb 2025); **sub-1% for 4+ straight weeks, lows 0.58–0.60%.** A liquidity-survival proxy
  converges: of 7M+ tokens with ≥5 trades, only ~1.4% retain >$1,000 liquidity. [2602.14860; Defiant; Solidus; TheBlock]
- **Collapse is near-universal: 98.6% of tokens collapse** to worthless shortly after launch; **92.22%**
  of ≥30-swap tokens show ≥1 statistically-flagged dump; **~93% of Raydium pools** show soft-rug traits.
  *(Caveat: these are liquidity-collapse / negative-shock PROXIES — not 98.6% verified deliberate fraud.)* [Solidus; 2602.14860]
- **Post-migration is brutal: >73% of migrated tokens fall below 40% of migration price within 20
  minutes; 60.26% below 20%; only ~5.2% hold at/above migration price.** [2602.13480] ⇒ the case for
  banking principal FAST post-migration (our WL7 take-initial) is evidence-backed.

## 4. The detectable rug signals (HIGH) — what actually separates, and WHEN

- **Holder concentration + bundles are the #1 detectable PRE-migration rug tell** (manifests during the
  launchpad phase, not only after): high-risk tokens hold **17pp more supply in the first 10 buyers** and
  **19pp more in the first 20**; **~36.5% of supply sits in bundled (coordinated multi-address) accounts**;
  high-risk tokens show a **+24% median jump in bundle-revealed holdings.** [2602.13480]
  ⇒ **Directly validates our top-5 concentration + `conc_rise` hold-time signal as the right #1 free tell**,
  and names **bundle/coordinated-wallet detection** as the highest-value missing signal (our BuyerIntel direction).
- **Real-time rug trigger: a ≥73% relative holder DECLINE within 24h** is a usable Phase-2 detector. [2603.24625]
- **Rug on-chain signatures (POST-HOC, descriptive — not forward entry filters):** median lifespan
  **~0.01 days**; **95.69% of DeFi txns on Day-1**; **median 9 holders** (vs 41,026 for legit tokens). [2603.24625]
- **Trading VELOCITY = the single strongest GRADUATION predictor** ("fast accumulation of liquidity
  through few trades," dominating all variables); **bot-dominated early activity LOWERS graduation
  probability**; **conditioning on prolific creators does NOT improve prediction.** [2602.14860]
  ⇒ velocity is a **graduation** signal, **NOT** a profit/rug signal — do not conflate. (Consistent with
  our finding that creator-prolificacy alone is weak; creator reputation may still help rug-avoidance, a separate question.)

## 5. How this maps to the bot (what we already do right, and the gaps)

- ✅ **Survival-first / loss-min ceiling / rug-avoidance** — the literature's exact conclusion.
- ✅ **Concentration (top-5 + rise-while-held) as the #1 tell** — validated as the strongest pre-migration signal.
- ✅ **Bank principal fast post-migration (WL7 take-initial 1.3x, WL2 partial)** — the >73%-dump-in-20-min
  reality backs early principal-recovery.
- ✅ **Banded entry filters (WL10–12: mcap/holders/vol sweet-spots)** — consistent with the "filter to a
  better cohort" practitioner approach (improves selection, never creates a profit edge).
- ⚠️ **Bundle / coordinated-wallet detection** — the literature's named #1 missing signal; our BuyerIntel
  (free-data, learned-from-our-outcomes) is the free approximation; full detection (Jito bundle IDs,
  fund-flow tracing) likely needs richer-than-free data. Open question worth a research spike.
- ❌ **A profit edge** — needs speed/MEV/advance-notice infra (the insider-sniper game) we do not have and
  cannot get on free data + paper-only.

## 5b. Top-trader playbook (named KOLs — e.g. Cupsey, ~$20-25M) — what the best actually DO

These are the repeatable PRINCIPLES top discretionary memecoin traders use (the named-trader specifics
are mostly private; the methods below are consistently reported across them + the practitioner guides).
Crucially, **they CONFIRM what the bot already does** — there is no secret signal, just discipline + speed
+ human judgment:
- **Early entry, disciplined exit BEFORE the hype peaks** — "a booked move beats a round-trip." ⇒ our
  take-initial (WL7) + partial (WL2) + breakeven-trail.
- **The 50/50 rule** — sell ~50% at the target, let ~50% ride as house money. ⇒ our `partial_tp_frac` 0.7
  + take-initial principal-recovery is the same shape (bank most early, free-roll the rest).
- **Small per-coin size** — 1-5% of bankroll per trade. ⇒ our `risk_per_trade_frac` 4% + per-trade caps.
- **Smart-money wallet tracking** — follow proven-PnL wallets; ≥5 smart wallets into the same token = a
  lean; the first ~70 buyers + snipers "predict the pump"; **top-10 holders < 40% = healthy.** ⇒ our
  BuyerIntel (free-data approximation) + concentration veto. Tools they use: GMGN, kolscan.io/leaderboard,
  BullX, BonkBot, Photon — these surface smart-money/insider wallets (data we'd need their API/tape for).
- **Exit when smart money STOPS buying or sends tokens to an exchange** (distribution prep). ⇒ our
  sell-pressure / concentration-rise de-risk is the free-data analogue.
- **Social/narrative monitoring** — Twitter/Reddit/TG velocity for tokens gaining traction. ⇒ the one
  modality we lack on free data (needs a paid X API; social PRESENCE alone is universal/no-signal).
- **What we structurally CANNOT match:** their SPEED (BullX/sniper bots, millisecond fills) and same-block
  insider advantage. That speed/MEV edge — not a better filter — is what separates the profitable few.
⇒ Net: the top-trader playbook validates our exits + sizing + smart-money + banded-filter direction. The
gap is SPEED + paid smart-money/social data + human judgment, not a missing free-data signal.

## 6. FOLKLORE / REFUTED — do NOT use these (failed adversarial verification)

- ✗ **"76% rug rate" (76,469/100,063)** — failed verification (1-2). The defensible figure is the
  98.6% liquidity-collapse PROXY, not a verified 76% fraud rate.
- ✗ **"800M on curve / 200M held back" supply split** — failed (1-2). (Total supply is ~1B but this exact split is unverified.)
- ✗ **"Over 98% of pump.fun tokens are scams" (as a fraud rate)** — failed (0-3). It's a collapse proxy, not a fraud count.
- ✗ **"Snipers exit in <1 min / 85% within 5 min / 90% in 1–2 swaps"** — failed (0-3). The sub-minute
  speed-play specifics are unverified; the insiders' edge is advance-notice, not a published exit-timing rule.
- ✗ **"99.6% of traders never cleared $10K profit"** (Cointelegraph/Dune) — failed (1-2); directional only.

## 7. Open questions (next research / build)

1. Can bundle/coordinated-wallet detection be approximated from the free PumpPortal WS + public RPC, or
   does it need paid data? (The papers used richer bundle/fund-flow datasets.) — the BuyerIntel question.
2. ~~What's the false-positive cost of applying the concentration/bundle filters at ENTRY?~~ **ANSWERED
   (WL15, negative).** We bought the Solana Tracker / Axiom risk data — `score`, `top10`, `bundlers`,
   `dev`, `snipers`, `insiders` — and backfilled it onto our 191 realized mints (`st_separation`). Even
   though the backfill is CURRENT-not-entry-time (look-ahead that should INFLATE separation), **every
   signal lands at AUC ~0.39–0.57, none ≥0.60** — i.e. the pro Axiom filters do NOT separate winners from
   rugs on our cohort. Why: (a) our book is already gate-filtered, so these add no separation WITHIN it;
   (b) our losses are mostly FADES (never-pump bleed), not rugs, and concentration/bundle signals are
   RUG tells; (c) the structural wall (every free-data signal ≈ 0.5 on our set). ⇒ The WL14 veto stays
   `rugged`-only (the clean, rare, explicit flag); no calibrated score/concentration veto is justified.
   The Axiom edge for live traders is speed + human judgment + a pre-filter universe, NOT the filter values.
3. Can the velocity-of-liquidity graduation predictor be repurposed as a rug-avoidance / sizing input?
4. Per-round-trip fee + MEV/slippage drag decomposition (how much of net-negative is fees vs selection).

*Numbers are 2024–2025 point-in-time snapshots and drift; the SOL-denominated ones float with SOL price.
Re-verify before treating any single figure as current.*
