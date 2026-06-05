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
