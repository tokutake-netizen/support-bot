"""Feature H: Auction (async, bid-button style).

State machine: pending -> live -> ending -> ended
  - live    : accepting bids
  - ending  : last `anti_snipe_threshold` seconds; bids extend by `anti_snipe_seconds`
  - ended   : winner announced + private deal channel auto-created

Persistence: `data/auctions.json`. On bot restart, persistent views resume.
End-of-auction action: a private "deal" channel is created under TICKET_CATEGORY_ID
(or AUCTION_TICKET_CATEGORY_ID if set) with the winner + host + staff. The bot
does no payment integration — the host handles invoicing manually.

Slash commands:
  /auction create  - admin/manager starts a new auction (in a Forum channel or text channel)
  /auction end     - end early
  /auction cancel  - cancel (no winner; mark as cancelled)
  /auction list    - active auctions
  /auction history - show bid history for one auction
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from services import storage

log = logging.getLogger(__name__)

AUCTIONS_FILE = "auctions.json"
DURATION_RE = re.compile(r"^\s*(?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?\s*$", re.I)


# ---------- helpers ----------


def parse_duration(text: str) -> Optional[int]:
    text = text.strip().lower()
    if not text:
        return None
    m = DURATION_RE.match(text)
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    total = d * 86400 + h * 3600 + mi * 60 + s
    return total if total > 0 else None


def fmt_duration(secs: int) -> str:
    if secs <= 0:
        return "0s"
    parts = []
    for unit, label in ((86400, "d"), (3600, "h"), (60, "m"), (1, "s")):
        if secs >= unit:
            parts.append(f"{secs // unit}{label}")
            secs %= unit
    return " ".join(parts)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _load_all() -> dict:
    return storage.load(AUCTIONS_FILE, default={})


def _save_all(data: dict) -> None:
    storage.save(AUCTIONS_FILE, data)


def _manager_role_ids() -> list[int]:
    raw = os.getenv("AUCTION_MANAGER_ROLE_IDS", "")
    return [int(t.strip()) for t in raw.split(",") if t.strip().isdigit()]


async def _ensure_manager(interaction: discord.Interaction) -> bool:
    """Admin OR member with an AUCTION_MANAGER_ROLE_IDS role."""
    member = interaction.user
    if not isinstance(member, discord.Member):
        await interaction.response.send_message("⚠️ Guild-only command.", ephemeral=True)
        return False
    allowed = _manager_role_ids()
    if member.guild_permissions.administrator:
        return True
    if allowed and any(r.id in allowed for r in member.roles):
        return True
    await interaction.response.send_message(
        "⚠️ You don't have permission to manage auctions.", ephemeral=True
    )
    return False


def _staff_role_ids() -> list[int]:
    raw = os.getenv("TICKET_STAFF_ROLE_IDS", "") or os.getenv("TICKET_STAFF_ROLE_ID", "")
    return [int(t.strip()) for t in raw.split(",") if t.strip().isdigit()]


def _resolve_deal_category(guild: discord.Guild) -> Optional[discord.CategoryChannel]:
    """Where to put the winner's private deal channel. AUCTION_TICKET_CATEGORY_ID wins,
    falls back to TICKET_CATEGORY_ID."""
    for env in ("AUCTION_TICKET_CATEGORY_ID", "TICKET_CATEGORY_ID"):
        cid = os.getenv(env, "").strip()
        if cid.isdigit():
            cat = guild.get_channel(int(cid))
            if isinstance(cat, discord.CategoryChannel):
                return cat
    return None


def _fmt_money(amount: int, currency: str) -> str:
    if currency.upper() in {"JPY", "¥"}:
        return f"¥{amount:,}"
    if currency.upper() in {"USD", "$"}:
        return f"${amount:,}"
    return f"{amount:,} {currency.upper()}"


def _highest_bid(auction: dict) -> Optional[dict]:
    bids = auction.get("bids", [])
    if not bids:
        return None
    return max(bids, key=lambda b: int(b["amount"]))


def _current_price(auction: dict) -> int:
    """The amount the next bid must exceed (or equal for the very first bid)."""
    hi = _highest_bid(auction)
    if hi is None:
        return int(auction["starting_bid"])
    return int(hi["amount"]) + int(auction.get("min_increment", 100))


# ---------- Embed ----------


def _build_embed(auction: dict) -> discord.Embed:
    ends_at = _parse_iso(auction["ends_at"])
    ended = auction.get("ended", False)
    cancelled = auction.get("cancelled", False)
    currency = auction.get("currency", "JPY")

    if cancelled:
        color = 0x4E5058
        title = f"🛑 [Cancelled] {auction['title']}"
    elif ended:
        color = 0x4E5058
        title = f"🏁 [Ended] {auction['title']}"
    else:
        color = 0xF0B232

        title = f"🔨 AUCTION: {auction['title']}"

    desc = auction.get("description") or ""

    hi = _highest_bid(auction)
    fields = []
    if hi is not None:
        fields.append((
            "💰 Current high bid",
            f"{_fmt_money(int(hi['amount']), currency)} by <@{hi['user_id']}>",
            True,
        ))
    else:
        fields.append((
            "💰 Starting bid",
            _fmt_money(int(auction["starting_bid"]), currency),
            True,
        ))
    fields.append(("➕ Min increment", _fmt_money(int(auction.get("min_increment", 100)), currency), True))
    fields.append(("👥 Bidders", str(len(set(b["user_id"] for b in auction.get("bids", [])))), True))

    if ended and not cancelled:
        winner_id = auction.get("winner")
        winning_bid = auction.get("winning_bid", 0)
        if winner_id:
            fields.append(("🏆 Winner", f"<@{winner_id}> — {_fmt_money(int(winning_bid), currency)}", False))
        else:
            fields.append(("🏆 Winner", "_No bids — no winner._", False))
    elif not cancelled:
        ts = int(ends_at.timestamp())
        fields.append(("⏱ Ends", f"<t:{ts}:R> (<t:{ts}:F>)", False))

    fields.append(("🎯 Host", f"<@{auction['host_id']}>", True))

    embed = discord.Embed(title=title, description=desc or None, color=color)
    for name, value, inline in fields:
        embed.add_field(name=name, value=value, inline=inline)
    if auction.get("image_url"):
        embed.set_image(url=auction["image_url"])
    if not ended and not cancelled:
        embed.set_footer(text="Click 💰 Place Bid to bid. Anti-snipe: bidding near the end extends the timer.")
    return embed


# ---------- UI ----------


class BidModal(discord.ui.Modal, title="Place Bid"):
    amount: discord.ui.TextInput

    def __init__(self, auction_key: str, min_amount: int, currency: str) -> None:
        super().__init__()
        self._key = auction_key
        self._min = min_amount
        self.amount = discord.ui.TextInput(
            label=f"Your bid (>= {_fmt_money(min_amount, currency)})",
            placeholder=str(min_amount),
            required=True,
            max_length=12,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.amount.value.strip().replace(",", "").replace("¥", "").replace("$", "")
        try:
            amt = int(raw)
        except ValueError:
            await interaction.response.send_message(
                "⚠️ Enter a whole number (no decimals).", ephemeral=True
            )
            return
        if amt <= 0:
            await interaction.response.send_message("⚠️ Amount must be positive.", ephemeral=True)
            return

        all_data = _load_all()
        auction = all_data.get(self._key)
        if not auction:
            await interaction.response.send_message("⚠️ Auction is no longer tracked.", ephemeral=True)
            return
        if auction.get("ended") or auction.get("cancelled"):
            await interaction.response.send_message("⚠️ This auction has already ended.", ephemeral=True)
            return
        if interaction.user.id == auction["host_id"]:
            await interaction.response.send_message(
                "⚠️ You can't bid on your own auction.", ephemeral=True
            )
            return

        need = _current_price(auction)
        if amt < need:
            await interaction.response.send_message(
                f"⚠️ Your bid must be at least {_fmt_money(need, auction.get('currency','JPY'))}.",
                ephemeral=True,
            )
            return

        previous_high = _highest_bid(auction)

        # Record bid
        auction.setdefault("bids", []).append({
            "user_id": interaction.user.id,
            "amount": amt,
            "at": _now_utc().isoformat(),
        })

        # Anti-snipe: extend ends_at if within threshold
        ends_at = _parse_iso(auction["ends_at"])
        threshold = int(auction.get("anti_snipe_threshold", 60))
        extension = int(auction.get("anti_snipe_seconds", 60))
        remaining = (ends_at - _now_utc()).total_seconds()
        extended = False
        if 0 < remaining <= threshold:
            ends_at = ends_at + timedelta(seconds=extension)
            auction["ends_at"] = ends_at.isoformat()
            extended = True

        all_data[self._key] = auction
        _save_all(all_data)

        # Refresh embed in place
        try:
            embed = _build_embed(auction)
            await interaction.response.edit_message(embed=embed, view=AuctionView())
        except discord.HTTPException:
            log.exception("bid: edit_message failed")
            try:
                await interaction.response.send_message("✅ Bid recorded.", ephemeral=True)
            except discord.HTTPException:
                pass

        # Notify outbid + extension publicly (small follow-up)
        try:
            ch = interaction.channel
            if ch is not None:
                msg_bits = [f"💰 <@{interaction.user.id}> bid **{_fmt_money(amt, auction.get('currency','JPY'))}**."]
                if previous_high:
                    msg_bits.append(f"<@{previous_high['user_id']}> has been outbid.")
                if extended:
                    msg_bits.append(f"⏰ Anti-snipe: timer extended +{extension}s.")
                await ch.send(" ".join(msg_bits))
        except discord.HTTPException:
            log.exception("bid: follow-up announce failed")


class AuctionView(discord.ui.View):
    """Persistent view: bid + history. One view object handles all auctions
    via the message_id key in storage."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="💰 Place Bid",
        style=discord.ButtonStyle.success,
        custom_id="auction:bid",
    )
    async def bid(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        key = str(interaction.message.id)
        all_data = _load_all()
        auction = all_data.get(key)
        if not auction:
            await interaction.response.send_message("⚠️ Auction is no longer tracked.", ephemeral=True)
            return
        if auction.get("ended") or auction.get("cancelled"):
            await interaction.response.send_message("⚠️ This auction has already ended.", ephemeral=True)
            return
        need = _current_price(auction)
        await interaction.response.send_modal(
            BidModal(key, need, auction.get("currency", "JPY"))
        )

    @discord.ui.button(
        label="📜 Bid history",
        style=discord.ButtonStyle.secondary,
        custom_id="auction:history",
    )
    async def history(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        key = str(interaction.message.id)
        all_data = _load_all()
        auction = all_data.get(key)
        if not auction:
            await interaction.response.send_message("⚠️ Auction is no longer tracked.", ephemeral=True)
            return
        bids = auction.get("bids", [])
        if not bids:
            await interaction.response.send_message("_No bids yet._", ephemeral=True)
            return
        currency = auction.get("currency", "JPY")
        # Reverse-chronological, top 20
        sorted_bids = sorted(bids, key=lambda b: b["at"], reverse=True)[:20]
        lines = [f"📜 **Bid history** ({len(bids)} total)"]
        for b in sorted_bids:
            ts = _parse_iso(b["at"])
            lines.append(
                f"・<@{b['user_id']}> — {_fmt_money(int(b['amount']), currency)} "
                f"(<t:{int(ts.timestamp())}:R>)"
            )
        await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)


# ---------- Cog ----------


class AuctionCog(commands.Cog):
    _views_registered = False

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.check_expired.start()

    def cog_unload(self) -> None:
        self.check_expired.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if AuctionCog._views_registered:
            return
        AuctionCog._views_registered = True
        self.bot.add_view(AuctionView())
        log.info("Auction persistent view registered (one-time)")

    @tasks.loop(seconds=30)
    async def check_expired(self) -> None:
        all_data = _load_all()
        now = _now_utc()
        for key, auction in list(all_data.items()):
            if auction.get("ended") or auction.get("cancelled"):
                continue
            try:
                ends_at = _parse_iso(auction["ends_at"])
            except Exception:
                continue
            if ends_at <= now:
                try:
                    await self._end_auction(key, auction, manual=False)
                except Exception:
                    log.exception("auction: auto-end failed for %s", key)

    @check_expired.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _end_auction(self, key: str, auction: dict, manual: bool = False) -> Optional[dict]:
        hi = _highest_bid(auction)
        auction["ended"] = True
        auction["winner"] = hi["user_id"] if hi else None
        auction["winning_bid"] = int(hi["amount"]) if hi else 0
        all_data = _load_all()
        all_data[key] = auction
        _save_all(all_data)

        ch_id = auction.get("thread_id") or auction.get("channel_id")
        ch = self.bot.get_channel(int(ch_id)) if ch_id else None
        if ch is not None:
            try:
                msg = await ch.fetch_message(int(key))
                embed = _build_embed(auction)
                # Replace view with a disabled "ended" button
                view = discord.ui.View()
                view.add_item(discord.ui.Button(
                    style=discord.ButtonStyle.secondary,
                    label="🏁 Ended",
                    disabled=True,
                    custom_id=f"auction:ended:{key}",
                ))
                await msg.edit(embed=embed, view=view)
            except discord.HTTPException:
                log.exception("auction end: edit message failed")
            try:
                if hi:
                    await ch.send(
                        f"🏆 **WINNER**: <@{hi['user_id']}> with "
                        f"{_fmt_money(int(hi['amount']), auction.get('currency','JPY'))}!\n"
                        f"<@{auction['host_id']}> opening a private deal channel..."
                    )
                else:
                    await ch.send("⚠️ No bids — auction closed without a winner.")
            except discord.HTTPException:
                log.exception("auction end: announce failed")

        if hi:
            await self._open_deal_channel(auction, hi)
        return hi

    async def _open_deal_channel(self, auction: dict, hi: dict) -> None:
        guild_id = auction.get("guild_id")
        guild = self.bot.get_guild(int(guild_id)) if guild_id else None
        if guild is None:
            return
        category = _resolve_deal_category(guild)

        host = guild.get_member(int(auction["host_id"]))
        winner = guild.get_member(int(hi["user_id"]))
        if winner is None:
            try:
                winner = await guild.fetch_member(int(hi["user_id"]))
            except discord.HTTPException:
                pass

        overwrites: dict = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
                read_message_history=True,
                embed_links=True,
            ),
        }
        if winner is not None:
            overwrites[winner] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, embed_links=True,
            )
        if host is not None:
            overwrites[host] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, embed_links=True,
            )
        for staff_id in _staff_role_ids():
            role = guild.get_role(staff_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    manage_messages=True, attach_files=True, embed_links=True,
                )

        slug_src = (auction.get("title") or "deal").lower()
        slug = re.sub(r"[^a-z0-9]+", "-", slug_src).strip("-")[:40] or "deal"
        ch_name = f"deal-{slug}-{winner.name[:10] if winner else 'winner'}"[:95]
        ch_name = re.sub(r"[^a-zA-Z0-9-]+", "-", ch_name).strip("-").lower()

        try:
            deal_ch = await guild.create_text_channel(
                name=ch_name,
                category=category,
                overwrites=overwrites,
                topic=f"Auction win: {auction.get('title')} — {_fmt_money(int(hi['amount']), auction.get('currency','JPY'))}",
                reason="auction.py: deal channel for winner",
            )
        except discord.HTTPException:
            log.exception("auction: create deal channel failed")
            return

        currency = auction.get("currency", "JPY")
        body = (
            f"🏆 **Auction won!**\n"
            f"・Item: **{auction.get('title')}**\n"
            f"・Winning bid: **{_fmt_money(int(hi['amount']), currency)}**\n"
            f"・Winner: <@{hi['user_id']}>\n"
            f"・Seller: <@{auction['host_id']}>\n\n"
            f"Please coordinate payment and shipping here. "
            f"(Payment / invoicing handled outside this bot.)"
        )
        try:
            await deal_ch.send(body)
        except discord.HTTPException:
            log.exception("auction: deal channel welcome failed")

        # DM the winner (best-effort)
        if winner is not None:
            try:
                await winner.send(
                    f"🏆 You won the auction **{auction.get('title')}** with "
                    f"{_fmt_money(int(hi['amount']), currency)}!\n"
                    f"A private channel was opened: {deal_ch.mention}"
                )
            except discord.HTTPException:
                pass

    # ============== Slash commands ==============

    auction_group = app_commands.Group(
        name="auction",
        description="Auction tool",
    )

    @auction_group.command(name="create", description="Start a new auction")
    @app_commands.describe(
        title="Item title (e.g. 'PSA 10 Charizard 1st Ed.')",
        starting_bid="Starting price (whole number, no decimals)",
        duration="Duration (e.g. 1d, 12h, 6h30m)",
        description="Item description (optional)",
        image="Item image to attach (optional)",
        image_url="Item image URL (optional, if no attachment)",
        min_increment="Minimum bid step (default 100)",
        currency="Currency code: JPY (default) / USD / etc.",
        anti_snipe_window="Seconds before end that trigger extension (default 60)",
        anti_snipe_extend="Seconds added on snipe (default 60)",
    )
    async def create(
        self,
        interaction: discord.Interaction,
        title: str,
        starting_bid: int,
        duration: str,
        description: Optional[str] = None,
        image: Optional[discord.Attachment] = None,
        image_url: Optional[str] = None,
        min_increment: int = 100,
        currency: str = "JPY",
        anti_snipe_window: int = 60,
        anti_snipe_extend: int = 60,
    ) -> None:
        from services.channel_guard import ensure_channel_allowed
        if not await ensure_channel_allowed(interaction, "auction"):
            return
        if not await _ensure_manager(interaction):
            return

        secs = parse_duration(duration)
        if secs is None or secs < 60:
            await interaction.response.send_message(
                "⚠️ Duration must be at least 1m. Examples: `12h`, `1d`, `6h30m`.",
                ephemeral=True,
            )
            return
        if starting_bid < 1 or min_increment < 1:
            await interaction.response.send_message(
                "⚠️ `starting_bid` and `min_increment` must be positive integers.",
                ephemeral=True,
            )
            return
        if anti_snipe_window < 0 or anti_snipe_extend < 0:
            await interaction.response.send_message(
                "⚠️ anti-snipe values must be >= 0.", ephemeral=True
            )
            return

        # Resolve image
        resolved_image_url: Optional[str] = None
        if image is not None:
            if not (image.content_type or "").startswith("image/"):
                await interaction.response.send_message(
                    "⚠️ The uploaded file is not an image.", ephemeral=True
                )
                return
            resolved_image_url = image.url
        elif image_url:
            resolved_image_url = image_url.strip()

        ends_at = _now_utc() + timedelta(seconds=secs)
        auction = {
            "guild_id": interaction.guild_id,
            "channel_id": None,           # filled below; the parent channel of post / thread parent
            "thread_id": None,            # filled if posted to a Forum/Thread
            "title": title,
            "description": description or "",
            "image_url": resolved_image_url,
            "host_id": interaction.user.id,
            "starting_bid": int(starting_bid),
            "min_increment": int(min_increment),
            "currency": currency.upper(),
            "ends_at": ends_at.isoformat(),
            "anti_snipe_threshold": int(anti_snipe_window),
            "anti_snipe_seconds": int(anti_snipe_extend),
            "bids": [],
            "ended": False,
            "cancelled": False,
            "winner": None,
            "winning_bid": 0,
        }
        embed = _build_embed(auction)
        view = AuctionView()

        target = interaction.channel
        msg: Optional[discord.Message] = None

        try:
            if isinstance(target, discord.ForumChannel):
                # Create a thread inside the forum
                thread_with_message = await target.create_thread(
                    name=title[:95],
                    embed=embed,
                    view=view,
                    reason="auction.py: create",
                )
                msg = thread_with_message.message
                auction["channel_id"] = target.id
                auction["thread_id"] = thread_with_message.thread.id
                await interaction.response.send_message(
                    f"✅ Auction posted: {thread_with_message.thread.mention}",
                    ephemeral=True,
                )
            elif isinstance(target, (discord.TextChannel, discord.Thread)):
                await interaction.response.send_message(embed=embed, view=view)
                msg = await interaction.original_response()
                auction["channel_id"] = target.id
                if isinstance(target, discord.Thread):
                    auction["thread_id"] = target.id
            else:
                await interaction.response.send_message(
                    "⚠️ Run /auction create inside a text or forum channel.", ephemeral=True
                )
                return
        except discord.HTTPException:
            log.exception("auction create: post failed")
            await interaction.followup.send("⚠️ Failed to post auction.", ephemeral=True)
            return

        if msg is None:
            return

        all_data = _load_all()
        all_data[str(msg.id)] = auction
        _save_all(all_data)
        log.info(
            "auction created msg=%s title=%r start=%d duration=%ds",
            msg.id, title, starting_bid, secs,
        )

    @auction_group.command(name="end", description="End an auction early")
    @app_commands.describe(message_id="Auction message ID")
    async def end(self, interaction: discord.Interaction, message_id: str) -> None:
        if not await _ensure_manager(interaction):
            return
        if not message_id.isdigit():
            await interaction.response.send_message("⚠️ Invalid message_id.", ephemeral=True)
            return
        all_data = _load_all()
        auction = all_data.get(message_id)
        if not auction:
            await interaction.response.send_message("⚠️ No auction found.", ephemeral=True)
            return
        if auction.get("ended") or auction.get("cancelled"):
            await interaction.response.send_message("⚠️ Already ended.", ephemeral=True)
            return
        # Only host or admin can end
        if interaction.user.id != auction["host_id"] and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⚠️ Only the host or an admin can end this auction.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._end_auction(message_id, auction, manual=True)
        await interaction.followup.send("✅ Auction ended.", ephemeral=True)

    @auction_group.command(name="cancel", description="Cancel an auction (no winner)")
    @app_commands.describe(message_id="Auction message ID")
    async def cancel(self, interaction: discord.Interaction, message_id: str) -> None:
        if not await _ensure_manager(interaction):
            return
        if not message_id.isdigit():
            await interaction.response.send_message("⚠️ Invalid message_id.", ephemeral=True)
            return
        all_data = _load_all()
        auction = all_data.get(message_id)
        if not auction:
            await interaction.response.send_message("⚠️ No auction found.", ephemeral=True)
            return
        if auction.get("ended") or auction.get("cancelled"):
            await interaction.response.send_message("⚠️ Already ended/cancelled.", ephemeral=True)
            return
        if interaction.user.id != auction["host_id"] and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⚠️ Only the host or an admin can cancel.", ephemeral=True
            )
            return

        auction["cancelled"] = True
        auction["ended"] = True  # treat as terminal
        all_data[message_id] = auction
        _save_all(all_data)

        ch_id = auction.get("thread_id") or auction.get("channel_id")
        ch = self.bot.get_channel(int(ch_id)) if ch_id else None
        if ch is not None:
            try:
                msg = await ch.fetch_message(int(message_id))
                view = discord.ui.View()
                view.add_item(discord.ui.Button(
                    style=discord.ButtonStyle.secondary,
                    label="🛑 Cancelled",
                    disabled=True,
                    custom_id=f"auction:cancel:{message_id}",
                ))
                await msg.edit(embed=_build_embed(auction), view=view)
                await ch.send("🛑 Auction cancelled by host. No winner.")
            except discord.HTTPException:
                log.exception("auction cancel: edit failed")
        await interaction.response.send_message("✅ Cancelled.", ephemeral=True)

    @auction_group.command(name="list", description="List active auctions")
    async def list_active(self, interaction: discord.Interaction) -> None:
        all_data = _load_all()
        actives = [
            (k, a) for k, a in all_data.items()
            if not a.get("ended") and not a.get("cancelled")
        ]
        if not actives:
            await interaction.response.send_message("No active auctions.", ephemeral=True)
            return
        now = _now_utc()
        lines = ["📋 **Active auctions**"]
        for key, a in actives:
            try:
                ends = _parse_iso(a["ends_at"])
                remain = int((ends - now).total_seconds())
                hi = _highest_bid(a)
                price = _fmt_money(int(hi["amount"]) if hi else int(a["starting_bid"]), a.get("currency","JPY"))
                lines.append(
                    f"・🔨 **{a['title']}** (id `{key}`) — {price} — "
                    f"{len(a.get('bids',[]))} bids — {fmt_duration(max(remain,0))} left"
                )
            except Exception:
                continue
        await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

    @auction_group.command(name="history", description="Show bid history for an auction")
    @app_commands.describe(message_id="Auction message ID")
    async def history_cmd(self, interaction: discord.Interaction, message_id: str) -> None:
        if not message_id.isdigit():
            await interaction.response.send_message("⚠️ Invalid message_id.", ephemeral=True)
            return
        all_data = _load_all()
        a = all_data.get(message_id)
        if not a:
            await interaction.response.send_message("⚠️ No auction found.", ephemeral=True)
            return
        bids = a.get("bids", [])
        if not bids:
            await interaction.response.send_message("_No bids yet._", ephemeral=True)
            return
        currency = a.get("currency", "JPY")
        sorted_bids = sorted(bids, key=lambda b: b["at"], reverse=True)[:30]
        lines = [f"📜 **{a['title']}** — {len(bids)} bids"]
        for b in sorted_bids:
            ts = _parse_iso(b["at"])
            lines.append(
                f"・<@{b['user_id']}> — {_fmt_money(int(b['amount']), currency)} "
                f"(<t:{int(ts.timestamp())}:R>)"
            )
        await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AuctionCog(bot))
