"""Dashboard → bot command queue.

Both the dashboard process and the bot process write into the same
data/dashboard_commands.jsonl file:

  - Dashboard appends `{"ts", "action", "params", "status": "pending"}`
  - Bot polls, picks up "pending" entries, runs them, then flips them
    to `"status": "done"` (or "error") with a `result` field for the
    dashboard to surface.

This is intentionally JSON Lines (newline-delimited) so the bot can
parse incrementally and the dashboard can append cheaply without lock
games. Low admin-action volume = fine.

Allowed actions live in ALLOWED_ACTIONS; the dispatcher in
cogs/command_queue.py refuses anything else, so the "generic command
terminal" in the UI can't be turned into a remote shell.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

QUEUE_FILE = "dashboard_commands.jsonl"

ALLOWED_ACTIONS = {
    "ticket_panel",      # params: {channel_id}
    "digest_now",        # post weekly digest to DIGEST_CHANNEL_ID
    "digest_preview",    # post digest preview to MODERATOR_CHANNEL_ID
    "backup_now",        # post snapshot to BACKUP_CHANNEL_ID
    "tree_sync",         # re-sync slash command tree (no args)
    "set_event",         # params: {name, start_ts, end_ts, ...}
}


def _path(base_dir: Optional[Path] = None) -> Path:
    """Resolve the queue file path.

    The bot's CWD is its deployment dir so the default Path("data")/...
    works. The dashboard talks to many guilds, so it passes a per-guild
    base_dir explicitly.
    """
    base = Path(base_dir) if base_dir else Path("data")
    if base_dir is not None and not str(base).endswith("data"):
        base = base / "data"
    return base / QUEUE_FILE


def enqueue(action: str, params: Optional[dict] = None, base_dir: Optional[Path] = None) -> dict:
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"unknown action: {action}")
    row = {
        "ts": time.time(),
        "action": action,
        "params": params or {},
        "status": "pending",
        "result": None,
    }
    p = _path(base_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def _load_all(base_dir: Optional[Path] = None) -> list[dict]:
    p = _path(base_dir)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _write_all(rows: list[dict], base_dir: Optional[Path] = None) -> None:
    p = _path(base_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""),
        "utf-8",
    )


def claim_pending(base_dir: Optional[Path] = None) -> list[dict]:
    rows = _load_all(base_dir)
    pending = [r for r in rows if r.get("status") == "pending"]
    for r in pending:
        r["status"] = "claimed"
        r["claimed_at"] = time.time()
    _write_all(rows, base_dir)
    return pending


def finalize(ts: float, status: str, result: Optional[str] = None,
             base_dir: Optional[Path] = None) -> None:
    rows = _load_all(base_dir)
    for r in rows:
        if r.get("ts") == ts:
            r["status"] = status
            r["result"] = result
            r["finished_at"] = time.time()
            break
    _write_all(rows, base_dir)


def recent(limit: int = 20, base_dir: Optional[Path] = None) -> list[dict]:
    rows = _load_all(base_dir)
    return list(reversed(rows))[:limit]
