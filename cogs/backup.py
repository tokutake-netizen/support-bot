"""Feature: settings snapshot / backup.

Once a day, snapshots:
  - channel + category structure (with permission overwrites)
  - roles
  - the deployment's .env, with secrets redacted

…and posts the JSON as an attachment to BACKUP_CHANNEL_ID
(fallback: MODERATOR_CHANNEL_ID). Useful for "we changed something
and can't remember the old layout" recovery.

This is the reverse of scripts/clone_server.py: that script applies a
captured layout to a new guild; this captures the layout for later.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import time
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    from zoneinfo import ZoneInfo
    JST = ZoneInfo("Asia/Tokyo")
except Exception:  # pragma: no cover
    JST = timezone.utc

from services import storage

log = logging.getLogger(__name__)

LAST_RUN_FILE = "backup_last_run.json"

# Any key matching one of these patterns is redacted before backup.
_SECRET_PATTERNS = (
    re.compile(r"TOKEN", re.I),
    re.compile(r"API_KEY", re.I),
    re.compile(r"SECRET", re.I),
    re.compile(r"PASSWORD", re.I),
    re.compile(r"CLIENT_SECRET", re.I),
)


def _is_secret(key: str) -> bool:
    return any(p.search(key) for p in _SECRET_PATTERNS)


def _destination_channel_id() -> Optional[int]:
    raw = (os.getenv("BACKUP_CHANNEL_ID") or os.getenv("MODERATOR_CHANNEL_ID") or "").strip()
    return int(raw) if raw.isdigit() else None


def _read_env_redacted() -> dict[str, str]:
    """Read the current .env from CWD and return its contents with secrets masked."""
    env_path = Path(".env")
    if not env_path.exists():
        return {}
    out: dict[str, str] = {}
    for line in env_path.read_text("utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        key = k.strip()
        if _is_secret(key) and v:
            out[key] = "<redacted>"
        else:
            out[key] = v.strip()
    return out


def _serialize_overwrites(channel: discord.abc.GuildChannel) -> list[dict]:
    out = []
    for target, ow in channel.overwrites.items():
        allow, deny = ow.pair()
        out.append({
            "type": "role" if isinstance(target, discord.Role) else "member",
            "id": target.id,
            "name": getattr(target, "name", None),
            "allow": allow.value,
            "deny": deny.value,
        })
    return out


def build_snapshot(guild: discord.Guild) -> dict:
    """Capture the guild's channels/roles + redacted env as a single JSON-friendly dict."""
    roles = [
        {
            "id": r.id,
            "name": r.name,
            "color": r.color.value,
            "hoist": r.hoist,
            "mentionable": r.mentionable,
            "permissions": r.permissions.value,
            "position": r.position,
            "managed": r.managed,
        }
        for r in sorted(guild.roles, key=lambda r: r.position)
    ]
    categories = [
        {"id": c.id, "name": c.name, "position": c.position, "overwrites": _serialize_overwrites(c)}
        for c in sorted(guild.categories, key=lambda c: c.position)
    ]
    text_like = []
    for c in sorted(guild.channels, key=lambda c: (c.category_id or 0, c.position)):
        if isinstance(c, discord.CategoryChannel):
            continue
        text_like.append({
            "id": c.id,
            "name": c.name,
            "type": int(c.type),
            "parent_id": c.category_id,
            "position": c.position,
            "topic": getattr(c, "topic", None),
            "nsfw": getattr(c, "nsfw", False),
            "slowmode_delay": getattr(c, "slowmode_delay", 0),
            "overwrites": _serialize_overwrites(c),
        })
    return {
        "captured_at": datetime.now(JST).isoformat(),
        "guild": {"id": guild.id, "name": guild.name, "member_count": guild.member_count},
        "roles": roles,
        "categories": categories,
        "channels": text_like,
        "env": _read_env_redacted(),
    }


class BackupCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.daily_check.start()

    def cog_unload(self) -> None:
        self.daily_check.cancel()

    @tasks.loop(time=dtime(hour=3, minute=0, tzinfo=JST))
    async def daily_check(self) -> None:
        now = datetime.now(JST)
        last = storage.load(LAST_RUN_FILE, default={})
        if last.get("date") == now.strftime("%Y-%m-%d"):
            return
        for guild in self.bot.guilds:
            await self._post_backup(guild, now)
        storage.save(LAST_RUN_FILE, {"date": now.strftime("%Y-%m-%d"), "ts": time.time()})

    @daily_check.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _post_backup(self, guild: discord.Guild, now: datetime) -> None:
        ch_id = _destination_channel_id()
        if ch_id is None:
            log.warning("backup: no destination channel configured")
            return
        ch = guild.get_channel(ch_id)
        if not isinstance(ch, discord.TextChannel):
            log.warning("backup: destination channel %s not text", ch_id)
            return
        try:
            snap = build_snapshot(guild)
        except Exception:
            log.exception("backup: failed to build snapshot")
            return
        data = json.dumps(snap, ensure_ascii=False, indent=2).encode("utf-8")
        filename = f"backup_{now.strftime('%Y-%m-%d')}_{guild.id}.json"
        file = discord.File(io.BytesIO(data), filename=filename)
        msg = (
            f"📦 **Daily backup** ・ {now.strftime('%Y-%m-%d %H:%M %Z')}\n"
            f"・roles: {len(snap['roles'])}\n"
            f"・categories: {len(snap['categories'])}\n"
            f"・channels: {len(snap['channels'])}\n"
            f"・env keys: {len(snap['env'])} (secrets redacted)"
        )
        try:
            await ch.send(msg, file=file)
            log.info("backup posted to #%s for guild %s", ch.name, guild.name)
        except discord.HTTPException:
            log.exception("backup post failed")

    backup_group = app_commands.Group(
        name="backup",
        description="Settings snapshot / backup",
        default_permissions=discord.Permissions(administrator=True),
    )

    @backup_group.command(name="now", description="Post a backup snapshot right now")
    async def backup_now(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("⚠️ Guild-only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._post_backup(interaction.guild, datetime.now(JST))
        await interaction.followup.send("✅ Backup posted to the configured channel.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BackupCog(bot))
