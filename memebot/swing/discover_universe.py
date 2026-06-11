"""WL33 — auto-discover candidate swing-universe tokens (the v2 'dynamic top-by-liquidity' upgrade the
curated universe note anticipated).

The forward-proof is calendar-bound: the selective ~18% dip fires rarely on 4h bars, so MORE established
liquid memecoins = proportionally more entry chances = a faster path to the >=40-trade readiness gate.
Hand-entering 44-char mints is error-prone, so this pulls the top Solana pools from GeckoTerminal
(keyless), reads each pool's BASE token (symbol / mint / USD liquidity), drops the non-memecoins
(SOL/stables/blue-chips/LSTs) and anything already in the universe, and prints verified candidates ranked
by liquidity — a paste-ready block for universe.py. Read-only; never trades.

  python -m memebot.swing.discover_universe [--min-liq 300000] [--pages 4] [--top 15]
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from .universe import DEFAULT_UNIVERSE

_GT = "https://api.geckoterminal.com/api/v2"

# Not memecoins — quote assets, stables, blue-chips, liquid-staking + major DEX/L1 tokens. The
# mean-reversion edge is an established-MEMECOIN property; these don't 18%-dip-and-revert the same way.
_EXCLUDE = {
    "SOL", "WSOL", "USDC", "USDT", "USDS", "USDE", "USD1", "PYUSD", "FDUSD", "DAI", "USDG",
    "JUP", "RAY", "PYTH", "JTO", "JLP", "ORCA", "W", "TNSR", "DRIFT", "KMNO", "ZEUS", "CLOUD",
    "MSOL", "JITOSOL", "BSOL", "INF", "HSOL", "JUPSOL", "BNSOL", "LST", "PICOSOL",
    "WBTC", "WETH", "CBBTC", "ZBTC", "BTC", "ETH", "WSTETH", "HYPE", "BERA", "S", "SUI",
}


def _age_days(created: str | None) -> float | None:
    """Pool age in days from an ISO 'pool_created_at' (None if unparseable)."""
    if not created:
        return None
    try:
        dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86_400.0
    except (ValueError, TypeError):
        return None


def _norm(sym: str) -> str:
    return (sym or "").strip().upper()


async def _fetch_pages(client, pages: int) -> list[dict]:
    """Top Solana pools by 24h volume, with base/quote tokens included. Defensive: skips bad pages."""
    rows = []
    for page in range(1, pages + 1):
        try:
            r = await client.get(f"{_GT}/networks/solana/pools",
                                 params={"include": "base_token", "page": page, "sort": "h24_volume_usd_desc"})
            if r.status_code != 200:
                continue
            body = r.json()
        except Exception:  # noqa: BLE001
            continue
        # map included base-token id -> (symbol, address)
        tok = {}
        for inc in body.get("included") or []:
            if inc.get("type") == "token":
                a = inc.get("attributes") or {}
                tok[inc.get("id")] = (_norm(a.get("symbol")), a.get("address") or "")
        for pool in body.get("data") or []:
            attrs = pool.get("attributes") or {}
            rel = (pool.get("relationships") or {}).get("base_token", {}).get("data") or {}
            sym, mint = tok.get(rel.get("id"), ("", ""))
            try:
                liq = float(attrs.get("reserve_in_usd") or 0.0)
            except (TypeError, ValueError):
                liq = 0.0
            if sym and mint:
                rows.append({"symbol": sym, "mint": mint, "liq": liq,
                             "age": _age_days(attrs.get("pool_created_at"))})
        await asyncio.sleep(2.2)   # GeckoTerminal ~30/min — space the page pulls
    return rows


def _rank_candidates(rows: list[dict], min_liq: float, top: int, min_age_days: float) -> list[dict]:
    """Best liquidity per NEW *established* memecoin mint. Excludes quotes/stables/blue-chips, the existing
    universe, pools younger than min_age_days (established only), and DUPE symbols — a symbol carried by >1
    distinct mint in the candidate set is the classic copy-spam scam tell, so the whole symbol is dropped."""
    have_mint = set(DEFAULT_UNIVERSE.values())
    have_sym = {_norm(s) for s in DEFAULT_UNIVERSE}
    sym_mints: dict[str, set] = {}
    for r in rows:
        if r["symbol"] and r["mint"]:
            sym_mints.setdefault(r["symbol"], set()).add(r["mint"])
    dupe_syms = {s for s, mints in sym_mints.items() if len(mints) > 1}
    best: dict[str, dict] = {}
    for r in rows:
        if r["liq"] < min_liq or r["symbol"] in _EXCLUDE or r["symbol"] in dupe_syms:
            continue
        if r["mint"] in have_mint or r["symbol"] in have_sym:
            continue
        if r["age"] is not None and r["age"] < min_age_days:    # too fresh -> not "established"
            continue
        cur = best.get(r["mint"])
        if cur is None or r["liq"] > cur["liq"]:
            best[r["mint"]] = r
    out = sorted(best.values(), key=lambda d: d["liq"], reverse=True)
    return out[:top]


async def run(min_liq: float, pages: int, top: int, min_age_days: float) -> list[dict]:
    import httpx
    async with httpx.AsyncClient(timeout=20.0, trust_env=False,
                                 headers={"accept": "application/json"}) as client:
        rows = await _fetch_pages(client, pages)
    return _rank_candidates(rows, min_liq, top, min_age_days)


def main() -> None:
    ap = argparse.ArgumentParser(description="WL33 swing-universe auto-discovery (read-only)")
    ap.add_argument("--min-liq", type=float, default=300_000.0, help="min pool USD liquidity (default 300k)")
    ap.add_argument("--min-age", type=float, default=90.0, help="min pool age in days (established; default 90)")
    ap.add_argument("--pages", type=int, default=4, help="GeckoTerminal pages to scan (~20 pools each)")
    ap.add_argument("--top", type=int, default=15, help="max candidates to print")
    args = ap.parse_args()
    cands = asyncio.run(run(args.min_liq, args.pages, args.top, args.min_age))
    print(f"=== SWING UNIVERSE DISCOVERY (WL33) — established memecoins >= ${args.min_liq:,.0f} liq, "
          f">= {args.min_age:.0f}d old ===")
    print(f"  universe has {len(DEFAULT_UNIVERSE)} tokens; {len(cands)} fresh candidates:\n")
    print(f"  {'symbol':<14} {'liquidity':>14} {'age_d':>7}   mint")
    for c in cands:
        sym = c["symbol"].encode("ascii", "replace").decode("ascii")
        age = "?" if c.get("age") is None else f"{c['age']:.0f}"
        print(f"  {sym:<14} {c['liq']:>14,.0f} {age:>7}   {c['mint']}")
    if cands:
        print("\n  paste-ready (verify each looks like an established memecoin before adding to universe.py):")
        for c in cands:
            sym = c["symbol"].encode("ascii", "replace").decode("ascii")
            print(f'    "{sym}": "{c["mint"]}",')


if __name__ == "__main__":
    main()
