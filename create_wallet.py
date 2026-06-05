"""Create a PumpPortal wallet + linked API key (for G3, the real-time data stream).

Run ONCE:  python create_wallet.py   (or double-click create_wallet.bat)

It saves the FULL result (incl. the PRIVATE KEY) to pumpportal_wallet.json (gitignored), and writes
PUMPPORTAL_API_KEY + TRADE_STREAM_ENABLED INTO your .env AUTOMATICALLY — so the secret never has to
be copied by hand or pasted anywhere. It then prints ONLY the wallet ADDRESS for you to fund.
NEVER paste your apiKey/privateKey into a chat. For G3 (data only, trades stay paper) the bot needs
just the apiKey (now in .env) + the wallet funded with >= 0.02 SOL.
"""
import json
import os

import httpx


def _set_env(path: str, key: str, value: str) -> None:
    """Set/replace KEY=value in the .env file, preserving every other line."""
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    out, found = [], False
    for ln in lines:
        if ln.strip().startswith(key + "=") or ln.strip().startswith("# " + key + "="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"{key}={value}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")


resp = httpx.get("https://pumpportal.fun/api/create-wallet", timeout=20.0)
resp.raise_for_status()
data = resp.json()

with open("pumpportal_wallet.json", "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)

pub = data.get("walletPublicKey") or data.get("publicKey") or "(see pumpportal_wallet.json)"
api = data.get("apiKey")

if api:
    _set_env(".env", "PUMPPORTAL_API_KEY", api)
    _set_env(".env", "TRADE_STREAM_ENABLED", "true")
    wrote = "  Wrote PUMPPORTAL_API_KEY + TRADE_STREAM_ENABLED into .env automatically."
else:
    wrote = "  WARNING: no apiKey in the response — check pumpportal_wallet.json."

print("\n  New wallet + key created. Private key saved to pumpportal_wallet.json (KEEP IT SECRET).")
print(wrote)
print("\n  >> FUND THIS WALLET with >= 0.02 SOL (this is the ONLY thing you copy):")
print("     " + str(pub))
print("\n  Then tell the assistant: bolson   (it will restart the bot with G3).\n")
