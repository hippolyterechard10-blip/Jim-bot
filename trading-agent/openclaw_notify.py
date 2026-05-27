"""
openclaw_notify.py — Notifications vers OpenClaw gateway.
Écrit les events dans /tmp/jimbot-events.jsonl (lu par l'agent jim-bot).
Envoie aussi via HTTP au gateway OpenClaw si configuré.
"""
from __future__ import annotations
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_EVENTS_FILE = Path("/tmp/jimbot-events.jsonl")
_GATEWAY_URL = os.getenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
_GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")
_NOTIFY_CHANNEL = os.getenv("OPENCLAW_NOTIFY_CHANNEL", "")  # ex: "clickclack"


def _write_event(event_type: str, data: dict):
    event = {
        "ts":   datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        **data,
    }
    try:
        with open(_EVENTS_FILE, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as e:
        logger.warning(f"[notify] write_event: {e}")
    return event


def _gateway_send(message: str):
    if not _NOTIFY_CHANNEL or not _GATEWAY_TOKEN:
        return
    try:
        requests.post(
            f"{_GATEWAY_URL}/api/agents/jim-bot/notify",
            headers={"Authorization": f"Bearer {_GATEWAY_TOKEN}"},
            json={"channel": _NOTIFY_CHANNEL, "message": message},
            timeout=5,
        )
    except Exception as e:
        logger.debug(f"[notify] gateway: {e}")


def trade_opened(symbol: str, side: str, qty: float, entry: float,
                 stop: float, target: float, rr: float):
    emoji = "📈" if side == "long" else "📉"
    msg = (
        f"{emoji} TRADE OUVERT\n"
        f"{symbol} {side.upper()} @ ${entry:.4f}\n"
        f"SL ${stop:.4f} | TP ${target:.4f} | R:R {rr:.1f}x\n"
        f"qty={qty}"
    )
    _write_event("trade_open", {
        "symbol": symbol, "side": side, "qty": qty,
        "entry": entry, "stop": stop, "target": target, "rr": rr,
    })
    _gateway_send(msg)
    logger.info(f"[notify] {msg.splitlines()[0]}")


def trade_closed(symbol: str, side: str, entry: float, exit_price: float,
                 pnl: float, reason: str):
    emoji = "💰" if pnl >= 0 else "🔴"
    sign  = "+" if pnl >= 0 else ""
    msg = (
        f"{emoji} TRADE FERMÉ [{reason}]\n"
        f"{symbol} {side.upper()} ${entry:.4f} → ${exit_price:.4f}\n"
        f"P&L: {sign}${pnl:.2f}"
    )
    _write_event("trade_close", {
        "symbol": symbol, "side": side, "entry": entry,
        "exit": exit_price, "pnl": pnl, "reason": reason,
    })
    _gateway_send(msg)
    logger.info(f"[notify] {msg.splitlines()[0]}")


def circuit_breaker(daily_loss: float, capital: float):
    msg = (
        f"🚨 CIRCUIT BREAKER\n"
        f"Perte jour: ${abs(daily_loss):.2f} — pause trading\n"
        f"Capital: ${capital:.2f}"
    )
    _write_event("circuit_breaker", {"daily_loss": daily_loss, "capital": capital})
    _gateway_send(msg)
    logger.warning(f"[notify] {msg}")


def drawdown_alert(drawdown_pct: float, capital: float):
    msg = (
        f"⚠️ DRAWDOWN {drawdown_pct:.1f}%\n"
        f"Capital: ${capital:.2f}"
    )
    _write_event("drawdown_2pct", {"drawdown_pct": drawdown_pct, "capital": capital})
    _gateway_send(msg)
    logger.warning(f"[notify] {msg}")


def bot_started(pid: int):
    _write_event("bot_start", {"pid": pid})
    _gateway_send(f"✅ Jim Bot démarré (PID {pid})")


def bot_stopped():
    _write_event("bot_stop", {})
    _gateway_send("⏹ Jim Bot arrêté")
