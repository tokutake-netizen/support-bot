"""Feature: Event management.

Two layers on top of Discord's native Scheduled Events:
  1. Slash commands + dashboard CRUD to create/cancel/list events
     (these end up as real Discord scheduled events that members see
     in the server's "Events" tab and get notified about natively).
  2. A custom overlay embed posted to a channel with a 🔔 RSVP button.
     Clicking it stores the user in data/events.json and, optionally,
     grants them an RSVP role. A periodic task DMs RSVPers (best-effort)
     and posts a heads-up to the moderator channel a configurable
     number of minutes before start.

The cog deliberately does NOT host the create-event REST call itself —
the dashboard's /guild/<id>/events/create owns that path (it has the
form). The cog only handles after-the-fact wiring: persisting RSVPs,
posting the overlay embed, dispatching reminders.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from services import digest_store, storage

log = logging.getLogger(__name__)

EVENTS_FILE = "events.json"  # {str(scheduled_event_id): {rsvps:[user_id], reminder_sent:bool, role_id:opt, channel_id:opt, message_id:opt, name, start_ts, end_ts}}


def _load_events() -> dict:
    return storage.load(EVENTS_FILE, default={}) or {}


def _save_events(data: dict) -> None:
    storage.save(EVENTS_FILE, data)


def _reminder_channel_id() -> Optional[int]:
    raw = (os.getenv("EVENT_REMINDER_CHANNEL_ID") or os.getenv("MODERATOR_CHANNEL_ID") or "").strip()
    return int(raw) if raw.isdigit() else None


def _reminder_minutes() -> int:
    raw = (os.getenv("EVENT_REMINDER_MINUTES_BEFORE") or "60").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 60


def _event_manager_role_ids() -> list[int]:
    raw = os.getenv("EVENT_MANAGER_ROLE_IDS", "")
    return [int(t.strip()) for t in raw.split(",") if t.strip().isdigit()]


async def _ensure_manager(interaction: discord.Interaction) -> bool:
    member = interaction.user
    if not isinstance(member, discord.Member):
        await interaction.response.send_message("⚠️ Guild-only command.", ephemeral=True)
        return False
    if member.guild_permissions.administrator:
        return True
    allowed = _event_manager_role_ids()
    if allowed and any(r.id in allowed for r in member.roles):
        return True
    await interaction.response.send_message(
        "⚠️ Event manager role required (or admin).", ephemeral=True
    )
    return False


def _build_overlay_embed(meta: dict) -> discord.Embed:
    """Embed posted to a channel alongside the native Discord event."""
    start_ts = int(meta.get("start_ts", 0))
    rsvps = meta.get("rsvps", [])
    desc_lines = [
        f"📅 **Starts**: <t:{start_ts}:F> (<t:{start_ts}:R>)",
        f"🔔 **RSVP**: {len(rsvps)} attendee(s)",
    ]
    if meta.get("location"):
        desc_lines.append(f"📍 **Location**: {meta['location']}")
    if meta.get("description"):
        desc_lines.append("")
        desc_lines.append(meta["description"][:600])
    embed = discord.Embed(
        title=f"📅 {meta.get('name', 'Event')}",
        description="\n".join(desc_lines),
        color=0x5865F2,
    )
    if meta.get("image_url"):
        embed.set_image(url=meta["image_url"])
    embed.set_footer(text="Press the button below to RSVP. We'll DM you before it starts.")
    return embed


class EventRSVPView(discord.ui.View):
    """Persistent RSVP button. Re-resolves the event by its custom_id suffix."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🔔 RSVP",
        style=discord.ButtonStyle.success,
        custom_id="event:rsvp",
    )
    async def rsvp(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Resolve the event by message id (we store message_id alongside the event)
        msg_id = str(interaction.message.id)
        data = _load_events()
        # Find the event whose message_id matches
        event_key = None
        for ev_id, meta in data.items():
            if str(meta.get("message_id")) == msg_id:
                event_key = ev_id
                break
        if event_key is None:
            await interaction.response.send_message(
                "⚠️ This event is no longer tracked.", ephemeral=True
            )
            return

        meta = data[event_key]
        rsvps: list[int] = meta.setdefault("rsvps", [])
        toggle_off = interaction.user.id in rsvps

        if toggle_off:
            rsvps.remove(interaction.user.id)
        else:
            rsvps.append(interaction.user.id)
        data[event_key] = meta
        _save_events(data)

        # Optional role assignment
        role_id = meta.get("role_id")
        if role_id and isinstance(interaction.user, discord.Member):
            role = interaction.guild.get_role(int(role_id))
            if role is not None:
                try:
                    if toggle_off:
                        await interaction.user.remove_roles(role, reason="event RSVP toggle off")
                    else:
                        await interaction.user.add_roles(role, reason="event RSVP")
                except discord.HTTPException:
                    log.exception("RSVP role toggle failed")

        # Refresh embed
        try:
            await interaction.message.edit(embed=_build_overlay_embed(meta), view=self)
        except discord.HTTPException:
            log.exception("RSVP message edit failed")

        if toggle_off:
            await interaction.response.send_message(
                f"❌ RSVP canceled. ({len(rsvps)} attending now)", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"✅ You're in! We'll DM you before it starts. ({len(rsvps)} attending now)",
                ephemeral=True,
            )


class EventsCog(commands.Cog):
    _views_registered = False

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.check_reminders.start()

    def cog_unload(self) -> None:
        self.check_reminders.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if EventsCog._views_registered:
            return
        EventsCog._views_registered = True
        self.bot.add_view(EventRSVPView())
        log.info("Event RSVP persistent view registered (one-time)")

    @tasks.loop(seconds=300)  # 5 min
    async def check_reminders(self) -> None:
        data = _load_events()
        now = datetime.now(timezone.utc).timestamp()
        window = _reminder_minutes() * 60
        for ev_id, meta in list(data.items()):
            if meta.get("reminder_sent") or meta.get("cancelled"):
                continue
            start_ts = int(meta.get("start_ts", 0) or 0)
            if not start_ts:
                continue
            if now < start_ts - window or now >= start_ts:
                continue
            await self._send_reminder(ev_id, meta)
            meta["reminder_sent"] = True
            data[ev_id] = meta
            _save_events(data)

    @check_reminders.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _send_reminder(self, ev_id: str, meta: dict) -> None:
        ch_id = _reminder_channel_id()
        if ch_id is None:
            return
        ch = self.bot.get_channel(ch_id)
        if not isinstance(ch, discord.TextChannel):
            return
        start_ts = int(meta.get("start_ts", 0))
        rsvps = meta.get("rsvps", [])
        try:
            await ch.send(
                f"📅 **{meta.get('name')}** が <t:{start_ts}:R> に始まります "
                f"({len(rsvps)} 名 RSVP 済み)"
            )
        except discord.HTTPException:
            log.exception("event reminder post failed")
        # Best-effort DMs
        for uid in rsvps:
            user = self.bot.get_user(int(uid))
            if user is None:
                try:
                    user = await self.bot.fetch_user(int(uid))
                except Exception:
                    continue
            try:
                await user.send(
                    f"⏰ Reminder: **{meta.get('name')}** starts <t:{start_ts}:R>."
                )
            except discord.HTTPException:
                pass  # DMs closed → silent

    # ---------- slash commands ----------
    event_group = app_commands.Group(name="event", description="Event management")

    @event_group.command(name="list", description="List upcoming events")
    async def list_events(self, interaction: discord.Interaction) -> None:
        data = _load_events()
        now = datetime.now(timezone.utc).timestamp()
        upcoming = [
            (ev_id, meta) for ev_id, meta in data.items()
            if not meta.get("cancelled") and int(meta.get("start_ts", 0) or 0) >= now
        ]
        if not upcoming:
            await interaction.response.send_message("No upcoming events.", ephemeral=True)
            return
        upcoming.sort(key=lambda x: int(x[1].get("start_ts", 0) or 0))
        lines = ["📅 **Upcoming events**"]
        for ev_id, meta in upcoming[:20]:
            rsvps = len(meta.get("rsvps", []))
            lines.append(
                f"・**{meta.get('name')}** — <t:{int(meta['start_ts'])}:R> — "
                f"{rsvps} RSVP — `{ev_id}`"
            )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @event_group.command(name="cancel", description="Cancel an event by id (admin/manager)")
    @app_commands.describe(event_id="Discord scheduled event id (from /event list)")
    async def cancel_event(
        self, interaction: discord.Interaction, event_id: str
    ) -> None:
        if not await _ensure_manager(interaction):
            return
        data = _load_events()
        if event_id not in data:
            await interaction.response.send_message(
                "⚠️ Unknown event id.", ephemeral=True
            )
            return
        data[event_id]["cancelled"] = True
        _save_events(data)
        # Best-effort: cancel the Discord scheduled event too
        if interaction.guild:
            try:
                await interaction.guild.delete_scheduled_event(int(event_id))
            except Exception:
                pass
        await interaction.response.send_message(
            f"✅ Event `{event_id}` marked cancelled.", ephemeral=True
        )

    @event_group.command(name="remind", description="Send the heads-up to RSVPers right now")
    @app_commands.describe(event_id="Discord scheduled event id")
    async def remind_event(
        self, interaction: discord.Interaction, event_id: str
    ) -> None:
        if not await _ensure_manager(interaction):
            return
        data = _load_events()
        meta = data.get(event_id)
        if not meta:
            await interaction.response.send_message("⚠️ Unknown event id.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._send_reminder(event_id, meta)
        meta["reminder_sent"] = True
        data[event_id] = meta
        _save_events(data)
        digest_store.append("event_reminder", {"event_id": event_id, "name": meta.get("name")})
        await interaction.followup.send(
            f"✅ Reminder sent to {len(meta.get('rsvps', []))} RSVPer(s).",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EventsCog(bot))
