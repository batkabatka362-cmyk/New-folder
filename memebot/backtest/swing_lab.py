"""C (path-C feasibility) — does SWING/MOMENTUM trading of ESTABLISHED, liquid Solana memecoins have an
edge, vs our (structurally-dead) fresh-launch sniping?

The fresh-launch game is a rug lottery we lose on speed. The hypothesis for a DIFFERENT game: trade
LIQUID SURVIVORS (BONK/WIF-tier) on an hours/days horizon, where rug risk is low, speed doesn't matter,
and free OHLCV suffices. This pulls hourly OHLCV (Solana Tracker /chart, cached so re-runs don't re-spend
the key) for a basket and backtests standard swing strategies NET OF A REALISTIC liquid-pair fee, judged
against BUY-AND-HOLD on the same tokens.

  python -m memebot.backtest.swing_lab [--fee 0.01] [--type 1h]

HONEST CAVEATS: (1) survivorship — the basket is today's survivors, so buy-and-hold looks good for reasons
we could NOT have known in advance; the meaningful read is whether an active strategy BEATS buy-and-hold
(a timing edge is survivorship-robust in a way that "it went up" is not). (2) No slippage/impact model
beyond the flat fee. (3) Small basket. So this is a GO/NO-GO feasibility screen, not a deployable backtest:
if active timing can't beat hold on the survivors net of a tiny fee, the swing mode isn't worth building.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os

from ..config import get_settings
from ..feed.solanatracker import SolanaTrackerClient

# Verified liquid, established Solana memecoins (mints confirmed to return /chart data).
BASKET = {
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "POPCAT": "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
    "MEW": "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5",
    "GIGA": "63LfDmNb3MQ8mw9MtZ2To9bEA2M71kZUUGq5tiJxcqj9",
    "PNUT": "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump",
    "FARTCOIN": "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump",
    "TRUMP": "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN",
    # Survivorship stress-test: faded / high-drawdown memecoins (pumped then bled hard). If a strategy's
    # edge HOLDS once these are in the basket, it's robust; if it CRATERS, the edge was survivorship bias
    # (e.g. mean-reversion "buy the dip" catches a falling knife on a dying token).
    "SLERF": "7BgBvyjrZX1YKz4oh9mjb8ZScatkkwb8DzFx7LoiVkM3",
    "WEN": "WENWENvqqNya429ubCdR81ZmD69brwQaaBYY6p3LCpk",
    "MOTHER": "3S8qX1MsMqRbiwKg2cQyx7nis1oHMgaCuc9c4VfvVdPN",
    "MICHI": "5mbK36SZ7J19An8jFochhQS4of8g6BwUjbeCSxBSoWdp",
    "MUMU": "5LafQUrVco6o7KMz42eqVEJ9LW31StPyGjeeu5sKoMtA",
    "HARAMBE": "Fch1oixTPri8zxBnmdCEADoJW2toyFHxqDZacQkwdvSP",
    "CHILLGUY": "Df6yfrKC8kZE3KNkrHERKzAetSxbrWeniQfyJY4Jpump",
}
_CACHE = "swing_cache.json"


async def _fetch(client, mint, type_):
    r = await client._client.get(f"{client.base_url}/chart/{mint}?type={type_}")
    if r.status_code != 200:
        return []
    bars = r.json().get("oclhv", [])
    return [b for b in bars if isinstance(b, dict) and b.get("close")]


async def load_bars(s, type_) -> dict:
    cache = {}
    if os.path.exists(_CACHE):
        try:
            cache = json.load(open(_CACHE))
        except (ValueError, OSError):
            cache = {}
    todo = {sym: m for sym, m in BASKET.items() if cache.get(type_, {}).get(sym) is None}
    if todo:
        async with SolanaTrackerClient(s.solanatracker_api_key, s.solanatracker_base_url) as cl:
            for sym, mint in todo.items():
                bars = await _fetch(cl, mint, type_)
                cache.setdefault(type_, {})[sym] = bars
                await asyncio.sleep(0.35)
        json.dump(cache, open(_CACHE, "w"))
    return cache.get(type_, {})


def _sma(xs, i, n):
    if i + 1 < n:
        return None
    return sum(xs[i - n + 1:i + 1]) / n


def _simulate(bars, decide, fee, warmup, clip=3.0, stop_k=0.0, slippage_bps=0.0):
    """Stateful, no-look-ahead sim: at bar i (data through i known) `decide(closes,highs,lows,i,pos)`
    returns the position 0/1 to HOLD over i->i+1. Equity compounds the realized bar return; a flat
    `fee` round-trip is split half on each position change, plus `slippage_bps` per SIDE (the live
    engine's fill haircut — honest dip-buy cost). `stop_k`>0 mirrors the engine's hard stop: force-exit
    if price falls >= stop_k below the ENTRY price. Returns (equity_mult, n_trades, n_win, trade_rets)."""
    _side_cost = (1.0 - fee / 2.0) * (1.0 - slippage_bps / 10000.0)   # fee + slippage per position change
    closes = [float(b["close"]) for b in bars]
    highs = [float(b.get("high", b["close"])) for b in bars]
    lows = [float(b.get("low", b["close"])) for b in bars]
    eq, pos, n_trades = 1.0, 0, 0
    entry_eq = None
    entry_price = 0.0
    trade_rets = []
    for i in range(warmup, len(closes) - 1):
        new = decide(closes, highs, lows, i, pos)
        if pos == 1 and stop_k > 0 and entry_price > 0 and (entry_price - closes[i]) / entry_price >= stop_k:
            new = 0                                # hard stop overrides the strategy's hold (matches engine)
        if new != pos:
            if new == 1:
                entry_eq = eq                      # capture BEFORE the entry cost so the per-trade return
            eq *= _side_cost                       # includes BOTH sides (matches the live engine's pnl_pct);
            if new == 1:                           # transition cost = fee/2 + slippage, per side
                n_trades += 1
                entry_price = closes[i]
            elif pos == 1 and entry_eq:
                trade_rets.append(eq / entry_eq - 1.0)
            pos = new
        if pos == 1:
            # clip per-bar move to kill OHLCV glitches (a wick-to-near-zero close then "recovery" prints
            # an astronomical fake bar return — the same pricing-glitch class pnl_reconstruct excludes).
            # A liquid established token does not legitimately 3x or -67% in a single 1h bar.
            r = closes[i + 1] / closes[i]
            eq *= max(1.0 / clip, min(clip, r))
    if pos == 1 and entry_eq:                       # close the open trade at the end
        eq *= _side_cost
        trade_rets.append(eq / entry_eq - 1.0)
    n_win = sum(1 for r in trade_rets if r > 0)
    return eq, n_trades, n_win, trade_rets


# ---- strategies: decide(closes, highs, lows, i, pos) -> 0/1 -------------------------------------------
def buy_hold(c, h, l, i, pos):
    return 1


def ma_cross(fast=12, slow=48):
    def d(c, h, l, i, pos):
        f, s = _sma(c, i, fast), _sma(c, i, slow)
        if f is None or s is None:
            return 0
        return 1 if f > s else 0
    return d


def donchian(n=48, m=24):
    def d(c, h, l, i, pos):
        if i < n:
            return pos
        if pos == 0 and c[i] >= max(h[i - n:i]):       # breakout above prior n-high -> enter
            return 1
        if pos == 1 and c[i] <= min(l[i - m:i]):       # breakdown below prior m-low -> exit
            return 0
        return pos
    return d


def mean_rev(window=24, k=0.12, regime_window=0, regime_tol=0.0):
    """Mean-reversion: buy < SMA*(1-k), sell on reversion above SMA. Optional REGIME GATE: if
    regime_window>0, skip the dip-buy when price is more than regime_tol below the LONG SMA (a structural
    downtrend — don't catch a falling knife). regime_tol=0 => only buy dips at/above the long average."""
    def d(c, h, l, i, pos):
        sma = _sma(c, i, window)
        if sma is None:
            return pos
        if pos == 0 and c[i] < sma * (1 - k):
            if regime_window > 0:
                ls = _sma(c, i, regime_window)
                if ls is not None and ls > 0 and c[i] < ls * (1 - regime_tol):
                    return 0                                # structural downtrend -> skip the dip
            return 1
        if pos == 1 and c[i] > sma:
            return 0
        return pos
    return d


def _gmean(xs):
    if not xs:
        return 0.0
    p = 1.0
    for x in xs:
        p *= max(x, 1e-9)
    return p ** (1.0 / len(xs))


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest swing strategies on established memecoins.")
    ap.add_argument("--fee", type=float, default=0.01, help="round-trip fee (liquid-pair realistic ~0.7-1.5%)")
    ap.add_argument("--type", default="1h", help="candle interval (1h/4h/1d)")
    ap.add_argument("--clip", type=float, default=3.0, help="per-bar glitch clip (a real bar rarely exceeds this x); tighten to stress-test")
    ap.add_argument("--sweep", action="store_true", help="param-robustness sweep of mean-reversion (window x k) — overfit check")
    ap.add_argument("--stop", type=float, default=0.0, help="hard stop-loss frac below entry (0=off); applied to the active strategies, not buy_hold")
    ap.add_argument("--slippage", type=float, default=0.0, help="per-side slippage bps (honest dip-buy fill cost)")
    args = ap.parse_args()
    s = get_settings()
    if not (s.solanatracker_enabled and s.solanatracker_api_key):
        print("SOLANATRACKER_ENABLED + key required.")
        return
    data = asyncio.run(load_bars(s, args.type))
    data = {k: v for k, v in data.items() if v and len(v) > 100}
    if not data:
        print("no chart data.")
        return
    if args.sweep:
        # Overfit check: is the mean-reversion edge robust across params, or lucky at window=24/k=0.12?
        print(f"=== MEAN-REVERSION PARAM SWEEP ({args.type}, fee {args.fee*100:.1f}%, clip {args.clip}, "
              f"{len(data)} tokens) ===")
        print(f"  {'window':>7} {'k':>6} {'gmean_x':>8} {'>hold':>6} {'win%':>5}")
        hold = {sym: _simulate(bars, buy_hold, args.fee, 0, clip=args.clip)[0] for sym, bars in data.items()}
        for window in (12, 24, 48):
            for k in (0.08, 0.12, 0.20):
                mults, beat, tr, wn = [], 0, 0, 0
                for sym, bars in data.items():
                    eq, nt, nw, _ = _simulate(bars, mean_rev(window, k), args.fee, window, clip=args.clip)
                    mults.append(eq)
                    tr += nt
                    wn += nw
                    if eq > hold[sym]:
                        beat += 1
                wr = (wn / tr * 100) if tr else 0.0
                print(f"  {window:>7} {k:>6.2f} {_gmean(mults):>8.2f} {beat:>3}/{len(data):<2} {wr:>5.0f}")
        print("\n  Robust if MOST cells are >1.0 gmean AND beat hold on most tokens. If only window=24/k=0.12\n"
              "  works, it's overfit. (The SIGN robustness is what matters, not the magnitude.)")
        return
    strategies = {
        "buy_hold": (buy_hold, 0),
        "ma_cross_12_48": (ma_cross(12, 48), 48),
        "donchian_48_24": (donchian(48, 24), 48),
        "mean_rev_24_18": (mean_rev(24, 0.18), 24),    # the live default (WL17/WL19 best OOS config)
    }
    print(f"=== SWING FEASIBILITY ({args.type}, fee {args.fee*100:.1f}% round-trip, stop {args.stop*100:.0f}%, "
          f"{len(data)} tokens) ===")
    print(f"  {'strategy':16} {'gmean_x':>8} {'mean_x':>7} {'worst':>6} {'>hold':>6} {'trades':>7} {'win%':>5}")
    hold_mult = {}
    for name, (fn, wu) in strategies.items():
        st = 0.0 if name == "buy_hold" else args.stop      # the stop applies to active strategies, not hold
        mults, trades, wins, beat = [], 0, 0, 0
        for sym, bars in data.items():
            eq, nt, nw, _ = _simulate(bars, fn, args.fee, wu, clip=args.clip, stop_k=st,
                                      slippage_bps=args.slippage)
            mults.append(eq)
            trades += nt
            wins += nw
            if name == "buy_hold":
                hold_mult[sym] = eq
            elif eq > hold_mult.get(sym, 0):
                beat += 1
        gm, mm = _gmean(mults), sum(mults) / len(mults)
        wr = (wins / trades * 100) if trades else 0.0
        bh = f"{beat}/{len(data)}" if name != "buy_hold" else "—"
        print(f"  {name:16} {gm:>8.2f} {mm:>7.2f} {min(mults):>6.2f} {bh:>6} {trades:>7} {wr:>5.0f}")
    print("\n  gmean_x = geometric-mean equity multiple across tokens (1.0 = flat). '>hold' = tokens where the\n"
          "  strategy BEAT buy-and-hold. VERDICT: an active strategy is only worth building if it BEATS hold\n"
          "  on most tokens net of fees; if not, the swing edge isn't there either (back to loss-min reality).")


if __name__ == "__main__":
    main()
