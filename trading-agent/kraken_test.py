"""
kraken_test.py — Diagnostic auth Kraken Futures, version élargie.

Teste 5 variantes pour isoler la cause :
  1. Endpoint public (connectivité)
  2. .env : whitespace / caractères cachés dans la clé et le secret
  3. Signature Futures avec endpoint "/accounts"       (scheme standard SDK)
  4. Signature Futures avec endpoint "/api/v3/accounts" (scheme alternatif docs)
  5. Signature Spot avec /0/private/Balance            (valide si clé unifiée)

Usage (depuis trading-agent/) :
    sudo /opt/jimbot-venv/bin/python kraken_test.py
"""
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse

import requests
from dotenv import load_dotenv

load_dotenv()

k_raw = os.getenv("KRAKEN_API_KEY", "")
s_raw = os.getenv("KRAKEN_API_SECRET", "")

# ── 2. Audit caractères cachés ────────────────────────────────────────────────
def char_audit(name, v):
    spaces   = v.count(" ")
    tabs     = v.count("\t")
    crlf     = v.count("\r") + v.count("\n")
    trailing = len(v) - len(v.rstrip())
    leading  = len(v) - len(v.lstrip())
    print(f"  {name}: len={len(v)}  spaces={spaces}  tabs={tabs}  "
          f"crlf={crlf}  leading_ws={leading}  trailing_ws={trailing}")

print("── Audit .env (whitespace/chars cachés) ──")
char_audit("KEY   ", k_raw)
char_audit("SECRET", s_raw)
print()

k = k_raw.strip()
s = s_raw.strip()
print(f"Après strip() — KEY    : len={len(k):<4} start={k[:6]}... end=...{k[-4:]}")
print(f"Après strip() — SECRET : len={len(s):<4} start={s[:6]}... end=...{s[-4:]}")
print(f"PAPER={os.getenv('KRAKEN_PAPER', '(unset)')}")
print()

# ── 1. Public ─────────────────────────────────────────────────────────────────
print("── Test 1 — Public (pas d'auth) ──")
try:
    r = requests.get("https://futures.kraken.com/derivatives/api/v3/feeschedules",
                     timeout=10)
    print(f"  status={r.status_code}  result={r.json().get('result')}")
except Exception as e:
    print(f"  erreur : {e}")
print()


def sign_futures(endpoint: str, post: str, nonce: str, secret: str) -> str:
    msg = (post + nonce + endpoint).encode("utf-8")
    sha = hashlib.sha256(msg).digest()
    return base64.b64encode(
        hmac.new(base64.b64decode(secret), sha, hashlib.sha512).digest()
    ).decode()


def try_futures(signed_endpoint: str, label: str):
    try:
        nonce = str(int(time.time() * 1000))
        sig   = sign_futures(signed_endpoint, "", nonce, s)
        r = requests.get(
            "https://futures.kraken.com/derivatives/api/v3/accounts",
            headers={"APIKey": k, "Nonce": nonce, "Authent": sig},
            timeout=10,
        )
        body = r.json()
        err  = body.get("error") or body.get("errors")
        print(f"  {label}: status={r.status_code}  "
              f"result={body.get('result')}  error={err}")
    except Exception as e:
        print(f"  {label}: exception {e}")


# ── 3 + 4. Futures avec 2 variantes d'endpoint signé ──────────────────────────
print("── Test 3 — Futures / scheme SDK (signe '/accounts') ──")
try_futures("/accounts", "A")
print()
print("── Test 4 — Futures / scheme docs (signe '/api/v3/accounts') ──")
try_futures("/api/v3/accounts", "B")
print()

# ── 5. Spot signing ───────────────────────────────────────────────────────────
print("── Test 5 — Spot /0/private/Balance ──")
try:
    urlpath  = "/0/private/Balance"
    nonce    = str(int(time.time() * 1000))
    postdata = urllib.parse.urlencode({"nonce": nonce})
    sha      = hashlib.sha256((nonce + postdata).encode("utf-8")).digest()
    mac      = hmac.new(base64.b64decode(s), urlpath.encode() + sha,
                        hashlib.sha512).digest()
    sig      = base64.b64encode(mac).decode()
    r = requests.post(
        "https://api.kraken.com" + urlpath,
        data=postdata,
        headers={"API-Key": k, "API-Sign": sig,
                 "Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )
    print(f"  status={r.status_code}  body={json.dumps(r.json())[:200]}")
except Exception as e:
    print(f"  exception : {e}")
print()

print("── Lecture ──")
print("• Audit whitespace : si spaces/tabs/crlf != 0 → corrompu, à réécrire")
print("• Test 3 success  → bot OK, juste restart")
print("• Test 4 success  → bug dans mon broker, je corrige la signature")
print("• 3 ET 4 ko, 5 ok → clé Spot, pas Futures, il faut une clé Futures")
print("• 3, 4, 5 tous ko → clé invalide, régénérer")
