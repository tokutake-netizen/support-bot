"""Subprocess management for bot instances.

One bot process per guild — started by the dashboard's "Save & Restart"
button. Tracks PIDs in memory; on dashboard restart, prunes orphans by
checking with `os.kill(pid, 0)`.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

from .config_store import deployment_dir

log = logging.getLogger(__name__)

# guild_id -> subprocess.Popen
_PROCS: dict[str, subprocess.Popen] = {}


def _python() -> str:
    return sys.executable or "python3"


def _main_py() -> Path:
    return Path(__file__).resolve().parent.parent / "main.py"


def is_running(guild_id: str) -> bool:
    proc = _PROCS.get(guild_id)
    if proc is None:
        return False
    return proc.poll() is None


def status(guild_id: str) -> dict:
    proc = _PROCS.get(guild_id)
    if proc is None:
        return {"running": False, "pid": None}
    if proc.poll() is None:
        return {"running": True, "pid": proc.pid, "returncode": None}
    return {"running": False, "pid": proc.pid, "returncode": proc.returncode}


def start(guild_id: str) -> dict:
    if is_running(guild_id):
        return status(guild_id)
    d = deployment_dir(guild_id)
    if not (d / ".env").exists():
        raise FileNotFoundError(f"no .env for guild {guild_id}")
    proc = subprocess.Popen(
        [_python(), str(_main_py()), "--env-dir", str(d)],
        cwd=str(d.parent.parent),  # run from repo root
        stdout=subprocess.DEVNULL,  # bot writes its own support_bot.log
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _PROCS[guild_id] = proc
    log.info("started bot for guild %s, pid=%s", guild_id, proc.pid)
    return status(guild_id)


def stop(guild_id: str) -> dict:
    proc = _PROCS.get(guild_id)
    if proc is None or proc.poll() is not None:
        _PROCS.pop(guild_id, None)
        return {"running": False, "pid": None}
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=10)
    except Exception:
        log.exception("graceful stop failed; sending SIGKILL")
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    _PROCS.pop(guild_id, None)
    return {"running": False, "pid": proc.pid, "returncode": proc.returncode}


def restart(guild_id: str) -> dict:
    if is_running(guild_id):
        stop(guild_id)
    return start(guild_id)


def tail_log(guild_id: str, lines: int = 200) -> str:
    log_path = deployment_dir(guild_id) / "support_bot.log"
    if not log_path.exists():
        return "(no log yet)"
    # cheap tail: read last 64 KB
    with open(log_path, "rb") as f:
        try:
            f.seek(-65536, os.SEEK_END)
        except OSError:
            f.seek(0)
        data = f.read().decode("utf-8", errors="replace")
    return "\n".join(data.splitlines()[-lines:])
