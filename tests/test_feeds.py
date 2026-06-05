"""Feed parsing tests: PumpPortal normalize() + DexScreener _to_pair()."""
from __future__ import annotations

from memebot.feed.dexscreener import _best, _to_pair
from memebot.feed.helius_rpc import concentration_pct
from memebot.feed.pumpportal_ws import PumpPortalFeed, normalize
from memebot.feed.watchlist import diff_watchlist, select_watchlist
from memebot.models import MigrationEvent, NewTokenEvent, TradeEvent


def test_normalize_new_token():
    ev = normalize({
        "txType": "create", "mint": "M", "name": "Doge", "symbol": "DOGE",
        "traderPublicKey": "C", "pool": "pump", "marketCapSol": 10,
        "vSolInBondingCurve": 5, "vTokensInBondingCurve": 1000,
    })
    assert isinstance(ev, NewTokenEvent)
    assert ev.mint == "M" and ev.symbol == "DOGE" and ev.market_cap_sol == 10
    assert ev.creator == "C"


def test_normalize_trade():
    ev = normalize({"txType": "buy", "mint": "M", "traderPublicKey": "T",
                    "solAmount": 1.5, "tokenAmount": 1000})
    assert isinstance(ev, TradeEvent) and ev.side == "buy" and ev.sol_amount == 1.5


def test_normalize_migration_and_acks():
    assert isinstance(normalize({"txType": "migrate", "mint": "M", "pool": "raydium"}), MigrationEvent)
    assert normalize({"message": "Successfully subscribed"}) is None    # ack
    assert normalize({"txType": "buy"}) is None                          # no mint
    assert normalize({"txType": "weird", "mint": "M"}) is None           # unknown type


def test_dexscreener_to_pair():
    snap = _to_pair({
        "baseToken": {"address": "M", "symbol": "DOGE"},
        "priceUsd": "0.001", "priceNative": "0.0000005",
        "liquidity": {"usd": 20000}, "volume": {"h1": 3000, "h24": 50000},
        "txns": {"h1": {"buys": 30, "sells": 10}},
        "priceChange": {"m5": 5, "h1": 12},
        "marketCap": 90000, "fdv": 100000, "pairCreatedAt": 123,
        "pairAddress": "P", "dexId": "raydium",
    })
    assert snap.mint == "M" and snap.price_usd == 0.001 and snap.price_native == 5e-7
    assert snap.liquidity_usd == 20000 and snap.buys_h1 == 30 and snap.sells_h1 == 10
    assert abs(snap.buy_sell_ratio_h1 - 3.0) < 1e-9
    assert abs(snap.vol_to_mcap_pct - (3000 / 90000 * 100)) < 1e-9
    assert _to_pair({"baseToken": {}}) is None         # no address


def test_dexscreener_best_picks_most_liquid():
    a = _to_pair({"baseToken": {"address": "M"}, "liquidity": {"usd": 100}})
    b = _to_pair({"baseToken": {"address": "M"}, "liquidity": {"usd": 9000}})
    assert _best([a, b]) is b
    assert _best([]) is None


def test_concentration_excludes_vault():
    amounts = [800, 20, 20, 20, 20, 20, 5, 5]   # 800 = pool/curve vault (largest)
    # drop largest from BOTH numerator and denominator: denom = 1000-800 = 200;
    # top5 of the rest = 100 -> 50% of *circulating* (non-vault) supply
    assert abs(concentration_pct(amounts, 1000, 5, True) - 50.0) < 1e-9
    # without dropping -> 800 + 20*4 = 880 / 1000 = 88%
    assert abs(concentration_pct(amounts, 1000, 5, False) - 88.0) < 1e-9
    assert concentration_pct([], 1000) is None       # no holders
    assert concentration_pct([1, 2], 0) is None        # no supply
    assert concentration_pct([100], 1000, 5, True) == 0.0  # only the vault -> nothing circulating
    # docstring example: 1B supply, 800M vault, 150M across top wallets -> 75% of circulating
    assert abs(concentration_pct([800_000_000, 50_000_000, 50_000_000, 30_000_000, 20_000_000],
                                 1_000_000_000) - 75.0) < 1e-6


class _St:
    def __init__(self, mint, ts):
        self.mint, self.last_ts = mint, ts


def test_watchlist_select_and_diff():
    states = [_St("a", 1.0), _St("b", 3.0), _St("c", 2.0)]
    assert select_watchlist(states, 2) == ["b", "c"]   # freshest (highest last_ts) first
    assert select_watchlist(states, 10) == ["b", "c", "a"]
    add, rem = diff_watchlist({"a", "b"}, {"b", "c"})
    assert add == ["c"] and rem == ["a"]               # subscribe new, unsubscribe aged-out


def test_pumpportal_connect_url_and_redact():
    import asyncio
    base = "wss://pumpportal.fun/api/data"
    # no key (free new-token/migration only) -> URL unchanged, nothing to redact
    f0 = PumpPortalFeed(base, asyncio.Queue())
    assert f0._connect_url() == base and f0._redact("err xyz") == "err xyz"
    # metered key attaches as ?api-key= and is scrubbed from any logged error
    f1 = PumpPortalFeed(base, asyncio.Queue(), api_key="SECRET123")
    assert f1._connect_url() == base + "?api-key=SECRET123"
    assert "SECRET123" not in f1._redact("handshake failed for wss://...api-key=SECRET123")
    # a url that already has a query param uses &
    f2 = PumpPortalFeed(base + "?x=1", asyncio.Queue(), api_key="K")
    assert f2._connect_url() == base + "?x=1&api-key=K"
