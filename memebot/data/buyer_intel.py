"""Buyer intelligence (WL9, the #1 trader winner-signal on FREE data) — learn which WALLETS' early
buys precede WINNERS vs RUGS, from OUR OWN observed outcomes.

Good traders find winners by SMART-MONEY CONFLUENCE: when several wallets with a proven track record buy
the same fresh token, follow them (Nansen/GMGN). The orthodox way needs each wallet's external PnL —
which needs the METERED PumpPortal trade tape (spends SOL). This takes the free-data path instead: we
already see a token's early buyers via `HeliusRPC.get_recent_buyers` (standard RPC, no SOL), so we learn
wallet reputation from OUR labels — a wallet whose early-bought tokens tend to WIN is "smart"; one whose
tokens tend to RUG is a "dumper". Then for a fresh candidate, the count of smart vs dumper early-buyers
is the confluence signal.

Two halves, both pure + bounded (mirrors CreatorHistory/SmartMoney):
  record_buyers(mint, wallets) — stash a token's early buyers, pending its outcome
  on_outcome(mint, won)        — when the token resolves, credit (win) / debit (rug) each of its buyers

LOG-FIRST by design: the confluence counts are features to VALIDATE (does smart-buyer count separate?)
before they ever gate a buy — every on-chain signal we have measured separates at ~0.5, so this earns a
veto only if the data shows it separates.
"""
from __future__ import annotations


class BuyerIntel:
    def __init__(self, *, min_tokens: int = 3, smart_winrate: float = 0.55, dumper_rugrate: float = 0.6,
                 max_wallets: int = 200_000, max_pending: int = 50_000) -> None:
        self.min_tokens = max(1, int(min_tokens))      # a wallet needs this many resolved tokens to earn a label
        self.smart_winrate = smart_winrate             # >= this win-rate (over resolved tokens) = SMART
        self.dumper_rugrate = dumper_rugrate           # >= this rug-rate = DUMPER (an exit-liquidity magnet)
        self.max_wallets = max(1, int(max_wallets))
        self.max_pending = max(1, int(max_pending))
        self._wallet: dict[str, dict] = {}             # wallet -> {tokens, wins, rugs}
        self._pending: dict[str, list[str]] = {}       # mint -> [early-buyer wallets] awaiting an outcome

    def record_buyers(self, mint: str, wallets: list[str]) -> None:
        """Stash a token's (deduped, non-empty) early-buyer wallets until its outcome is known."""
        if not mint:
            return
        seen, clean = set(), []
        for w in wallets or []:
            if w and w not in seen:
                seen.add(w)
                clean.append(w)
        if not clean:
            return
        if mint not in self._pending and len(self._pending) >= self.max_pending:
            self._evict_pending()
        self._pending[mint] = clean

    def on_outcome(self, mint: str, won: bool, *, rugged: bool = False) -> None:
        """Resolve a token: credit (won) / debit-as-rug (rugged) each of its recorded early buyers.
        One-time per mint (the pending entry is consumed). `won` and `rugged` are independent: a fade is
        won=False, rugged=False (no credit, no rug-debit) — only a witnessed collapse is a rug."""
        wallets = self._pending.pop(mint, None)
        if not wallets:
            return
        for w in wallets:
            rec = self._wallet.get(w)
            if rec is None:
                if len(self._wallet) >= self.max_wallets:
                    self._evict_wallets()
                rec = self._wallet.setdefault(w, {"tokens": 0, "wins": 0, "rugs": 0})
            rec["tokens"] += 1
            if won:
                rec["wins"] += 1
            if rugged:
                rec["rugs"] += 1

    def is_smart(self, wallet: str) -> bool:
        r = self._wallet.get(wallet)
        return bool(r and r["tokens"] >= self.min_tokens and r["wins"] / r["tokens"] >= self.smart_winrate)

    def is_dumper(self, wallet: str) -> bool:
        r = self._wallet.get(wallet)
        return bool(r and r["tokens"] >= self.min_tokens and r["rugs"] / r["tokens"] >= self.dumper_rugrate)

    def confluence(self, wallets: list[str]) -> dict:
        """For a fresh candidate's early buyers: how many are known-SMART vs known-DUMPER (deduped).
        smart_count is the research's confluence signal; dumper_count is the inverse (rug-magnet) tell."""
        uniq = {w for w in (wallets or []) if w}
        return {"smart_count": sum(1 for w in uniq if self.is_smart(w)),
                "dumper_count": sum(1 for w in uniq if self.is_dumper(w)),
                "n_buyers": len(uniq)}

    def snapshot(self) -> list[tuple]:
        """[(wallet, tokens, wins, rugs)] for wallets with >=1 resolved token -> cross-restart persistence."""
        return [(w, r["tokens"], r["wins"], r["rugs"]) for w, r in self._wallet.items() if r["tokens"] > 0]

    def load(self, rows) -> None:
        """Seed from a snapshot() (or DB rows of the same shape)."""
        self._wallet = {}
        for row in rows or []:
            try:
                w, tokens, wins, rugs = row[0], int(row[1]), int(row[2]), int(row[3])
            except (TypeError, ValueError, IndexError):
                continue
            if w and tokens > 0:
                self._wallet[w] = {"tokens": tokens, "wins": wins, "rugs": rugs}

    def _evict_wallets(self) -> None:
        """Drop the lowest-resolved-token wallets (a 1-token wallet carries no label yet)."""
        ranked = sorted(self._wallet.items(), key=lambda kv: kv[1]["tokens"])
        for w, _ in ranked[: max(1, len(self._wallet) // 10)]:
            del self._wallet[w]

    def _evict_pending(self) -> None:
        """Bound the pending map — drop the oldest-inserted tokens (dict preserves insertion order)."""
        for mint in list(self._pending.keys())[: max(1, len(self._pending) // 10)]:
            del self._pending[mint]
