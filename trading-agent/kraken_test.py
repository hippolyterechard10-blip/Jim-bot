"""
kraken_test.py — Diagnostic auth Kraken Futures.
Usage (depuis trading-agent/) :
    sudo /opt/jimbot-venv/bin/python kraken_test.py
"""
import json
import os

from dotenv import load_dotenv

load_dotenv()

k = os.getenv("KRAKEN_API_KEY", "")
s = os.getenv("KRAKEN_API_SECRET", "")
print(f"KEY    : len={len(k):<4} start={k[:6]}... end=...{k[-4:]}")
print(f"SECRET : len={len(s):<4} start={s[:6]}... end=...{s[-4:]}")
print(f"PAPER  : {os.getenv('KRAKEN_PAPER', '(unset)')}")
print()

from kraken_broker import KrakenBroker

br = KrakenBroker()
r = br._get("/derivatives/api/v3/accounts")
print(json.dumps(r, indent=2)[:2000])
