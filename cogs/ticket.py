"""Feature D: Mee6-style ticket bot.

Flow:
  1. Admin runs /ticket panel in panel channel
  2. Bot posts embed with "🎫 Open ticket" button (persistent)
  3. User clicks → bot creates a NEW private text channel under TICKET_CATEGORY_ID
     - Name format: ticket-{username-ascii}-{NNNN}
     - Permission overwrites: only opener + staff role + bot can see
  4. Welcome message with "🔒 Close ticket" button inside
  5. Close → channel deleted (after short notice)
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from services import storage
from services.i18n import get_ui_lang, t


def _ticket_lang(locale: Optional[str] = None) -> str:
    return get_ui_lang(locale, feature="ticket")


def _card_game_channel_ref() -> str:
    """Return a clickable channel mention for CARD_GAME_CHANNEL_ID, or a #card-game fallback."""
    cid = os.getenv("CARD_GAME_CHANNEL_ID", "").strip()
    return f"<#{cid}>" if cid.isdigit() else "#card-game"

log = logging.getLogger(__name__)

COUNTER_FILE = "ticket_counter.json"
OWNERS_FILE = "ticket_owners.json"  # {user_id: channel_id}


def _staff_role_ids() -> list[int]:
    """Comma-separated TICKET_STAFF_ROLE_IDS first; fall back to legacy single TICKET_STAFF_ROLE_ID."""
    raw = os.getenv("TICKET_STAFF_ROLE_IDS", "") or os.getenv("TICKET_STAFF_ROLE_ID", "")
    out: list[int] = []
    for tok in raw.split(","):
        t = tok.strip()
        if t.isdigit():
            out.append(int(t))
    return out


def _staff_mention() -> str:
    ids = _staff_role_ids()
    return " ".join(f"<@&{i}>" for i in ids) if ids else "@staff"


def _next_counter() -> int:
    data = storage.load(COUNTER_FILE, default={"next": 1})
    n = int(data.get("next", 1))
    data["next"] = n + 1
    storage.save(COUNTER_FILE, data)
    return n


def _load_owners() -> dict:
    return storage.load(OWNERS_FILE, default={})


def _save_owners(d: dict) -> None:
    storage.save(OWNERS_FILE, d)


def _set_owner(user_id: int, channel_id: int) -> None:
    d = _load_owners()
    d[str(user_id)] = channel_id
    _save_owners(d)


def _clear_owner_by_channel(channel_id: int) -> None:
    d = _load_owners()
    for uid, cid in list(d.items()):
        if cid == channel_id:
            d.pop(uid, None)
    _save_owners(d)


def _get_existing_ticket_channel(
    guild: discord.Guild, user_id: int
) -> Optional[discord.TextChannel]:
    """Return the user's currently-open ticket channel, or None."""
    d = _load_owners()
    cid = d.get(str(user_id))
    if not cid:
        return None
    ch = guild.get_channel(int(cid))
    if isinstance(ch, discord.TextChannel):
        return ch
    # Stale entry (channel deleted manually) → clean up
    d.pop(str(user_id), None)
    _save_owners(d)
    return None


def _safe_name(raw: str, max_len: int = 20) -> str:
    """Sanitize a user display name to ascii-safe channel slug."""
    nfkd = unicodedata.normalize("NFKD", raw)
    ascii_only = nfkd.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    return slug[:max_len] or "user"


def _resolve_category(panel_channel: discord.TextChannel) -> Optional[discord.CategoryChannel]:
    cat_id = os.getenv("TICKET_CATEGORY_ID", "")
    if cat_id.isdigit():
        cat = panel_channel.guild.get_channel(int(cat_id))
        if isinstance(cat, discord.CategoryChannel):
            return cat
    # Fallback: use panel channel's own category
    return panel_channel.category


def _is_ticket_channel(channel: discord.abc.GuildChannel) -> bool:
    """A channel is a ticket if it lives under the configured ticket category."""
    if not isinstance(channel, discord.TextChannel):
        return False
    cat_id = os.getenv("TICKET_CATEGORY_ID", "")
    if cat_id.isdigit() and channel.category_id == int(cat_id):
        return True
    # Backward compat: name-based check
    return channel.name.startswith("ticket-") or channel.name[:4].isdigit()


class TicketCloseView(discord.ui.View):
    """Persistent close button inside each ticket channel."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🔒 Close ticket",
        style=discord.ButtonStyle.danger,
        custom_id="ticket:close",
    )
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        lang = _ticket_lang(str(interaction.locale))
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or not _is_ticket_channel(channel):
            await interaction.response.send_message(
                t("ticket.close_not_ticket", lang), ephemeral=True
            )
            return
        await interaction.response.send_message(
            t("ticket.close_notice", lang, user=interaction.user.mention)
        )
        await asyncio.sleep(5)
        _clear_owner_by_channel(channel.id)
        try:
            await channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException:
            log.exception("delete failed")


class TicketOpenView(discord.ui.View):
    """Persistent open button on the support panel."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🎫 Open ticket",
        style=discord.ButtonStyle.primary,
        custom_id="ticket:open",
    )
    async def open(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        lang = _ticket_lang(str(interaction.locale))
        panel_channel = interaction.channel
        guild = interaction.guild
        if not isinstance(panel_channel, discord.TextChannel) or guild is None:
            await interaction.followup.send(
                t("ticket.open_not_text", lang), ephemeral=True
            )
            return

        # Duplicate guard: one open ticket per user
        existing = _get_existing_ticket_channel(guild, interaction.user.id)
        if existing is not None:
            await interaction.followup.send(
                t("ticket.duplicate", lang, channel=existing.mention),
                ephemeral=True,
            )
            return

        # Build channel name: {NNNN}-{username-ascii}
        n = _next_counter()
        safe = _safe_name(interaction.user.name)
        ch_name = f"{n:04d}-{safe}"[:95]

        # Permission overwrites: hide from @everyone, allow opener + staff role + bot
        overwrites: dict = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
                add_reactions=True,
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
                read_message_history=True,
                embed_links=True,
            ),
        }
        for staff_id in _staff_role_ids():
            staff_role = guild.get_role(staff_id)
            if staff_role:
                overwrites[staff_role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_messages=True,
                    manage_channels=True,
                    attach_files=True,
                    embed_links=True,
                )

        category = _resolve_category(panel_channel)

        try:
            new_channel = await guild.create_text_channel(
                name=ch_name,
                category=category,
                overwrites=overwrites,
                topic=f"Ticket #{n:04d} opened by {interaction.user} ({interaction.user.id})",
                reason=f"Ticket opened by {interaction.user}",
            )
        except discord.HTTPException as e:
            log.exception("channel creation failed")
            await interaction.followup.send(
                t("ticket.create_failed", lang, err=str(e)), ephemeral=True
            )
            return

        welcome = t(
            "ticket.welcome",
            lang,
            n=n,
            opener=interaction.user.mention,
            staff=_staff_mention(),
        )

        try:
            await new_channel.send(welcome, view=TicketCloseView())
        except discord.HTTPException:
            log.exception("welcome message failed")

        # Track ownership for duplicate prevention
        _set_owner(interaction.user.id, new_channel.id)

        await interaction.followup.send(
            t("ticket.created", lang, channel=new_channel.mention),
            ephemeral=True,
        )


class TicketCog(commands.Cog):
    _views_registered = False

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Register persistent views ONCE (on_ready can fire multiple times on reconnect).
        if TicketCog._views_registered:
            return
        TicketCog._views_registered = True
        self.bot.add_view(TicketOpenView())
        self.bot.add_view(TicketCloseView())
        log.info("Ticket persistent views registered (one-time)")

    ticket_group = app_commands.Group(
        name="ticket",
        description="Ticket tool",
        default_permissions=discord.Permissions(administrator=True),
    )

    @ticket_group.command(name="panel", description="Post the ticket panel in this channel")
    async def panel(self, interaction: discord.Interaction) -> None:
        lang = _ticket_lang(str(interaction.locale))
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                t("ticket.panel_not_text", lang), ephemeral=True
            )
            return

        card_game_ref = _card_game_channel_ref()
        if os.getenv("CARD_GAME_CHANNEL_ID", "").strip().isdigit():
            desc = t("ticket.panel_desc", lang, card_game=card_game_ref)
        else:
            desc = t("ticket.panel_desc_no_cardgame", lang)
        bullets = t("ticket.panel_bullets", lang)

        embed = discord.Embed(
            title=t("ticket.panel_title", lang),
            description=f"{desc}\n\n—\n{bullets}",
            color=0x5865F2,
        )
        embed.set_footer(text=t("ticket.panel_footer", lang))
        await interaction.response.send_message(embed=embed, view=TicketOpenView())

    @ticket_group.command(name="close", description="Close this ticket (delete channel)")
    async def close_cmd(self, interaction: discord.Interaction) -> None:
        lang = _ticket_lang(str(interaction.locale))
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or not _is_ticket_channel(channel):
            await interaction.response.send_message(
                t("ticket.close_cmd_not_ticket", lang), ephemeral=True
            )
            return
        await interaction.response.send_message(
            t("ticket.close_notice", lang, user=interaction.user.mention)
        )
        await asyncio.sleep(5)
        _clear_owner_by_channel(channel.id)
        try:
            await channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException:
            log.exception("close failed")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TicketCog(bot))
