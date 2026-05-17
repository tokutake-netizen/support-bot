"""Append-only event log used by digest / backup / health.

Other cogs call ``digest_store.append("ticket_open", {...})`` when
interesting things happen; the digest cog reads back the last 7 days to
build its weekly summary, and /health reads recent counts for status.

The implementation is intentionally dumb (single JSON file, full
rewrite on each append). At expected volumes — at most a few hundred
events per day per guild — that's fine and survives unclean shutdowns.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from services import storage

log = logging.getLogger(__name__)

EVENTS_FILE = "digest_events.json"
RETENTION_DAYS = 31


def append(event_type: str, payload: Optional[dict] = None) -> None:
    """Record an event. Always succeeds (errors are logged, not raised)."""
    try:
        events = storage.load(EVENTS_FILE, default=[])
        if not isinstance(events, list):
            events = []
        events.append({
            "ts": time.time(),
            "type": event_type,
            "payload": payload or {},
        })
        cutoff = time.time() - RETENTION_DAYS * 86400
        events = [e for e in events if e.get("ts", 0) >= cutoff]
        storage.save(EVENTS_FILE, events)
    except Exception:
        log.exception("digest_store.append failed for %s", event_type)


def load_events() -> list[dict]:
    events = storage.load(EVENTS_FILE, default=[])
    return events if isinstance(events, list) else []


def events_in_range(since: float, until: Optional[float] = None) -> list[dict]:
    until = until or (time.time() + 1)
    return [e for e in load_events() if since <= e.get("ts", 0) < until]


def count_by_type(events: list[dict], event_type: str) -> int:
    return sum(1 for e in events if e.get("type") == event_type)


def top_payload_keys(
    events: list[dict], event_type: str, key: str, limit: int = 3
) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for e in events:
        if e.get("type") != event_type:
            continue
        v = (e.get("payload") or {}).get(key)
        if v is None:
            continue
        counts[str(v)] = counts.get(str(v), 0) + 1
    return sorted(counts.items(), key=lambda x: -x[1])[:limit]
