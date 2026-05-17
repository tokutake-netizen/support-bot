"""Feature: weekly digest.

Posts a Monday-morning summary to MODERATOR_CHANNEL_ID (or
DIGEST_CHANNEL_ID if set) covering the previous 7 days: tickets opened,
translations performed, top inquiry countries, new members, intent
detections.

State is read from services/digest_store.py, which other cogs feed via
``digest_store.append(...)``.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, time as dtime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    from zoneinfo import ZoneInfo
    JST = ZoneInfo("Asia/Tokyo")
except Exception:  # pragma: no cover - fallback if tz data missing
    JST = timezone.utc

from services import digest_store, storage

log = logging.getLogger(__name__)

LAST_RUN_FILE = "digest_last_run.json"


def _destination_channel_id() -> Optional[int]:
    raw = (os.getenv("DIGEST_CHANNEL_ID") or os.getenv("MODERATOR_CHANNEL_ID") or "").strip()
    return int(raw) if raw.isdigit() else None


def _format_countries(top: list[tuple[str, int]]) -> str:
    if not top:
        return "—"
    return " · ".join(f"{c} ({n})" for c, n in top)


def build_digest_embed(window_start: float, window_end: float, guild_name: str = "") -> discord.Embed:
    """Build the embed shown in moderator-only on Monday morning."""
    events = digest_store.events_in_range(window_start, window_end)
    tickets = digest_store.count_by_type(events, "ticket_open")
    translations = digest_store.count_by_type(events, "translation")
    joins = digest_store.count_by_type(events, "member_join")
    intents = digest_store.count_by_type(events, "intent_detected")
    shipping_quotes = digest_store.count_by_type(events, "shipping_quote")
    top_countries = digest_store.top_payload_keys(events, "shipping_quote", "country", limit=5)
    top_invites = digest_store.top_payload_keys(events, "member_join", "invite_code", limit=5)

    start_dt = datetime.fromtimestamp(window_start, JST)
    end_dt = datetime.fromtimestamp(window_end, JST)
    title = f"📊 Weekly Digest — {start_dt.strftime('%Y-%m-%d')} ~ {end_dt.strftime('%Y-%m-%d')}"
    if guild_name:
        title = f"{guild_name} ・ {title}"

    embed = discord.Embed(title=title, color=0x5865F2)
    embed.add_field(name="🎫 New tickets", value=f"**{tickets}**", inline=True)
    embed.add_field(name="🌐 Translations", value=f"**{translations}**", inline=True)
    embed.add_field(name="👥 New members", value=f"**{joins}**", inline=True)
    embed.add_field(name="📦 Shipping quotes", value=f"**{shipping_quotes}**", inline=True)
    embed.add_field(name="🤖 Intent detected", value=f"**{intents}**", inline=True)
    embed.add_field(name="​", value="​", inline=True)
    embed.add_field(
        name="🌍 Top inquiry countries",
        value=_format_countries(top_countries),
        inline=False,
    )
    if top_invites:
        embed.add_field(
            name="🚪 Top invite sources",
            value=_format_countries(top_invites),
            inline=False,
        )
    embed.set_footer(text=f"Generated {datetime.now(JST).strftime('%Y-%m-%d %H:%M %Z')}")
    return embed


class DigestCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.daily_check.start()

    def cog_unload(self) -> None:
        self.daily_check.cancel()

    @tasks.loop(time=dtime(hour=8, minute=0, tzinfo=JST))
    async def daily_check(self) -> None:
        """Fires every day at 08:00 JST; sends the digest only on Mondays.

        We dedupe via LAST_RUN_FILE so a same-day restart doesn't double-post.
        """
        now = datetime.now(JST)
        if now.weekday() != 0:  # 0 = Monday
            return
        last = storage.load(LAST_RUN_FILE, default={})
        if last.get("date") == now.strftime("%Y-%m-%d"):
            return
        await self._post_digest(now)
        storage.save(LAST_RUN_FILE, {"date": now.strftime("%Y-%m-%d"), "ts": time.time()})

    @daily_check.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _post_digest(self, now: datetime) -> None:
        ch_id = _destination_channel_id()
        if ch_id is None:
            log.warning("digest: no destination channel configured")
            return
        ch = self.bot.get_channel(ch_id)
        if not isinstance(ch, discord.TextChannel):
            log.warning("digest: destination channel %s not text", ch_id)
            return
        window_end = now.timestamp()
        window_start = window_end - 7 * 86400
        guild_name = ch.guild.name if ch.guild else ""
        embed = build_digest_embed(window_start, window_end, guild_name=guild_name)
        try:
            await ch.send(embed=embed)
            log.info("posted weekly digest to #%s", ch.name)
        except discord.HTTPException:
            log.exception("digest post failed")

    # ---- admin command for on-demand preview ----
    digest_group = app_commands.Group(
        name="digest",
        description="Weekly digest tools",
        default_permissions=discord.Permissions(administrator=True),
    )

    @digest_group.command(name="now", description="Preview the weekly digest for the last 7 days")
    async def digest_now(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        window_end = time.time()
        window_start = window_end - 7 * 86400
        guild_name = interaction.guild.name if interaction.guild else ""
        embed = build_digest_embed(window_start, window_end, guild_name=guild_name)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @digest_group.command(name="post", description="Force-post the weekly digest to the configured channel")
    async def digest_post(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._post_digest(datetime.now(JST))
        await interaction.followup.send("✅ Digest posted.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DigestCog(bot))
