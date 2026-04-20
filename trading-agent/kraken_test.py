"""
kraken_test.py — Diagnostic auth Kraken Futures.

Teste la clé contre 3 endpoints distincts pour trancher :
  1. Endpoint public (connectivité Kraken)
  2. Endpoint Futures authentifié  /accounts  (scheme Kraken Futures)
  3. Endpoint Spot    authentifié  /0/private/Balance  (scheme Kraken Spot)

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

k = os.getenv("KRAKEN_API_KEY", "")
s = os.getenv("KRAKEN_API_SECRET", "")
print(f"KEY    : len={len(k):<4} start={k[:6]}... end=...{k[-4:]}")
print(f"SECRET : len={len(s):<4} start={s[:6]}... end=...{s[-4:]}")
print(f"PAPER  : {os.getenv('KRAKEN_PAPER', '(unset)')}")
print()

# ── 1. Public (no auth) ───────────────────────────────────────────────────────
print("── Test 1 — Endpoint public Kraken Futures (pas d'auth) ──")
try:
    r = requests.get("https://futures.kraken.com/derivatives/api/v3/feeschedules",
                     timeout=10)
    print(f"  status={r.status_code}  result={r.json().get('result')}")
except Exception as e:
    print(f"  erreur réseau : {e}")
print()

# ── 2. Futures auth ───────────────────────────────────────────────────────────
print("── Test 2 — /accounts avec scheme Kraken Futures ──")
try:
    endpoint = "/accounts"
    nonce    = str(int(time.time() * 1000))
    msg      = (nonce + endpoint).encode("utf-8")
    sha      = hashlib.sha256(msg).digest()
    sig      = base64.b64encode(
        hmac.new(base64.b64decode(s), sha, hashlib.sha512).digest()
    ).decode()
    r = requests.get(
        "https://futures.kraken.com/derivatives/api/v3/accounts",
        headers={"APIKey": k, "Nonce": nonce, "Authent": sig},
        timeout=10,
    )
    print(f"  status={r.status_code}")
    print(f"  body  ={json.dumps(r.json(), indent=2)[:800]}")
except Exception as e:
    print(f"  exception : {e}")
print()

# ── 3. Spot auth ──────────────────────────────────────────────────────────────
print("── Test 3 — /0/private/Balance avec scheme Kraken Spot ──")
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
    print(f"  status={r.status_code}")
    print(f"  body  ={json.dumps(r.json(), indent=2)[:800]}")
except Exception as e:
    print(f"  exception : {e}")
print()

print("── Lecture ──")
print("• Test 1 success       → Kraken accessible")
print("• Test 2 success       → clé Futures valide → bot OK après restart")
print("• Test 3 success       → clé SPOT (pas Futures) → en créer une Futures")
print("• Les 2 et 3 échouent  → clé invalide ou désactivée → régénère")
