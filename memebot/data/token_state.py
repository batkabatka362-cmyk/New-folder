"""Per-mint rolling state + a registry of all tracked tokens.

This is the in-memory source of truth the filter/scoring layers read from.
It is updated by the feed consumers (new-token, trade, migration events).
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from ..constants import SIDE_BUY, SIDE_SELL
from ..models import MigrationEvent, NewTokenEvent, TradeEvent
from .candles import CandleBuilder
from .indicators import ema, volume_spike_ratio


@dataclass(slots=True)
class _Tick:
    ts: float
    side: str
    sol: float
    tokens: float
    trader: str
    price_sol: float


class TokenState:
    """Rolling market/trade state for a single mint."""

    def __init__(self, mint: str, *, candle_interval_s: int = 5, trade_buffer: int = 600,
                 price_obs_buffer: int = 40) -> None:
        self.mint = mint
        self.symbol = ""
        self.name = ""
        self.uri = ""                  # WL6: off-chain metadata URI (-> image) for the vision scam-scorer
        self.creator = ""
        self.created_ts = time.time()
        self.last_ts = self.created_ts
        self.first_seen_ts = self.created_ts

        self.migrated = False
        self.pool = ""

        # latest bonding-curve snapshot (denominated in SOL)
        self.market_cap_sol = 0.0
        self.v_sol_in_curve = 0.0
        self.v_tokens_in_curve = 0.0
        self.last_price_sol = 0.0

        self.trades: deque[_Tick] = deque(maxlen=trade_buffer)
        self.candles = CandleBuilder(interval_s=candle_interval_s)
        # polled (DexScreener) price observations -> short-term trend / trendline for the AI
        self.price_obs: deque[tuple[float, float]] = deque(maxlen=price_obs_buffer)
        # polled liquidity observations -> liquidity TREND (a pool draining before entry = rug-in-progress)
        self.liquidity_obs: deque[tuple[float, float]] = deque(maxlen=price_obs_buffer)
        # N13: polled rolling-1h BUY COUNT -> buyer-breadth VELOCITY (accelerating vs fading breadth)
        self.buyer_obs: deque[tuple[float, float]] = deque(maxlen=price_obs_buffer)
        self.sell_obs: deque[tuple[float, float]] = deque(maxlen=price_obs_buffer)   # P10b SR5: sell-count velocity

    # ── updates ───────────────────────────────────────────────────────────────
    def apply_new_token(self, ev: NewTokenEvent) -> None:
        self.symbol = ev.symbol or self.symbol
        self.name = ev.name or self.name
        self.uri = ev.uri or self.uri                  # WL6: carry the metadata URI -> image scam-scorer
        self.creator = ev.creator or self.creator
        self.pool = ev.pool or self.pool
        self.market_cap_sol = ev.market_cap_sol or self.market_cap_sol
        self.v_sol_in_curve = ev.v_sol_in_curve or self.v_sol_in_curve
        self.v_tokens_in_curve = ev.v_tokens_in_curve or self.v_tokens_in_curve
        if ev.v_tokens_in_curve > 0 and ev.v_sol_in_curve > 0:
            self.last_price_sol = ev.v_sol_in_curve / ev.v_tokens_in_curve
        self.created_ts = ev.ts
        self.last_ts = ev.ts

    def apply_trade(self, ev: TradeEvent) -> None:
        price = (ev.sol_amount / ev.token_amount) if ev.token_amount > 0 else self.last_price_sol
        self.trades.append(_Tick(ev.ts, ev.side, ev.sol_amount, ev.token_amount, ev.trader, price))
        if price > 0:
            self.last_price_sol = price
            self.candles.add(ev.ts, price, ev.sol_amount)
        if ev.market_cap_sol > 0:
            self.market_cap_sol = ev.market_cap_sol
        self.last_ts = ev.ts

    def apply_migration(self, ev: MigrationEvent) -> None:
        self.migrated = True
        self.pool = ev.pool or self.pool
        self.last_ts = ev.ts

    def record_price(self, price_usd: float, ts: float | None = None) -> None:
        """Append a polled price observation (builds the short-term trend / trendline)."""
        if price_usd > 0:
            self.price_obs.append((time.time() if ts is None else ts, price_usd))

    def record_liquidity(self, liquidity_usd: float, ts: float | None = None) -> None:
        """Append a polled liquidity observation (builds the liquidity TREND — a draining pool is a
        rug-in-progress; the static liquidity LEVEL doesn't separate winners from rugs, the direction
        might)."""
        if liquidity_usd > 0:
            self.liquidity_obs.append((time.time() if ts is None else ts, liquidity_usd))

    def record_buyers(self, buys_h1: float, ts: float | None = None) -> None:
        """N13: append the rolling 1h BUY COUNT (breadth). buys_h1 is a 1-hour INTEGRAL, so its level
        is degenerate as a velocity (buys_h1/60 is just a rescale); the only non-degenerate signal is
        how it CHANGES poll-to-poll — more new buyers than aged out = accelerating breadth."""
        if buys_h1 >= 0:
            self.buyer_obs.append((time.time() if ts is None else ts, float(buys_h1)))

    def buyer_trend(self) -> dict:
        """N13: change in the rolling 1h buy-count across the polled window (late-mean minus early-mean,
        jitter-damped like price/liquidity trend). Positive = buyer breadth ACCELERATING, negative =
        fading. The static level doesn't separate winners from rugs; the velocity MIGHT (research:
        velocity is REFUTED as a standalone predictor — treat as a weak GBM contributor, never a gate)."""
        counts = [b for _, b in self.buyer_obs]
        n = len(counts)
        if n < 3:
            return {"n": n, "growth": 0.0}
        k = max(1, n // 3)
        early, late = sum(counts[:k]) / k, sum(counts[-k:]) / k
        return {"n": n, "growth": round(late - early, 2)}

    def record_sells(self, sells_h1: float, ts: float | None = None) -> None:
        """P10b SR5: append the rolling 1h SELL COUNT (mirror of record_buyers). Like buys_h1 its LEVEL
        is degenerate; the signal is how it CHANGES poll-to-poll (accelerating selling = fade/rug cue)."""
        if sells_h1 >= 0:
            self.sell_obs.append((time.time() if ts is None else ts, float(sells_h1)))

    def sell_trend(self) -> dict:
        """P10b SR5: change in the rolling 1h sell-count across the polled window (late-mean minus
        early-mean, jitter-damped — same shape as buyer_trend). Positive growth = selling ACCELERATING
        (a fade/rug cue). Advisory only: velocity is a weak standalone predictor (research-refuted as a
        gate) — a dataset-only GBM candidate + brain cue, never a deterministic veto."""
        counts = [s for _, s in self.sell_obs]
        n = len(counts)
        if n < 3:
            return {"n": n, "growth": 0.0}
        k = max(1, n // 3)
        early, late = sum(counts[:k]) / k, sum(counts[-k:]) / k
        return {"n": n, "growth": round(late - early, 2)}

    def liquidity_trend(self) -> dict:
        """Direction + % change of the polled liquidity series. 'falling' before we even buy = the
        pool is being pulled (rug). Tail-vs-head mean damps single-poll jitter (like price_trend)."""
        liqs = [liq for _, liq in self.liquidity_obs if liq > 0]
        n = len(liqs)
        if n < 3:
            return {"n": n, "dir": "unknown", "chg_pct": 0.0}
        first, last = liqs[0], liqs[-1]
        chg = (last - first) / first * 100.0 if first > 0 else 0.0
        k = max(1, n // 3)
        early, late = sum(liqs[:k]) / k, sum(liqs[-k:]) / k
        d = "rising" if late > early * 1.02 else ("falling" if late < early * 0.98 else "flat")
        return {"n": n, "dir": d, "chg_pct": round(chg, 1)}

    def price_trend(self) -> dict:
        """Summarize the recent polled price path: direction, % change over the window,
        and how far below the recent high we are. The AI reads this to reason on the
        TRAJECTORY (a trendline), not just a single snapshot."""
        prices = [p for _, p in self.price_obs if p > 0]
        n = len(prices)
        if n < 3:
            return {"n": n, "dir": "unknown", "chg_pct": 0.0, "off_high_pct": 0.0}
        first, last, hi = prices[0], prices[-1], max(prices)
        chg = (last - first) / first * 100.0 if first > 0 else 0.0
        off_high = (hi - last) / hi * 100.0 if hi > 0 else 0.0
        k = max(1, n // 3)                      # tail-vs-head mean damps single-tick noise
        early, late = sum(prices[:k]) / k, sum(prices[-k:]) / k
        d = "rising" if late > early * 1.02 else ("falling" if late < early * 0.98 else "choppy")
        return {"n": n, "dir": d, "chg_pct": round(chg, 1), "off_high_pct": round(off_high, 1)}

    def technicals(self) -> dict:
        """AI4 — structured technical signals off the polled price series: EMA-cross
        (fast vs slow), breakout/breakdown vs the recent range, and distance to the
        nearest support/resistance. Advisory context for the AI, not a hard rule."""
        prices = [p for _, p in self.price_obs if p > 0]
        n = len(prices)
        if n < 5:
            return {"n": n, "ema_signal": "n/a", "breakout": "none", "res_dist_pct": 0.0, "sup_dist_pct": 0.0}
        fast_p = max(2, n // 4)
        slow_p = max(fast_p + 1, n // 2)        # keep slow strictly slower than fast (informative even at n=5)
        fast, slow = ema(prices, fast_p), ema(prices, slow_p)
        ema_signal = "bull" if fast > slow else ("bear" if fast < slow else "flat")
        last, prior = prices[-1], prices[:-1]
        res, sup = max(prior), min(prior)               # recent resistance / support
        breakout = "up" if last > res else ("down" if last < sup else "none")
        res_dist = (res - last) / last * 100.0 if last > 0 else 0.0   # % up to resistance (<=0 once broken out)
        sup_dist = (last - sup) / last * 100.0 if last > 0 else 0.0   # % above support
        return {"n": n, "ema_signal": ema_signal, "breakout": breakout,
                "res_dist_pct": round(res_dist, 1), "sup_dist_pct": round(sup_dist, 1)}

    # ── derived features (windowed) ─────────────────────────────────────────────
    def _recent(self, window_s: float) -> list[_Tick]:
        cutoff = time.time() - window_s
        return [t for t in self.trades if t.ts >= cutoff]

    def volume_sol(self, window_s: float = 60.0) -> float:
        return sum(t.sol for t in self._recent(window_s))

    def buy_sell_counts(self, window_s: float = 60.0) -> tuple[int, int]:
        buys = sells = 0
        for t in self._recent(window_s):
            if t.side == SIDE_BUY:
                buys += 1
            else:
                sells += 1
        return buys, sells

    def buy_sell_ratio(self, window_s: float = 60.0) -> float:
        buys, sells = self.buy_sell_counts(window_s)
        if sells == 0:
            return float(buys) if buys else 0.0
        return buys / sells

    def unique_buyers(self, window_s: float = 60.0) -> int:
        return len({t.trader for t in self._recent(window_s) if t.side == SIDE_BUY})

    # ── G3 tape-derived signals (only when the metered trade stream feeds st.trades) ──────────
    def trader_flow(self, trader: str) -> tuple[float, float]:
        """(buy_sol, sell_sol) for one wallet across the trade buffer."""
        buy = sum(t.sol for t in self.trades if t.trader == trader and t.side == SIDE_BUY)
        sell = sum(t.sol for t in self.trades if t.trader == trader and t.side == SIDE_SELL)
        return buy, sell

    def creator_dump_ratio(self, creator: str) -> float | None:
        """REAL-TIME dev dump: how much of what the CREATOR bought it has since SOLD on the live tape
        (sell_sol / buy_sol). 0 = still holding; >= ~0.5 = dumping; None if we never saw the creator
        buy (can't assess). The user's #1 tell, now observed on the tape, not just a Helius snapshot."""
        if not creator:
            return None
        buy, sell = self.trader_flow(creator)
        if buy <= 0:
            return None
        return min(5.0, sell / buy)        # cap: a creator can sell more than its first buy via re-buys

    def sniper_share(self, top_n: int = 5, min_buys: int = 8) -> float | None:
        """Share of total BUY volume captured by the FIRST `top_n` distinct buyer wallets (the
        snipers who got in first). High = a few early wallets dominate and will dump on the crowd.
        None until at least `min_buys` buys are seen (too thin to judge)."""
        buys = sorted((t for t in self.trades if t.side == SIDE_BUY), key=lambda t: t.ts)
        if len(buys) < min_buys:
            return None
        total = sum(t.sol for t in buys)
        if total <= 0:
            return None
        first: list[str] = []
        seen: set[str] = set()
        for t in buys:
            if t.trader and t.trader not in seen:
                seen.add(t.trader)
                first.append(t.trader)
            if len(first) >= top_n:
                break
        snipers = set(first)
        return sum(t.sol for t in buys if t.trader in snipers) / total

    def bundle_share(self, window_s: float = 2.0, min_cluster: int = 3, min_buys: int = 6) -> float | None:
        """Share of buy volume from COORDINATED BUNDLES — clusters of >= min_cluster DISTINCT wallets
        buying within window_s of each other (same-block-ish snipe/dev bundles that CONCEAL true
        ownership concentration — the research's documented #1 missing signal; ~36.5% of supply is
        bundled on average). NOTE the tape ts is the RECEIVE time, so same-block bursts cluster within
        a small window (a proxy, not exact transaction-bundle membership). None until enough buys."""
        buys = sorted((t for t in self.trades if t.side == SIDE_BUY), key=lambda t: t.ts)
        if len(buys) < min_buys:
            return None
        total = sum(t.sol for t in buys)
        if total <= 0:
            return None
        bundled = 0.0
        i, n = 0, len(buys)
        while i < n:
            j = i
            wallets: set[str] = set()
            while j < n and buys[j].ts - buys[i].ts <= window_s:
                if buys[j].trader:
                    wallets.add(buys[j].trader)
                j += 1
            if len(wallets) >= min_cluster:          # >= min_cluster DISTINCT wallets in one burst
                bundled += sum(t.sol for t in buys[i:j])
            i = j                                    # non-overlapping clusters
        return bundled / total

    def volume_spike(self) -> float:
        # drop the still-OPEN latest bucket: it is partial and would understate the spike.
        # The ratio then compares the last CLOSED bucket to its trailing average.
        return volume_spike_ratio(self.candles.volumes()[:-1])

    def age_s(self) -> float:
        return time.time() - self.created_ts


class TokenRegistry:
    """Holds all tracked tokens; evicts stale ones to bound memory."""

    def __init__(self, max_tokens: int = 5000, ttl_s: float = 3600.0) -> None:
        self.max_tokens = max_tokens
        self.ttl_s = ttl_s
        self._tokens: dict[str, TokenState] = {}

    def __len__(self) -> int:
        return len(self._tokens)

    def get(self, mint: str) -> TokenState | None:
        return self._tokens.get(mint)

    def get_or_create(self, mint: str) -> TokenState:
        st = self._tokens.get(mint)
        if st is None:
            st = TokenState(mint)
            self._tokens[mint] = st
        return st

    def on_new_token(self, ev: NewTokenEvent) -> TokenState:
        st = self.get_or_create(ev.mint)
        st.apply_new_token(ev)
        return st

    def on_trade(self, ev: TradeEvent) -> TokenState | None:
        st = self._tokens.get(ev.mint)
        if st is None:
            return None          # ignore trades for tokens we never discovered
        st.apply_trade(ev)
        return st

    def on_migration(self, ev: MigrationEvent) -> TokenState | None:
        st = self._tokens.get(ev.mint)
        if st is None:
            return None
        st.apply_migration(ev)
        return st

    def active(self, max_age_s: float = 1800.0, limit: int = 120) -> list[TokenState]:
        """Recently-discovered tokens, freshest activity first (scan candidates)."""
        now = time.time()
        items = [s for s in self._tokens.values() if now - s.created_ts <= max_age_s]
        items.sort(key=lambda s: s.last_ts, reverse=True)
        return items[:limit]

    def evict_stale(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        stale = [m for m, s in self._tokens.items() if now - s.last_ts > self.ttl_s]
        for m in stale:
            del self._tokens[m]
        # hard cap: drop oldest-touched beyond capacity
        if len(self._tokens) > self.max_tokens:
            for m, _ in sorted(self._tokens.items(), key=lambda kv: kv[1].last_ts)[
                : len(self._tokens) - self.max_tokens
            ]:
                del self._tokens[m]
        return len(stale)
