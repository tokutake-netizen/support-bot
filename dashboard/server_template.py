"""Capture + replay a guild's category/channel structure.

The dashboard uses this to "clone" an exemplar server (categories +
non-ticket channels) onto a freshly-joined one. Dynamic per-user ticket
channels (anything whose parent_id matches the source guild's
TICKET_CATEGORY_ID) are deliberately excluded — they're created on
demand by cogs.ticket and shouldn't bleed across servers.

The template is stored as a single JSON file. Future enhancement: keep
multiple named templates.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from .discord_api import DiscordREST

log = logging.getLogger(__name__)

# Discord channel types we replicate.
CH_TEXT = 0
CH_VOICE = 2
CH_CATEGORY = 4
CH_NEWS = 5
CH_FORUM = 15


def template_path() -> Path:
    raw = os.environ.get("SERVER_TEMPLATE_FILE")
    if raw:
        return Path(raw)
    root = os.environ.get("DEPLOYMENTS_ROOT")
    if root:
        return Path(root).parent / "server_template.json"
    return Path(__file__).resolve().parent.parent / "server_template.json"


def load_template() -> Optional[dict]:
    p = template_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return None


def save_template(data: dict) -> None:
    p = template_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")


# ---------- capture ----------

async def snapshot_guild(
    bot_token: str, guild_id: str, ticket_category_id: Optional[str] = None
) -> dict:
    """Pull live channels + categories from Discord and return a template dict."""
    rest = DiscordREST(bot_token)
    guild = await rest.get_guild(guild_id)
    channels = await rest.list_channels(guild_id)

    categories = []
    by_parent: dict[Optional[str], list[dict]] = {}
    cat_by_id: dict[str, dict] = {}

    for c in channels:
        if c.get("type") == CH_CATEGORY:
            entry = {
                "name": c.get("name"),
                "position": c.get("position", 0),
                "source_id": c.get("id"),
            }
            categories.append(entry)
            cat_by_id[c["id"]] = entry

    for c in channels:
        if c.get("type") in (CH_TEXT, CH_VOICE, CH_NEWS, CH_FORUM):
            # Skip per-user ticket channels — they live under TICKET_CATEGORY_ID
            # and are created on demand. We DO keep the parent category itself.
            if ticket_category_id and str(c.get("parent_id")) == str(ticket_category_id):
                continue
            by_parent.setdefault(c.get("parent_id"), []).append({
                "name": c.get("name"),
                "type": c.get("type"),
                "position": c.get("position", 0),
                "topic": c.get("topic") or "",
                "nsfw": c.get("nsfw", False),
                "rate_limit_per_user": c.get("rate_limit_per_user", 0),
            })

    categories.sort(key=lambda x: x.get("position", 0))
    for cat in categories:
        cat["channels"] = sorted(
            by_parent.get(cat["source_id"], []), key=lambda x: x.get("position", 0)
        )

    # Channels without a category (rare but possible)
    orphans = sorted(by_parent.get(None, []), key=lambda x: x.get("position", 0))

    return {
        "source_guild_id": guild_id,
        "source_guild_name": (guild or {}).get("name", ""),
        "ticket_category_excluded": str(ticket_category_id or ""),
        "captured_at": _now(),
        "categories": categories,
        "orphan_channels": orphans,
    }


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------- apply ----------

async def apply_template(
    bot_token: str, target_guild_id: str, template: dict, *, dry_run: bool = False
) -> dict:
    """Create categories + channels on the target guild that don't exist yet.

    Matching is by lowercased channel/category name — re-applying is
    idempotent. Returns a summary of what was created / skipped.
    """
    rest = DiscordREST(bot_token)
    existing = await rest.list_channels(target_guild_id)

    existing_cat_by_name = {
        c["name"].lower(): c for c in existing if c.get("type") == CH_CATEGORY
    }
    existing_ch_by_parent_name: dict[tuple[Optional[str], str], dict] = {
        (str(c.get("parent_id")) if c.get("parent_id") else None, c["name"].lower()): c
        for c in existing
        if c.get("type") in (CH_TEXT, CH_VOICE, CH_NEWS, CH_FORUM)
    }

    summary = {
        "created_categories": [],
        "created_channels": [],
        "skipped_categories": [],
        "skipped_channels": [],
        "errors": [],
    }

    import httpx
    DISCORD_API = "https://discord.com/api/v10"
    hdr = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}

    async def _create_channel(payload: dict) -> Optional[dict]:
        if dry_run:
            return {"id": "dry-run", **payload}
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{DISCORD_API}/guilds/{target_guild_id}/channels",
                headers=hdr,
                json=payload,
            )
            if r.status_code in (200, 201):
                return r.json()
            summary["errors"].append(
                f"create {payload.get('name')}: HTTP {r.status_code} {r.text[:120]}"
            )
            return None

    # 1) categories first — collect their new IDs
    new_cat_id_by_source: dict[str, str] = {}
    for cat in template.get("categories", []):
        name = cat["name"]
        existing_match = existing_cat_by_name.get(name.lower())
        if existing_match:
            summary["skipped_categories"].append(name)
            new_cat_id_by_source[cat["source_id"]] = existing_match["id"]
            continue
        result = await _create_channel({
            "name": name,
            "type": CH_CATEGORY,
            "position": cat.get("position", 0),
        })
        if result:
            summary["created_categories"].append(name)
            new_cat_id_by_source[cat["source_id"]] = str(result["id"])

    # 2) channels under each new category
    for cat in template.get("categories", []):
        target_parent = new_cat_id_by_source.get(cat["source_id"])
        for ch in cat.get("channels", []):
            key = (target_parent, ch["name"].lower())
            if key in existing_ch_by_parent_name:
                summary["skipped_channels"].append(f"{cat['name']}/{ch['name']}")
                continue
            payload = {
                "name": ch["name"],
                "type": ch.get("type", CH_TEXT),
                "position": ch.get("position", 0),
                "topic": ch.get("topic") or "",
                "nsfw": ch.get("nsfw", False),
                "rate_limit_per_user": ch.get("rate_limit_per_user", 0),
            }
            if target_parent:
                payload["parent_id"] = target_parent
            result = await _create_channel(payload)
            if result:
                summary["created_channels"].append(f"{cat['name']}/{ch['name']}")

    # 3) orphan channels (no parent category)
    for ch in template.get("orphan_channels", []):
        key = (None, ch["name"].lower())
        if key in existing_ch_by_parent_name:
            summary["skipped_channels"].append(ch["name"])
            continue
        result = await _create_channel({
            "name": ch["name"],
            "type": ch.get("type", CH_TEXT),
            "position": ch.get("position", 0),
            "topic": ch.get("topic") or "",
            "nsfw": ch.get("nsfw", False),
            "rate_limit_per_user": ch.get("rate_limit_per_user", 0),
        })
        if result:
            summary["created_channels"].append(ch["name"])

    return summary
