"""WL32 image probe — prove the vision image-scan works on REAL token images, OFF the trading path.

The first concrete step of the user's "scan the image as a fast launch signal" strategy: SEE the WL6
scorer run on real tokens and return VARIED scam-tell answers, before building the accrual/separation
harness that labels each by its 10s+ forward outcome. Uses the already-pulled vision model (default
gemma3:4b — llava is NOT required; gemma3 is multimodal).

  python -m memebot.backtest.image_probe <mint|image-url> [more...]
  python -m memebot.backtest.image_probe --model gemma3:4b <mint>

A MINT is resolved to its image via Helius DAS getAsset (needs HELIUS_RPC_URL in .env); a direct
http(s)/ipfs image URL passes through. Reuses the REAL WL6 prompt/schema/derivation (agent/vision.py)
so this tests the production code path, and additionally PRINTS the 4 booleans the scorer hides.
Read-only, no trading, never touches the bot. Defensive: a row that fails resolves to '-'.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json

from ..agent.vision import _PROMPT, _SCORE_SCHEMA, _score_from_features, resolve_url
from ..config import get_settings

# WL32 finding: the WL6 prompt makes gemma3:4b flag EVERY meme-based logo as "impersonation" (BONK/WIF/
# POPCAT all scored 0.7-0.9), because a weak model reads "references a known thing" as "impersonates a
# known thing". v2 explicitly tells it meme/animal/character references are NORMAL and reserves
# impersonation for DECEPTIVE official-brand spoofing. A/B these to find a prompt that separates legit
# from scam BEFORE building the accrual harness.
_PROMPT_V2 = (
    "You are looking at a Solana MEMECOIN token's logo/art. Memecoins NORMALLY reference memes, animals, "
    "cartoon characters, or pop-culture jokes — that is EXPECTED and is NOT a scam tell by itself. "
    "Answer 4 yes/no questions about the IMAGE only (ignore price):\n"
    "  impersonation: does it DECEPTIVELY pose as an OFFICIAL/VERIFIED entity — e.g. uses a real "
    "company/exchange/project's EXACT logo or a real person's photo TOGETHER WITH 'official'/'verified' "
    "cues so a buyer thinks it IS that official thing? A meme/animal/character reference ALONE is NOT "
    "impersonation — answer no.\n"
    "  bait: does it contain a QR code, a contract/wallet address, a screenshot, or fake "
    "'official'/'verified'/'airdrop' text meant to bait a click or a send?\n"
    "  recycled_generic: is it plain generic stock/clipart or mass-produced low-effort filler?\n"
    "  original_effort: is it original, custom, coherent, effortful art (even if meme-based)?\n"
    "Return JSON with the 4 booleans."
)
_PROMPTS = {"v1": _PROMPT, "v2": _PROMPT_V2}


def _looks_like_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "ipfs://"))


def _safe(s: str) -> str:
    """ASCII-safe a string for the Windows console — token symbols carry arbitrary unicode/emoji (a real
    launch symbol was an emoji that crashed cp1252 stdout)."""
    return (s or "").encode("ascii", "replace").decode("ascii")


def _extract_obj(reply: str):
    """Parse the vision reply into a dict (structured JSON, or the outermost {...} a chatty model wraps)."""
    if not reply:
        return None
    try:
        return json.loads(reply)
    except (ValueError, TypeError):
        i, j = reply.find("{"), reply.rfind("}")
        if i < 0 or j <= i:
            return None
        try:
            return json.loads(reply[i:j + 1])
        except (ValueError, TypeError):
            return None


async def _image_url_for(token: str, helius, gateway: str) -> str:
    """A mint -> its image URL via Helius DAS; a URL passes through (ipfs -> gateway)."""
    if _looks_like_url(token):
        return resolve_url(token, gateway)
    if helius is None:
        return ""
    return await helius.get_asset_image(token)


async def _fetch_b64(client, url: str, gateway: str, max_bytes: int = 5_000_000) -> str:
    r = await client.get(resolve_url(url, gateway))
    r.raise_for_status()
    c = r.content
    if not c or len(c) > max_bytes:
        return ""
    return base64.b64encode(c).decode("ascii")


async def _vision_raw(client, host: str, model: str, b64: str, prompt: str):
    """Vision call (prompt + JSON schema) -> the raw 4-boolean dict (so the probe SHOWS the tells)."""
    payload = {"model": model, "prompt": prompt, "images": [b64], "stream": False,
               "format": _SCORE_SCHEMA, "options": {"temperature": 0.0}}
    r = await client.post(f"{host}/api/generate", json=payload)
    r.raise_for_status()
    body = r.json()
    return _extract_obj(body.get("response", "") if isinstance(body, dict) else "")


async def run(tokens, model, host, gateway, rpc_url, prompt):
    import httpx

    from ..feed.helius_rpc import HeliusRPC
    results = []
    need_helius = any(not _looks_like_url(t) for t in tokens) and bool(rpc_url)
    async with httpx.AsyncClient(timeout=90.0, trust_env=False, follow_redirects=True) as client:
        cm = HeliusRPC(rpc_url) if need_helius else None
        helius = await cm.__aenter__() if cm is not None else None
        try:
            for t in tokens:
                row = {"token": t, "img": "", "obj": None, "score": None}
                try:
                    url = await _image_url_for(t, helius, gateway)
                    row["img"] = url
                    if url:
                        b64 = await _fetch_b64(client, url, gateway)
                        if b64:
                            obj = await _vision_raw(client, host, model, b64, prompt)
                            row["obj"] = obj
                            if obj is not None:
                                row["score"] = _score_from_features(obj)
                except Exception as e:  # noqa: BLE001  — a probe must never raise mid-batch
                    row["err"] = type(e).__name__
                results.append(row)
        finally:
            if cm is not None:
                await cm.__aexit__(None, None, None)
    return results


async def _metadata_image_url(client, uri: str, gateway: str) -> str:
    """A create-event METADATA uri -> the image URL it points at ('' on any failure)."""
    try:
        r = await client.get(resolve_url(uri, gateway))
        r.raise_for_status()
        d = r.json()
        if isinstance(d, dict):
            return resolve_url(str(d.get("image") or ""), gateway)
    except Exception:  # noqa: BLE001
        return ""
    return ""


async def run_live(n, ws_url, model, host, gateway, prompt, timeout_s):
    """Pull the first N fresh pump.fun launches off the free PumpPortal WS, resolve each image, and score
    it — the decisive real-data test (does the model give SPREAD on a live scam+legit mix?) and the first
    half of the accrual harness. Read-only, no trading, no money."""
    import httpx

    from ..feed.pumpportal_ws import PumpPortalFeed
    q: asyncio.Queue = asyncio.Queue(maxsize=4000)
    feed = PumpPortalFeed(ws_url, q, subscribe_new=True, subscribe_migration=False)
    task = asyncio.create_task(feed.run())
    rows, seen = [], set()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    try:
        async with httpx.AsyncClient(timeout=30.0, trust_env=False, follow_redirects=True) as client:
            while len(rows) < n and loop.time() < deadline:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=max(0.1, min(deadline - loop.time(), 10.0)))
                except asyncio.TimeoutError:
                    continue
                mint, uri = getattr(ev, "mint", ""), getattr(ev, "uri", "")
                sym = getattr(ev, "symbol", "") or (mint[:6] if mint else "?")
                if not mint or not uri or mint in seen:
                    continue
                seen.add(mint)
                row = {"token": sym, "img": "", "obj": None, "score": None}
                try:
                    img_url = await _metadata_image_url(client, uri, gateway)
                    row["img"] = img_url
                    if img_url:
                        b64 = await _fetch_b64(client, img_url, gateway)
                        if b64:
                            obj = await _vision_raw(client, host, model, b64, prompt)
                            row["obj"] = obj
                            if obj is not None:
                                row["score"] = _score_from_features(obj)
                except Exception as e:  # noqa: BLE001
                    row["err"] = type(e).__name__
                rows.append(row)
    finally:
        feed.stop()
        task.cancel()
        try:
            await task
        except BaseException:  # noqa: BLE001
            pass
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="WL32 vision image scam-score probe (read-only)")
    ap.add_argument("tokens", nargs="*", help="mints (resolved via Helius DAS) and/or http/ipfs image URLs")
    ap.add_argument("--live", type=int, default=0, metavar="N",
                    help="instead of tokens: pull + score the next N fresh pump.fun launches off the free WS")
    ap.add_argument("--timeout", type=float, default=180.0, help="--live: max seconds to collect (default 180)")
    ap.add_argument("--model", default="gemma3:4b", help="Ollama vision model (default gemma3:4b)")
    ap.add_argument("--host", default="", help="Ollama host (default: settings ollama_host)")
    ap.add_argument("--prompt", default="v2", choices=("v1", "v2"),
                    help="v1 = WL6 production prompt; v2 = WL32 meme-aware impersonation fix (default)")
    args = ap.parse_args()
    s = get_settings()
    host = (args.host or s.image_scam_host or s.ollama_host).rstrip("/")
    gateway = s.image_ipfs_gateway
    rpc_url = getattr(s, "rpc_url", "") or s.helius_rpc_url
    prompt = _PROMPTS[args.prompt]
    if args.live > 0:
        results = asyncio.run(run_live(args.live, s.pumpportal_ws_url, args.model, host, gateway,
                                       prompt, args.timeout))
    elif args.tokens:
        results = asyncio.run(run(args.tokens, args.model, host, gateway, rpc_url, prompt))
    else:
        ap.error("give one or more mints/URLs, or --live N")
    print(f"=== IMAGE SCAM-SCORE PROBE (WL32) — model={args.model} prompt={args.prompt} host={host} ===")
    print("  score = 0.2 +0.5*impersonation +0.3*bait +0.2*recycled -0.2*original_effort (clamped 0..1)")
    print(f"  {'token':<14} {'imp':>4} {'bait':>4} {'recy':>4} {'orig':>4} {'score':>6}  image/err")
    scored = []
    for r in results:
        o = r.get("obj")
        b = (lambda k: ("Y" if (o or {}).get(k) else "n") if o is not None else "-")
        sc = "-" if r.get("score") is None else f"{r['score']:.2f}"
        if r.get("score") is not None:
            scored.append(r["score"])
        tok = _safe(r["token"])
        tok = (tok[:12] + "..") if len(tok) > 14 else tok
        tail = _safe(r.get("err") or (r.get("img") or ""))
        print(f"  {tok:<14} {b('impersonation'):>4} {b('bait'):>4} {b('recycled_generic'):>4} "
              f"{b('original_effort'):>4} {sc:>6}  {tail[:52]}")
    if len(scored) >= 2:
        rng = max(scored) - min(scored)
        print(f"\n  scored {len(scored)}/{len(results)} | score spread {min(scored):.2f}..{max(scored):.2f} "
              f"(range {rng:.2f})")
        print("  >> a non-trivial spread = the model DISCRIMINATES image content (the prerequisite for any")
        print("     image edge). Whether it separates by OUTCOME is the next step (accrual + AUC harness).")


if __name__ == "__main__":
    main()
