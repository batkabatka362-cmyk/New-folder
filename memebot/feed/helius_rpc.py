"""Helius (or any Solana) RPC reads for safety vetting.

Phase 1 implements the two MOST decisive, cheapest checks: mint & freeze
authority (a non-null mint authority = print-supply risk; a non-null freeze
authority = honeypot, you can't sell). LP-burn % and holder concentration need
the post-migration LP mint / full holder set and land in Phase 4.

Free Helius tier ~10 req/s; we self-limit below that.
"""
from __future__ import annotations

import httpx

from ..constants import BURN_ADDRESSES
from ..utils.logging import get_logger
from ..utils.rate_limit import AsyncRateLimiter

log = get_logger("feed.helius")


def concentration_pct(amounts: list[float], total_supply: float,
                      topn: int = 5, drop_largest: bool = True) -> float | None:
    """Top-N holder concentration as % of CIRCULATING supply.

    `drop_largest` excludes the single biggest account (assumed AMM pool/curve
    vault) from BOTH the numerator AND the denominator, so the result measures
    the top-N share of non-vault circulating supply. (Dividing by full minted
    supply while excluding the vault from the numerator understates it.)

    ADVISORY ONLY: getTokenLargestAccounts can't tell a pool vault from a dev
    whale, so this is fed to the brain as a signal, not used as a hard veto.
    Returns None if it can't be computed.
    """
    if total_supply <= 0 or not amounts:
        return None
    ranked = sorted((a for a in amounts if a > 0), reverse=True)
    denom = total_supply
    if drop_largest and ranked:
        denom = total_supply - ranked[0]      # exclude the vault from the base too
        ranked = ranked[1:]
    if denom <= 0:
        return None
    top = ranked[:topn]
    if not top:
        return 0.0
    return min(100.0, sum(top) / denom * 100.0)


def largest_funder_cluster(funders: list[str]) -> int:
    """Holder funding-cluster (free-data concealed-concentration tell, RESEARCH.md's #1 missing signal
    via funding rather than the G3 same-block tape): the count of distinct top-holder WALLETS that share
    ONE funding source. >= 2 means several "independent" top holders were funded from the same wallet =
    one entity hiding behind many wallets = concealed concentration / a coordinated dump setup. Ignores
    None/empty funders. Pure + unit-testable."""
    from collections import Counter
    c = Counter(f for f in funders if f)
    return max(c.values()) if c else 0


class HeliusRPC:
    def __init__(self, rpc_url: str, rate_per_sec: float = 8.0) -> None:
        self.rpc_url = rpc_url
        # burst = per-second budget, NOT the per-minute total, so a cold start or
        # a future concurrent caller can't dump hundreds of requests at once.
        self._limiter = AsyncRateLimiter(rate_per_sec * 60.0, burst=max(1, int(rate_per_sec)))
        self._client: httpx.AsyncClient | None = None
        self._id = 0

    async def __aenter__(self) -> "HeliusRPC":
        self._client = httpx.AsyncClient(timeout=10.0)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _rpc(self, method: str, params: list):
        assert self._client is not None, "use 'async with HeliusRPC(...)'"
        await self._limiter.acquire()
        self._id += 1
        try:
            r = await self._client.post(
                self.rpc_url,
                json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params},
            )
            r.raise_for_status()
            body = r.json()
            if "error" in body:
                log.debug("RPC %s error: %s", method, body["error"])
                return None
            return body.get("result")
        except httpx.HTTPStatusError as e:
            # NB: str(e) embeds the full URL, which carries ?api-key=... — log code only.
            log.debug("RPC %s failed: HTTP %s", method, e.response.status_code)
            return None
        except httpx.HTTPError as e:
            log.debug("RPC %s transport error: %s", method, type(e).__name__)
            return None
        # P9-harden: body.json() raises json.JSONDecodeError (a ValueError, not an
        # httpx error) on a 200 with a non-JSON body (provider HTML throttle/CDN page).
        # Without this it escapes _rpc and aborts the rest of the eval safety path
        # (get_authorities/top5_concentration/creator_holding_pct). None == "unknown",
        # the safe default for these advisory reads.
        except ValueError as e:
            log.debug("RPC %s decode error: %s", method, type(e).__name__)
            return None

    async def get_authorities(self, mint: str) -> tuple[bool | None, bool | None]:
        """Returns (mint_revoked, freeze_revoked). None == could not determine.

        A false "safe" is worse than a false "unknown" for a safety gate, so we
        only trust the result when the account actually parses as an SPL mint.
        A wrong address / token *account* / unparseable data -> (None, None).
        """
        res = await self._rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        try:
            data = res["value"]["data"]
            parsed = data["parsed"]
        except (TypeError, KeyError):
            return None, None
        if data.get("program") not in ("spl-token", "spl-token-2022") or parsed.get("type") != "mint":
            return None, None  # not a vettable mint -> unknown, NOT safe
        info = parsed.get("info", {})
        # distinguish an absent key (unknown) from an explicit null (revoked)
        mint_revoked = None if "mintAuthority" not in info else info["mintAuthority"] in (None, "")
        freeze_revoked = None if "freezeAuthority" not in info else info["freezeAuthority"] in (None, "")
        return mint_revoked, freeze_revoked

    async def get_token2022_risk(self, mint: str, *, max_transfer_fee_pct: float = 5.0) -> str | None:
        """A1 (rug-avoidance): inspect a Token-2022 mint's EXTENSIONS for the stealth rug vectors that
        classic mint/freeze checks miss (most bots check only mint/freeze). Returns a short risk reason
        (the caller HARD-FAILS the buy) or None when the token is safe / not Token-2022 / unparseable.
        Free Helius read; degrades to None on any failure. None == 'no detected extension risk'.

        Dangerous extensions:
          transferHook (non-null program)  -> dev runs arbitrary code on transfer (block sells / add tax)
          permanentDelegate (non-null)     -> dev can move/burn YOUR tokens at will
          defaultAccountState == frozen    -> new token accounts frozen by default (honeypot)
          transferFeeConfig fee > cap      -> stealth tax that eats the round-trip
        """
        res = await self._rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        return self._t2022_risk_from_account(res, max_transfer_fee_pct)

    @staticmethod
    def _t2022_risk_from_account(res, max_transfer_fee_pct: float) -> str | None:
        """Pure parse of a getAccountInfo(jsonParsed) result -> dangerous-extension reason or None
        (separated from the RPC so it's unit-testable without a network call)."""
        try:
            data = res["value"]["data"]
            parsed = data["parsed"]
        except (TypeError, KeyError):
            return None
        if data.get("program") != "spl-token-2022" or parsed.get("type") != "mint":
            return None                       # classic SPL token / unparseable -> no Token-2022 ext risk
        exts = parsed.get("info", {}).get("extensions", [])
        if not isinstance(exts, list):
            return None
        null_addr = ("", None, "11111111111111111111111111111111")
        for e in exts:
            if not isinstance(e, dict):
                continue
            name = e.get("extension", "")
            state = e.get("state", {}) or {}
            if name == "transferHook" and state.get("programId") not in null_addr:
                return f"token2022_transfer_hook({str(state.get('programId'))[:8]})"
            if name == "permanentDelegate" and state.get("delegate") not in null_addr:
                return "token2022_permanent_delegate"
            if name == "defaultAccountState" and str(state.get("accountState", "")).lower() == "frozen":
                return "token2022_default_frozen"
            if name == "transferFeeConfig":
                bps = 0
                for k in ("newerTransferFee", "olderTransferFee"):
                    fee = state.get(k) or {}
                    try:
                        bps = max(bps, int(fee.get("transferFeeBasisPoints") or 0))
                    except (TypeError, ValueError):
                        pass
                if bps / 100.0 > max_transfer_fee_pct:
                    return f"token2022_transfer_fee({bps}bps)"
        return None

    async def get_funder(self, wallet: str) -> str | None:
        """N12: the wallet that FUNDED this one (sent the SOL that created/first-funded it). Operators
        rotate to a fresh creator wallet per token but fund them from one treasury, so the funder
        re-links rotated creators. Resolves the OLDEST signature (the funding tx — a fresh creator
        wallet has a short history) then that transfer's source. None on failure / no funder found.
        Off the hot path; the caller MUST cache (a wallet's funder is immutable). Uses standard RPC
        methods (work even on the public RPC, unlike getTokenLargestAccounts)."""
        if not wallet:
            return None
        sigs = await self._rpc("getSignaturesForAddress", [wallet, {"limit": 1000}])
        if not sigs:
            return None
        try:
            oldest = sigs[-1].get("signature")          # newest-first -> the last is the funding tx
        except (AttributeError, IndexError):
            return None
        if not oldest:
            return None
        tx = await self._rpc("getTransaction",
                             [oldest, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        try:
            instrs = tx["transaction"]["message"]["instructions"]
        except (TypeError, KeyError):
            return None
        for ix in instrs:
            if ix.get("program") != "system":
                continue
            parsed = ix.get("parsed") or {}
            info = parsed.get("info") or {}
            typ = parsed.get("type")
            if typ == "transfer" and info.get("destination") == wallet:
                src = info.get("source")
                return src if src and src != wallet else None
            if typ in ("createAccount", "createAccountWithSeed") and info.get("newAccount") == wallet:
                src = info.get("source")
                return src if src and src != wallet else None
        return None

    async def get_token_supply(self, mint: str) -> float | None:
        res = await self._rpc("getTokenSupply", [mint])
        try:
            return float(res["value"]["uiAmount"])
        except (TypeError, KeyError, ValueError):
            return None

    async def get_largest_holders(self, mint: str) -> list[tuple[str, float]]:
        """Top ~20 token accounts (account address, uiAmount). [] on failure."""
        res = await self._rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        try:
            return [(a.get("address", ""), float(a.get("uiAmount") or 0.0)) for a in res["value"]]
        except (TypeError, KeyError):
            return []

    async def get_asset_image(self, mint: str) -> str:
        """WL6 fix: the token's IMAGE url via the Helius DAS `getAsset` (Metaplex metadata, already
        resolved by Helius — no ipfs gateway needed). Robust for ANY mint, unlike the create-event uri
        which is empty when the bot first saw the token post-launch. '' on any failure (never raises)."""
        if not mint:
            return ""
        try:
            res = await self._rpc("getAsset", {"id": mint})
        except Exception:  # noqa: BLE001
            return ""
        try:
            links = (res.get("content") or {}).get("links") or {}
            img = links.get("image") or ""
            if img:
                return str(img)
            files = (res.get("content") or {}).get("files") or []
            for f in files:
                u = (f or {}).get("uri") or (f or {}).get("cdn_uri")
                if u:
                    return str(u)
        except (AttributeError, TypeError):
            return ""
        return ""

    @staticmethod
    def _buyers_from_tx(tx: dict, mint: str) -> list[tuple[str, float]]:
        """Pure: the owners who NET-RECEIVED `mint` tokens in one getTransaction(jsonParsed) result —
        i.e. the BUYERS — with the token amount gained. Diffs pre/postTokenBalances by OWNER wallet (not
        token account), so it survives ATA indirection. [] on any shape error. Split out for unit tests."""
        try:
            meta = tx["meta"]
        except (TypeError, KeyError):
            return []
        if not isinstance(meta, dict):
            return []

        def by_owner(balances) -> dict[str, float]:
            out: dict[str, float] = {}
            for b in balances or []:
                if not isinstance(b, dict) or b.get("mint") != mint:
                    continue
                owner = b.get("owner")
                amt = (b.get("uiTokenAmount") or {}).get("uiAmount")
                if owner and amt is not None:
                    try:
                        out[owner] = out.get(owner, 0.0) + float(amt)
                    except (TypeError, ValueError):
                        continue
            return out

        pre_o, post_o = by_owner(meta.get("preTokenBalances")), by_owner(meta.get("postTokenBalances"))
        buyers = []
        for owner, post_amt in post_o.items():
            gained = post_amt - pre_o.get(owner, 0.0)
            if gained > 1e-9:                       # net token INCREASE = a buy this tx
                buyers.append((owner, gained))
        return buyers

    async def get_recent_buyers(self, mint: str, *, max_sigs: int = 20) -> list[tuple[str, float]]:
        """Recent distinct BUYER wallets of `mint` (owners who net-received tokens), newest-first, from
        on-chain history. The FREE-data path to the smart-money / early-buyer winner signal — PumpPortal's
        trade tape is METERED (0.01 SOL / 10k events), so we reconstruct buyers from standard RPC instead,
        SPENDING NO SOL. EXPENSIVE: ~1 + max_sigs RPC calls, so the caller MUST cost-gate it to a few top
        candidates + cache (a token's early buyers are immutable). [] on any failure (never raises)."""
        if not mint:
            return []
        try:
            sigs = await self._rpc("getSignaturesForAddress", [mint, {"limit": max(1, int(max_sigs))}])
        except Exception:  # noqa: BLE001
            return []
        if not sigs:
            return []
        buyers: list[tuple[str, float]] = []
        seen: set[str] = set()
        for s in sigs:
            sig = s.get("signature") if isinstance(s, dict) else None
            if not sig:
                continue
            try:
                tx = await self._rpc("getTransaction",
                                     [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
            except Exception:  # noqa: BLE001
                continue
            if not tx:
                continue
            for owner, amt in self._buyers_from_tx(tx, mint):
                if owner not in seen:
                    seen.add(owner)
                    buyers.append((owner, amt))
        return buyers

    async def get_owner_balance(self, owner: str, mint: str) -> float | None:
        """Total uiAmount the `owner` WALLET holds of `mint` (sums its token accounts). None on
        failure; 0.0 = the owner holds none (e.g. the creator has SOLD OUT their allocation)."""
        res = await self._rpc("getTokenAccountsByOwner",
                              [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
        try:
            accts = res["value"]
        except (TypeError, KeyError):
            return None
        total = 0.0
        for a in accts:
            try:
                total += float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0.0)
            except (TypeError, KeyError, ValueError):
                continue
        return total

    async def creator_holding_pct(self, mint: str, creator: str, supply: float | None = None) -> float | None:
        """What % of supply the CREATOR wallet still holds. 0 = the creator dumped their allocation
        (a strong rug tell the user flagged — "creator ih huwia zarsan eseh"); a real holding = skin
        in the game. Free Helius reads. Pass `supply` to skip a redundant getTokenSupply call."""
        if not creator:
            return None
        if supply is None:
            supply = await self.get_token_supply(mint)
        if not supply:
            return None
        bal = await self.get_owner_balance(creator, mint)
        if bal is None:
            return None
        return min(100.0, bal / supply * 100.0)

    def supports_largest_accounts(self) -> bool:
        """Whether this RPC serves getTokenLargestAccounts (needed for holder concentration). The
        public `mainnet-beta.solana.com` does NOT — top5_concentration is then always None, silently
        disabling the R2-b scam penalty / holder-quality / R13 re-eval. Any enhanced RPC (Helius
        etc.) does. URL heuristic, NOT a live mint-probe: getTokenLargestAccounts returns empty for
        very large mints (USDC/WSOL/BONK) even on a working RPC, so a fixed-mint probe false-negatives.
        Honest hint for the startup log; the ground truth is whether top5_concentration populates."""
        return "mainnet-beta.solana.com" not in self.rpc_url

    async def top5_concentration(self, mint: str) -> float | None:
        """Top-5 holder concentration % (excludes the largest = pool/curve vault,
        and known burn addresses). Meaningful only for graduated tokens."""
        supply = await self.get_token_supply(mint)
        if not supply:
            return None
        holders = await self.get_largest_holders(mint)
        if not holders:
            return None
        amounts = [amt for addr, amt in holders if addr not in BURN_ADDRESSES]
        return concentration_pct(amounts, supply, topn=5, drop_largest=True)

    async def get_holder_owners(self, mint: str, *, top_n: int = 4, exclude_largest: bool = True) -> list[str]:
        """Resolve the OWNER WALLETS of a mint's top token-account holders (excludes burn addresses and,
        by default, the single largest = pool/curve vault). getTokenLargestAccounts returns token
        ACCOUNTS, not wallets, but funding-source clustering needs the wallets. [] on failure. One
        getAccountInfo per holder, so the caller MUST bound top_n + cache (a token account's owner is
        immutable). Returns deduped owner wallets in holder-size order."""
        holders = await self.get_largest_holders(mint)
        holders = [(a, amt) for a, amt in holders if a and a not in BURN_ADDRESSES and amt > 0]
        if not holders:
            return []
        holders.sort(key=lambda x: x[1], reverse=True)
        if exclude_largest:
            holders = holders[1:]                       # drop the vault (same heuristic as top5_concentration)
        owners: list[str] = []
        seen: set[str] = set()
        for acct, _ in holders[:max(0, top_n)]:
            res = await self._rpc("getAccountInfo", [acct, {"encoding": "jsonParsed"}])
            try:
                owner = res["value"]["data"]["parsed"]["info"]["owner"]
            except (TypeError, KeyError):
                continue
            if owner and owner not in seen:
                seen.add(owner)
                owners.append(owner)
        return owners
