#!/usr/bin/env python3
"""Jim Bot watchdog.

Checks that the trading runtime is alive, enforces a single main.py process,
restarts it if needed, and emits a notification through the existing OpenClaw
notification helper.

Paper-only / lightweight / Mac-native.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAIN = ROOT / "main.py"
LOG = ROOT / "watchdog.log"
PIDFILE = ROOT / ".jim_watchdog.pid"
RESTART_GUARD = ROOT / ".jim_watchdog.restart.lock"


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(message: str):
    line = f"{_ts()} {message}"
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _find_main_pids() -> list[int]:
    try:
        out = subprocess.check_output([
            "ps", "-ax", "-o", "pid=,command="
        ], text=True)
    except Exception as e:
        _log(f"[watchdog] ps failed: {e}")
        return []

    pids: list[int] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid_str, cmd = line.split(None, 1)
            pid = int(pid_str)
        except Exception:
            continue
        if "main.py" not in cmd:
            continue
        if str(MAIN) in cmd or "jim-bot/trading-agent" in cmd:
            pids.append(pid)
    return sorted(set(pids))


def _send_alert(text: str):
    try:
        sys.path.insert(0, str(ROOT))
        import openclaw_notify as notify  # type: ignore
        notify._gateway_send(f"🛡 Jim watchdog: {text}")
    except Exception as e:
        _log(f"[watchdog] alert failed: {e}")


def _start_jim() -> int | None:
    if RESTART_GUARD.exists():
        try:
            age = time.time() - RESTART_GUARD.stat().st_mtime
            if age < 60:
                _log("[watchdog] restart suppressed by guard")
                return None
        except Exception:
            pass

    try:
        RESTART_GUARD.write_text(_ts())
    except Exception:
        pass

    # Keep it very simple: start the bot in the same runtime directory.
    log_path = ROOT / "jim-runtime.log"
    try:
        p = subprocess.Popen(
            ["python3", "main.py"],
            cwd=str(ROOT),
            stdout=open(log_path, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return p.pid
    except Exception as e:
        _log(f"[watchdog] restart failed: {e}")
        return None


def _kill_extras(keep_pid: int | None = None):
    pids = _find_main_pids()
    for pid in pids:
        if keep_pid and pid == keep_pid:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            _log(f"[watchdog] terminated duplicate pid={pid}")
        except Exception as e:
            _log(f"[watchdog] terminate pid={pid} failed: {e}")


def main() -> int:
    pids = _find_main_pids()
    if len(pids) > 1:
        _log(f"[watchdog] duplicates detected: {pids}")
        _kill_extras(keep_pid=pids[-1])
        _send_alert(f"duplicates detected and trimmed: {pids}")
        return 0

    if len(pids) == 1 and _pid_running(pids[0]):
        _log(f"[watchdog] alive pid={pids[0]}")
        return 0

    _log("[watchdog] dead — restarting jim")
    new_pid = _start_jim()
    if new_pid:
        _log(f"[watchdog] restarted pid={new_pid}")
        _kill_extras(keep_pid=new_pid)
        _send_alert(f"runtime restarted pid={new_pid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
