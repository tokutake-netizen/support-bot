"""Polls services/cmd_queue.py and runs each dispatch in the bot process.

This lets the dashboard trigger bot-internal operations (force-run the
digest task, force-sync slash commands, etc.) without restarting the
bot. The allowed action list is short and hard-coded, so dashboard
buttons can't be turned into a remote shell.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands, tasks

try:
    from zoneinfo import ZoneInfo
    JST = ZoneInfo("Asia/Tokyo")
except Exception:
    JST = timezone.utc

from services import cmd_queue

log = logging.getLogger(__name__)


def _staff_role_ids() -> list[int]:
    raw = os.getenv("TICKET_STAFF_ROLE_IDS", "")
    return [int(t.strip()) for t in raw.split(",") if t.strip().isdigit()]


def _ticket_panel_payload() -> dict:
    """Mirror the embed produced by /ticket panel so we can post it via REST."""
    from services.i18n import t, get_ui_lang
    lang = get_ui_lang(None, feature="ticket")
    card_game_id = (os.getenv("CARD_GAME_CHANNEL_ID") or "").strip()
    if card_game_id.isdigit():
        desc = t("ticket.panel_desc", lang, card_game=f"<#{card_game_id}>")
    else:
        desc = t("ticket.panel_desc_no_cardgame", lang)
    bullets = t("ticket.panel_bullets", lang)
    return {
        "embeds": [{
            "title": t("ticket.panel_title", lang),
            "description": f"{desc}\n\n—\n{bullets}",
            "color": 0x5865F2,
            "footer": {"text": t("ticket.panel_footer", lang)},
        }],
        "components": [{
            "type": 1,
            "components": [{
                "type": 2,
                "style": 1,                       # primary
                "label": "🎫 Open ticket",
                "custom_id": "ticket:open",
            }],
        }],
    }


class CommandQueueCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.tick.start()

    def cog_unload(self) -> None:
        self.tick.cancel()

    @tasks.loop(seconds=5)
    async def tick(self) -> None:
        try:
            pending = cmd_queue.claim_pending()
        except Exception:
            log.exception("cmd_queue claim failed")
            return
        for row in pending:
            try:
                result = await self._dispatch(row)
                cmd_queue.finalize(row["ts"], "done", result)
            except Exception as e:
                log.exception("cmd_queue: %s failed", row.get("action"))
                cmd_queue.finalize(row["ts"], "error", str(e))

    @tick.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _dispatch(self, row: dict) -> str:
        action = row.get("action")
        params = row.get("params") or {}

        if action == "ticket_panel":
            return await self._do_ticket_panel(params)
        if action == "welcome_test":
            return await self._do_welcome_test(params)
        if action == "digest_now":
            return await self._do_digest(force_post=True)
        if action == "digest_preview":
            return await self._do_digest(force_post=False)
        if action == "backup_now":
            return await self._do_backup()
        if action == "tree_sync":
            return await self._do_tree_sync()
        if action == "fuel_surcharge_refresh":
            return await self._do_fuel_surcharge_refresh()
        return f"unknown action: {action}"

    # ------------- handlers -------------

    async def _do_ticket_panel(self, params: dict) -> str:
        channel_id = params.get("channel_id")
        if not channel_id:
            return "channel_id required"
        ch = self.bot.get_channel(int(channel_id))
        if not isinstance(ch, discord.TextChannel):
            return f"channel {channel_id} not a text channel"
        payload = _ticket_panel_payload()
        # Re-build an embed/view from the payload via discord.py instead of REST
        embed = discord.Embed(
            title=payload["embeds"][0]["title"],
            description=payload["embeds"][0]["description"],
            color=payload["embeds"][0]["color"],
        )
        embed.set_footer(text=payload["embeds"][0]["footer"]["text"])
        try:
            from cogs.ticket import TicketOpenView  # late import — avoid circular
            view = TicketOpenView()
        except Exception:
            view = None
        msg = await ch.send(embed=embed, view=view)
        return f"posted ticket panel as message {msg.id} in #{ch.name}"

    async def _do_welcome_test(self, params: dict) -> str:
        channel_id = params.get("channel_id")
        user_id = params.get("user_id")
        if not channel_id:
            return "channel_id required"
        ch = self.bot.get_channel(int(channel_id))
        if not isinstance(ch, discord.TextChannel):
            return f"channel {channel_id} not a text channel"

        # Pick a member to render the embed for. Prefer the admin who clicked,
        # else fall back to the guild owner so we always have a real Member.
        member = None
        if user_id and ch.guild:
            try:
                member = ch.guild.get_member(int(user_id)) or await ch.guild.fetch_member(int(user_id))
            except Exception:
                member = None
        if member is None and ch.guild:
            member = ch.guild.owner
        if member is None:
            return "no member available to render welcome embed"

        # Build the embed via the welcome cog's helper, but post to the
        # chosen channel directly so the test doesn't end up in the real
        # WELCOME_CHANNEL_ID by accident.
        from cogs.welcome import _build_embed  # late import to avoid cycle
        embed = _build_embed(member)
        await ch.send(
            content=member.mention,
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return f"posted test welcome for {member} in #{ch.name}"

    async def _do_digest(self, force_post: bool) -> str:
        cog = self.bot.get_cog("DigestCog")
        if cog is None:
            return "DigestCog not loaded"
        if force_post:
            await cog._post_digest(datetime.now(JST))
            return "digest posted"
        # Preview: build the embed and send to MODERATOR_CHANNEL_ID anyway
        from cogs.digest import build_digest_embed, _destination_channel_id
        ch_id = _destination_channel_id()
        if ch_id is None:
            return "no destination channel configured"
        ch = self.bot.get_channel(ch_id)
        if not isinstance(ch, discord.TextChannel):
            return "destination not text channel"
        ts_end = time.time()
        ts_start = ts_end - 7 * 86400
        embed = build_digest_embed(ts_start, ts_end, guild_name=ch.guild.name if ch.guild else "")
        await ch.send(embed=embed)
        return "digest preview posted"

    async def _do_backup(self) -> str:
        cog = self.bot.get_cog("BackupCog")
        if cog is None:
            return "BackupCog not loaded"
        for guild in self.bot.guilds:
            await cog._post_backup(guild, datetime.now(JST))
        return f"backup posted for {len(self.bot.guilds)} guild(s)"

    async def _do_fuel_surcharge_refresh(self) -> str:
        from services import fuel_surcharge
        status = await fuel_surcharge.refresh_all()
        parts = []
        for carrier, info in status.items():
            if info.get("ok"):
                parts.append(f"{carrier}={info['pct']:.1f}%")
            else:
                parts.append(f"{carrier}=FAIL (kept={info.get('kept')})")
        # Also post to the moderator-only channel so the audit trail matches
        # the weekly auto-run.
        cog = self.bot.get_cog("FuelSurchargeCog")
        if cog is not None and hasattr(cog, "_post_status"):
            try:
                await cog._post_status(status)
            except Exception:
                log.exception("fuel_surcharge moderator broadcast failed")
        return " ・ ".join(parts) or "no change"

    async def _do_tree_sync(self) -> str:
        guild_id = os.getenv("GUILD_ID")
        if guild_id and guild_id.isdigit():
            guild = discord.Object(id=int(guild_id))
            self.bot.tree.copy_global_to(guild=guild)
            synced = await self.bot.tree.sync(guild=guild)
        else:
            synced = await self.bot.tree.sync()
        return f"synced {len(synced)} slash command(s)"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CommandQueueCog(bot))
