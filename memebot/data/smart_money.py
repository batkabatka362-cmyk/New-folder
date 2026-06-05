"""Smart-money ledger (P8 copy-trade) — track which WALLETS actually PROFIT, then treat a token they
buy as a positive signal.

The research left "profitable-wallet following" as the ONE free-data edge it could not refute (no
study quantified it for Solana) — so we answer it with our OWN tape: ingest every G3 TradeEvent,
keep a per-wallet CROSS-TOKEN realized-PnL book (buy = add cost basis; sell = realize PnL), and call
a wallet "smart" once it has a real profitable track record. The signal is the OPPOSITE axis from
bundle/sniper (those are bad; smart-money buying is good). Persisted to SQLite so reputation accrues
across restarts (a wallet's edge only shows over many trades).

This is observational only — it never executes a copy trade; it just feeds the brain + the dataset.
"""
from __future__ import annotations

from ..constants import SIDE_BUY


class SmartMoney:
    _DUST = 1e-9

    def __init__(self, *, min_closed: int = 3, min_pnl_sol: float = 0.05, win_bar: float = 0.55,
                 max_wallets: int = 50_000, max_open: int = 100_000) -> None:
        self.min_closed = min_closed          # need a real track record before trusting a wallet
        self.min_pnl_sol = min_pnl_sol        # ...and net-positive realized PnL
        self.win_bar = win_bar                # ...and a win rate above this
        self.max_wallets = max_wallets        # memory bound (prune the least-active on overflow)
        self.max_open = max(1, int(max_open))  # P9-harden: bound the open-position book too
        self._open: dict[tuple, dict] = {}    # (wallet, mint) -> {cost, qty} open position
        self._wallet: dict[str, dict] = {}    # wallet -> {pnl, closed, wins}

    # ── ingest ───────────────────────────────────────────────────────────────────
    def on_trade(self, trader: str, mint: str, side: str, sol: float, tokens: float) -> None:
        """One TradeEvent. A buy adds cost basis; a sell realizes PnL against it (FIFO-ish per
        wallet+mint aggregate). Untracked sells (we never saw the buy) are ignored — no cost basis."""
        if not trader or tokens <= 0:
            return
        key = (trader, mint)
        if side == SIDE_BUY:
            pos = self._open.setdefault(key, {"cost": 0.0, "qty": 0.0})
            pos["cost"] += sol
            pos["qty"] += tokens
            if len(self._open) > self.max_open:
                self._prune_open()
            return
        # sell
        pos = self._open.get(key)
        if pos is None or pos["qty"] <= 0:
            return
        portion = min(1.0, tokens / pos["qty"])
        cost_removed = pos["cost"] * portion
        pnl = sol - cost_removed
        pos["cost"] -= cost_removed
        pos["qty"] -= tokens
        if pos["qty"] <= self._DUST:
            del self._open[key]
        w = self._wallet.get(trader)
        if w is None:
            if len(self._wallet) >= self.max_wallets:
                self._prune()
            w = self._wallet.setdefault(trader, {"pnl": 0.0, "closed": 0, "wins": 0})
        w["pnl"] += pnl
        w["closed"] += 1
        if pnl > 0:
            w["wins"] += 1

    def _prune(self) -> None:
        """Drop the least-active 10% so the ledger can't grow unbounded (keep the track records)."""
        if not self._wallet:
            return
        ranked = sorted(self._wallet.items(), key=lambda kv: kv[1]["closed"])
        for wallet, _ in ranked[: max(1, len(ranked) // 10)]:
            del self._wallet[wallet]

    def _prune_open(self) -> None:
        """P9-harden: _open evicts a (wallet, mint) only on a full sell-down to dust, but most
        pump.fun positions are held to zero and never emit the offsetting sell on the tape, so
        without a cap _open grows unbounded once G3 is on. Drop the oldest-inserted 10% (dict
        preserves insertion order; oldest ~= longest-stale ~= most likely a dead token). A later
        sell for an evicted key hits the `pos is None` guard above and is harmlessly ignored as
        untracked; the realized-PnL signal lives in _wallet, which is unaffected."""
        drop = max(1, len(self._open) // 10)
        for key in list(self._open.keys())[:drop]:
            del self._open[key]

    # ── query ────────────────────────────────────────────────────────────────────
    def is_smart(self, wallet: str) -> bool:
        """A wallet with a real profitable track record (enough closes, net-positive, decent win-rate)."""
        w = self._wallet.get(wallet)
        return bool(w and w["closed"] >= self.min_closed and w["pnl"] >= self.min_pnl_sol
                    and (w["wins"] / w["closed"]) >= self.win_bar)

    def smart_count(self) -> int:
        return sum(1 for wal in self._wallet if self.is_smart(wal))

    # ── persistence ──────────────────────────────────────────────────────────────
    def snapshot(self) -> list[tuple]:
        """(wallet, pnl, closed, wins) rows for the wallets with a closed trade — to persist."""
        return [(wal, w["pnl"], w["closed"], w["wins"]) for wal, w in self._wallet.items() if w["closed"] > 0]

    def load(self, rows) -> None:
        """Seed the wallet book from persisted rows (cross-restart reputation)."""
        for wal, pnl, closed, wins in rows:
            if wal:
                self._wallet[wal] = {"pnl": float(pnl), "closed": int(closed), "wins": int(wins)}
