# RESEARCH — is this approach sound? (2026-06-03)

A deep, multi-source, adversarially-verified literature review of systematic pump.fun memecoin
trading, run to answer one question: **are we doing this right, and is a free-data edge even real?**
21 sources fetched, 104 claims extracted, 25 verified by 3-vote adversarial panels, 11 confirmed.

## The verdict

**Our methodology is SOUND. Our net-negative result is CORRECT and expected, not a bug. A
net-profitable free-data systematic memecoin strategy is NOT demonstrated by any credible source —
the realistic ceiling is LOSS-MINIMISATION (avoiding rugs), not profit.**

### What the evidence confirms (cited, 3-0 unless noted)

1. **The environment is structurally adverse.** Only ~0.5–2% of pump.fun tokens ever graduate
   (0.63% of 655,770 tokens); ~76% of new Solana tokens are rug pulls (76,469 of 100,063); ~92% of
   actively-traded tokens show ≥1 dump event. **Our 24% win-rate reflects reality.**
   *(arXiv 2602.14860, 2603.24625, 2512.11850)*

2. **Systematic trading is structurally net-negative for our approach.** "It is not possible to make
   profits with a buy-and-hold strategy based only on price/vSol." The best ML model only REDUCES
   loss (60% → ~27%), never produces profit — and those studies are FRICTIONLESS, so with our ~9.5%
   round-trip fees the real result is worse. **Our pf ~0.7 is consistent with reality.**
   *(arXiv 2602.14860, MELT 2602.13480)*

3. **Our validation method is exactly the right priority.** Temporal data leakage (training on
   post-event data) is "the most emphasized issue" / dominant flaw in prior rug-detection ML — and
   we explicitly avoid it (pre-entry features, honest PnL, stacked fees, the profit-factor gate).
   *(arXiv 2603.11324 LROO, 2602.21529 TM-RugPull)*

4. **Our AUC ~0.58 is REALISTIC, not a failure.** The "0.98 AUC" rug-detectors elsewhere are
   confounded — EVM (Ethereum/BSC) not Solana, multimodal, tiny hand-labeled sets, classic
   leakage/overfit red flags. Single-feature AUC ~0.58 on leakage-controlled Solana is the honest,
   harder reality. **We are not doing something wrong.** *(arXiv 2603.11324)*

### What got REFUTED (matters for honesty)

- **Top-holder concentration AND sniper-share as STANDALONE predictors FAILED verification (0-3,
  1-2).** Our concentration/sniper signals are weak in isolation — "folklore-adjacent" — and only
  add marginal lift inside a multi-feature model. *(refuted vs arXiv 2602.13480, 2603.11324)*
- "Trading velocity is the single strongest predictor" — refuted (1-2).
- So: keep these signals as weak contributors, do NOT treat any one of them as an edge.

### The #1 missing signal: BUNDLE / coordinated-wallet detection

~36.5% of token supply is held by **bundled (coordinated) accounts that deliberately CONCEAL true
ownership concentration**; clustering them raises measured top-10 concentration by up to **24
points** for high-risk tokens. We have sniper-share but NOT bundle clustering, so our holder metrics
**systematically understate risk.** It was the 2nd-most-important feature group. **Caveat: high-value
*relative to the feature set*, NOT a silver bullet** (marginal AUPRC ~+0.028). *(MELT 2602.13480, 3-0)*

### On going live (G1)

Profit likely requires **speed/infrastructure we do not have** — Jito bundles are a pay-to-win tip
auction, plus co-located RPC + MEV. Only **0.4% of pump.fun wallets** realized ≥$10k profit. Paper ≠
live (slippage, adverse selection, MEV destroy paper edges). *(Jito docs, decrypt, beincrypto)*

---

## The reframed goal (corrected, evidence-backed)

**memebot is NOT a "get-rich" machine. It is a rug-avoidance + honest-measurement + self-learning
research system.** Its real, defensible value:
- **Avoid the 76% of tokens that rug** (loss-minimisation — the documented achievable ceiling).
- **Measure honestly** (the methodology the literature endorses; never fool ourselves).
- **Learn** (reflection, calibration, separation-AUC) so it improves as data accrues.
Net profit on free data is the wrong expectation; if it ever appears, treat it as a bonus, not the
goal — and verify it forward before risking meaningful capital.

## Forward work plan

| # | Build | Why | Status |
|---|---|---|---|
| 1 | **Bundle detection** (`bundle_share` from the G3 tape: same-block coordinated buys) | The research's #1 missing signal; our holder metrics understate risk without it | **building now** |
| 2 | **Smart-money / copy-trade** (track wallets that historically PROFIT on our own tape; "smart money is buying this" signal) | User's idea + the one free-data angle the research left as an OPEN (unrefuted) question | next major build |
| 3 | **Multi-feature model, not single signals** | The evidence is clear: no single feature separates; lift only comes from COMBINING many (the P4 GBM, once enough tick data) | data-gated |
| 4 | **Measure paper-vs-live divergence** before any G1 | Quantify how much "edge" MEV/slippage would destroy, before risking real money | before G1 |

**Open questions the research could not settle (worth our own data answering):**
- Does bundle-detection + our signals push the *multi-feature* AUC materially above ~0.57 after fees?
- Is profitable-wallet-following (copy-trade) a real free-data edge? No study quantified it for
  Solana pump.fun — **our own tape data can answer this** (this is why #2 matters).

---
*All primary sources are arXiv preprints (late-2024 → mid-2026 datasets); graduation/fee figures are
time-variable snapshots. The honest takeaway is robust across all of them: structurally adverse,
loss-minimisation at best, methodology sound.*
