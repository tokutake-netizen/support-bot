"""Feature: Invite source tracker.

On every member join, identifies which invite code was used by diffing
cached invite-use counts against a fresh fetch, then logs to a channel.

Lightweight version: no label registration. Just shows the raw invite code,
inviter, and channel — enough to distinguish between e.g. multiple Instagram
invites you've created in Discord's invite settings.

Required permission: bot needs **Manage Server** to fetch invite uses.

Env:
- INVITE_LOG_CHANNEL_ID  : destination for join logs (falls back to MODERATOR_CHANNEL_ID)
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)


def _log_channel_id() -> Optional[int]:
    raw = (os.getenv("INVITE_LOG_CHANNEL_ID") or os.getenv("MODERATOR_CHANNEL_ID") or "").strip()
    return int(raw) if raw.isdigit() else None


def _invite_creator_role_ids() -> list[int]:
    raw = os.getenv("INVITE_CREATOR_ROLE_IDS", "")
    out: list[int] = []
    for tok in raw.split(","):
        t = tok.strip()
        if t.isdigit():
            out.append(int(t))
    return out


class InviteTrackerCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # {guild_id: {invite_code: uses}}
        self._cache: dict[int, dict[str, int]] = {}

    async def _prime_guild(self, guild: discord.Guild) -> None:
        try:
            invites = await guild.invites()
            self._cache[guild.id] = {inv.code: (inv.uses or 0) for inv in invites}
            log.info("Invite cache primed for %s: %d invites", guild.name, len(invites))
        except discord.Forbidden:
            log.warning(
                "Missing 'Manage Server' permission in %s — invite tracking disabled", guild.name
            )
        except discord.HTTPException:
            log.exception("Failed to fetch invites for %s", guild.name)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            await self._prime_guild(guild)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        self._cache.setdefault(invite.guild.id, {})[invite.code] = invite.uses or 0

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        self._cache.get(invite.guild.id, {}).pop(invite.code, None)

    async def _detect_invite(self, guild: discord.Guild) -> Optional[discord.Invite]:
        """Fetch current invites, diff against cache, return the incremented one.

        Also refreshes the cache as a side effect.
        """
        old = self._cache.get(guild.id, {}).copy()
        try:
            invites = await guild.invites()
        except discord.Forbidden:
            log.warning("Cannot fetch invites: missing Manage Server permission")
            return None
        except discord.HTTPException:
            log.exception("Failed to fetch invites")
            return None

        used: Optional[discord.Invite] = None
        for inv in invites:
            if (inv.uses or 0) > old.get(inv.code, 0):
                used = inv
                break
        self._cache[guild.id] = {inv.code: (inv.uses or 0) for inv in invites}
        return used

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        used = await self._detect_invite(guild)

        from services import digest_store
        digest_store.append(
            "member_join",
            {"user_id": member.id, "invite_code": used.code if used else None},
        )

        # Hand off to welcome cog so the welcome embed can include invite attribution
        welcome_cog = self.bot.get_cog("WelcomeCog")
        if welcome_cog is not None and hasattr(welcome_cog, "post_welcome"):
            try:
                await welcome_cog.post_welcome(member, used_invite=used)
            except Exception:
                log.exception("post_welcome dispatch failed")

        # Moderator-side log
        log_ch_id = _log_channel_id()
        if log_ch_id is None:
            return
        log_ch = guild.get_channel(log_ch_id)
        if not isinstance(log_ch, discord.TextChannel):
            return

        if used is not None:
            inviter = used.inviter.mention if used.inviter else "(unknown)"
            inv_channel = used.channel.mention if used.channel else "(unknown)"
            max_part = f"/{used.max_uses}" if used.max_uses else ""
            msg = (
                f"🚪 {member.mention} joined via invite `{used.code}`\n"
                f"  • Inviter: {inviter}\n"
                f"  • Channel: {inv_channel}\n"
                f"  • Uses: {used.uses or 0}{max_part}"
            )
        else:
            msg = (
                f"🚪 {member.mention} joined — invite source unknown "
                f"(possible vanity URL, server-discovery, or screening)"
            )

        try:
            await log_ch.send(msg)
        except discord.HTTPException:
            log.exception("Failed to post invite log")

    invite_group = app_commands.Group(
        name="invite",
        description="Invite source tracker",
    )

    @invite_group.command(name="list", description="List current invites with use counts (admin)")
    async def list_invites(self, interaction: discord.Interaction) -> None:
        from services.channel_guard import ensure_channel_allowed
        if not await ensure_channel_allowed(interaction, "invite"):
            return
        if not (
            isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message("⚠️ Admin only.", ephemeral=True)
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("⚠️ Guild-only command.", ephemeral=True)
            return
        try:
            invites = await guild.invites()
        except discord.Forbidden:
            await interaction.response.send_message(
                "⚠️ Missing **Manage Server** permission — bot can't read invite list.",
                ephemeral=True,
            )
            return

        if not invites:
            await interaction.response.send_message("No active invites.", ephemeral=True)
            return

        invites.sort(key=lambda i: (i.uses or 0), reverse=True)
        lines = ["📋 **Invites (sorted by uses)**"]
        for inv in invites[:25]:
            inviter = inv.inviter.name if inv.inviter else "?"
            ch = f"<#{inv.channel.id}>" if inv.channel else "?"
            max_part = f"/{inv.max_uses}" if inv.max_uses else ""
            lines.append(
                f"・`{inv.code}` — **{inv.uses or 0}**{max_part} uses — "
                f"channel {ch} — by **{inviter}**"
            )
        if len(invites) > 25:
            lines.append(f"_…and {len(invites) - 25} more_")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @invite_group.command(
        name="create",
        description="Generate an invite link (admin or whitelisted role)",
    )
    @app_commands.describe(
        channel="Channel for the invite (default: current channel)",
        max_uses="Max uses, 0 = unlimited (default 0)",
        max_age_hours="Hours until expiry, 0 = never (default 0)",
    )
    async def create(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        max_uses: int = 0,
        max_age_hours: int = 0,
    ) -> None:
        from services.channel_guard import ensure_channel_allowed
        if not await ensure_channel_allowed(interaction, "invite"):
            return
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("⚠️ Guild-only command.", ephemeral=True)
            return

        allowed_roles = _invite_creator_role_ids()
        is_admin = member.guild_permissions.administrator
        has_role = any(r.id in allowed_roles for r in member.roles) if allowed_roles else False
        if not (is_admin or has_role):
            await interaction.response.send_message(
                "⚠️ You don't have permission to generate invites. "
                "Ask an admin to grant you the invite-creator role.",
                ephemeral=True,
            )
            return

        target: Optional[discord.abc.GuildChannel] = channel or interaction.channel  # type: ignore[assignment]
        if not isinstance(target, (discord.TextChannel, discord.VoiceChannel)):
            await interaction.response.send_message(
                "⚠️ Pick a text or voice channel.", ephemeral=True
            )
            return

        if max_uses < 0 or max_uses > 100:
            await interaction.response.send_message(
                "⚠️ `max_uses` must be 0–100.", ephemeral=True
            )
            return
        if max_age_hours < 0 or max_age_hours > 24 * 30:
            await interaction.response.send_message(
                "⚠️ `max_age_hours` must be 0–720 (30 days).", ephemeral=True
            )
            return

        try:
            inv = await target.create_invite(
                max_uses=max_uses,
                max_age=max_age_hours * 3600,
                unique=True,
                reason=f"/invite create by {member} ({member.id})",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "⚠️ Bot is missing **Create Invite** permission on that channel.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            await interaction.response.send_message(f"⚠️ Failed: {e}", ephemeral=True)
            return

        uses_str = "unlimited" if inv.max_uses == 0 else str(inv.max_uses)
        age_str = "never" if inv.max_age == 0 else f"{inv.max_age // 3600}h"
        await interaction.response.send_message(
            f"✅ Invite created\n"
            f"{inv.url}\n"
            f"  • Channel: {target.mention}\n"
            f"  • Code: `{inv.code}`\n"
            f"  • Max uses: {uses_str}\n"
            f"  • Expires: {age_str}",
            ephemeral=True,
        )

        # Update cache so the new invite is tracked immediately
        self._cache.setdefault(interaction.guild_id or 0, {})[inv.code] = 0

        # Audit log
        log_ch_id = _log_channel_id()
        if log_ch_id and interaction.guild:
            log_ch = interaction.guild.get_channel(log_ch_id)
            if isinstance(log_ch, discord.TextChannel):
                try:
                    await log_ch.send(
                        f"🆕 Invite `{inv.code}` created by {member.mention} "
                        f"for {target.mention} (max_uses={uses_str}, expires={age_str})"
                    )
                except discord.HTTPException:
                    log.exception("audit log failed")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(InviteTrackerCog(bot))
