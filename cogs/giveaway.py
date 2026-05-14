"""Feature F: Giveaway/Raffle bot.

- /giveaway create   - admin starts a giveaway (prize, duration, winners, required role)
- /giveaway end      - end early
- /giveaway reroll   - re-pick winners for a past giveaway
- /giveaway list     - active giveaways

Click 🎉 button to enter. Background task auto-ends expired giveaways.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from services import storage

log = logging.getLogger(__name__)

GIVEAWAYS_FILE = "giveaways.json"
DURATION_RE = re.compile(r"^\s*(?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?\s*$", re.I)


def parse_duration(text: str) -> Optional[int]:
    """Parse '1d2h30m' or '90s' or '5m' style → seconds. None on bad input."""
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _load_all() -> dict:
    return storage.load(GIVEAWAYS_FILE, default={})


def _save_all(data: dict) -> None:
    storage.save(GIVEAWAYS_FILE, data)


class GiveawayEnterView(discord.ui.View):
    """Persistent enter button. Same view handles all giveaways via custom_id."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🎉 参加 / Enter",
        style=discord.ButtonStyle.success,
        custom_id="giveaway:enter",
    )
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        msg_id = str(interaction.message.id)
        all_data = _load_all()
        gw = all_data.get(msg_id)
        if not gw:
            await interaction.response.send_message(
                "⚠️ This giveaway is no longer tracked.", ephemeral=True
            )
            return
        if gw.get("ended"):
            await interaction.response.send_message(
                "⚠️ This giveaway has already ended.", ephemeral=True
            )
            return

        # Check required role
        req_role_id = gw.get("required_role_id")
        if req_role_id:
            member = interaction.user
            if isinstance(member, discord.Member):
                if not any(r.id == req_role_id for r in member.roles):
                    await interaction.response.send_message(
                        f"⚠️ 参加には <@&{req_role_id}> ロールが必要です。",
                        ephemeral=True,
                    )
                    return

        entries: list[int] = gw.setdefault("entries", [])
        if interaction.user.id in entries:
            # Toggle off (cancel entry)
            entries.remove(interaction.user.id)
            _save_all(all_data)
            await interaction.response.send_message(
                f"❌ 参加を取り消しました。/ Entry cancelled. (現在 {len(entries)} 名)",
                ephemeral=True,
            )
        else:
            entries.append(interaction.user.id)
            _save_all(all_data)
            await interaction.response.send_message(
                f"✅ 参加を受け付けました！/ Entry recorded! (現在 {len(entries)} 名)",
                ephemeral=True,
            )


def _build_embed(gw: dict, ended: bool = False) -> discord.Embed:
    ends_at = _parse_iso(gw["ends_at"])
    color = 0x57F287 if not ended else 0x4E5058
    title = f"🎉 GIVEAWAY: {gw['prize']}"
    if ended:
        title = f"🏁 [終了] {title}"
    desc_lines = [
        f"**🎁 賞品 / Prize**: {gw['prize']}",
        f"**👥 当選数 / Winners**: {gw['winner_count']}",
        f"**⏱ 終了 / Ends**: <t:{int(ends_at.timestamp())}:R> (<t:{int(ends_at.timestamp())}:F>)",
        f"**🎯 ホスト / Host**: <@{gw['host_id']}>",
        f"**👤 参加者 / Entries**: {len(gw.get('entries', []))}",
    ]
    if gw.get("required_role_id"):
        desc_lines.append(f"**🔒 必要ロール / Required**: <@&{gw['required_role_id']}>")
    if ended:
        winners = gw.get("winners", [])
        if winners:
            desc_lines.append("")
            desc_lines.append(f"**🏆 当選者 / Winners**: {', '.join(f'<@{w}>' for w in winners)}")
        else:
            desc_lines.append("\n_参加者なし / No entries_")
    else:
        desc_lines.append("\n下のボタンを押して参加！ / Click the button below to enter!")

    embed = discord.Embed(title=title, description="\n".join(desc_lines), color=color)
    image_url = gw.get("image_url")
    if image_url:
        embed.set_image(url=image_url)
    return embed


class GiveawayCog(commands.Cog):
    _views_registered = False

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.check_expired.start()

    def cog_unload(self) -> None:
        self.check_expired.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if GiveawayCog._views_registered:
            return
        GiveawayCog._views_registered = True
        self.bot.add_view(GiveawayEnterView())
        log.info("Giveaway persistent view registered (one-time)")

    @tasks.loop(seconds=30)
    async def check_expired(self) -> None:
        all_data = _load_all()
        now = datetime.now(timezone.utc)
        for msg_id, gw in list(all_data.items()):
            if gw.get("ended"):
                continue
            try:
                ends_at = _parse_iso(gw["ends_at"])
            except Exception:
                continue
            if ends_at <= now:
                await self._end_giveaway(int(msg_id), gw, manual=False)

    @check_expired.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _end_giveaway(self, msg_id: int, gw: dict, manual: bool = False) -> list[int]:
        entries: list[int] = gw.get("entries", [])
        n = max(1, int(gw.get("winner_count", 1)))
        winners: list[int] = []
        if entries:
            n = min(n, len(entries))
            winners = random.sample(entries, n)

        gw["ended"] = True
        gw["winners"] = winners
        all_data = _load_all()
        all_data[str(msg_id)] = gw
        _save_all(all_data)

        # Update message
        ch = self.bot.get_channel(gw["channel_id"])
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            try:
                msg = await ch.fetch_message(msg_id)
                embed = _build_embed(gw, ended=True)
                # Disable button
                view = discord.ui.View()
                btn = discord.ui.Button(
                    style=discord.ButtonStyle.secondary,
                    label="🏁 終了 / Ended",
                    disabled=True,
                    custom_id="giveaway:ended",
                )
                view.add_item(btn)
                await msg.edit(embed=embed, view=view)
                # Announce
                if winners:
                    mention_str = ", ".join(f"<@{w}>" for w in winners)
                    await ch.send(
                        f"🎊 おめでとう！/ Congratulations {mention_str}!\n"
                        f"🎁 **{gw['prize']}** に当選しました！ホストの <@{gw['host_id']}> から連絡があります。"
                    )
                else:
                    await ch.send("⚠️ 参加者がいなかったため当選者なしです。 / No entries, no winners.")
            except discord.HTTPException:
                log.exception("end_giveaway: edit/send failed")

        return winners

    # ============== Slash commands ==============
    giveaway_group = app_commands.Group(
        name="giveaway",
        description="Giveaway / Raffle 抽選ツール",
        default_permissions=discord.Permissions(administrator=True),
    )

    @giveaway_group.command(name="create", description="新しいGiveawayを開始")
    @app_commands.describe(
        prize="賞品名",
        duration="期間 (例: 1d, 2h30m, 30s)",
        winners="当選数 (デフォルト 1)",
        required_role="参加に必要なロール (任意)",
        image="賞品画像をアップロード (任意)",
        image_url="画像URLでも指定可 (任意)",
    )
    async def create(
        self,
        interaction: discord.Interaction,
        prize: str,
        duration: str,
        winners: int = 1,
        required_role: Optional[discord.Role] = None,
        image: Optional[discord.Attachment] = None,
        image_url: Optional[str] = None,
    ) -> None:
        secs = parse_duration(duration)
        if secs is None:
            await interaction.response.send_message(
                "⚠️ duration の書式は `1d`, `2h30m`, `90s` などです。",
                ephemeral=True,
            )
            return
        if winners < 1 or winners > 50:
            await interaction.response.send_message(
                "⚠️ winners は 1〜50 で指定してください。", ephemeral=True
            )
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "⚠️ テキストチャンネル内で実行してください。", ephemeral=True
            )
            return

        # Resolve image source: attachment takes priority over URL
        resolved_image_url: Optional[str] = None
        if image is not None:
            if not (image.content_type or "").startswith("image/"):
                await interaction.response.send_message(
                    "⚠️ アップロードされたファイルは画像ではありません。", ephemeral=True
                )
                return
            resolved_image_url = image.url
        elif image_url:
            resolved_image_url = image_url.strip()

        ends_at = datetime.now(timezone.utc) + timedelta(seconds=secs)
        gw = {
            "channel_id": interaction.channel.id,
            "guild_id": interaction.guild_id,
            "prize": prize,
            "winner_count": winners,
            "ends_at": ends_at.isoformat(),
            "host_id": interaction.user.id,
            "required_role_id": required_role.id if required_role else None,
            "image_url": resolved_image_url,
            "entries": [],
            "ended": False,
            "winners": [],
        }
        embed = _build_embed(gw, ended=False)
        await interaction.response.send_message(embed=embed, view=GiveawayEnterView())
        msg = await interaction.original_response()

        all_data = _load_all()
        all_data[str(msg.id)] = gw
        _save_all(all_data)

        log.info("giveaway %s created: prize=%r winners=%d duration=%ds", msg.id, prize, winners, secs)

    @giveaway_group.command(name="end", description="Giveawayを早期終了 (リプライまたはmessage_id指定)")
    @app_commands.describe(message_id="対象の Giveaway メッセージID")
    async def end(self, interaction: discord.Interaction, message_id: str) -> None:
        if not message_id.isdigit():
            await interaction.response.send_message("⚠️ message_id is invalid", ephemeral=True)
            return
        all_data = _load_all()
        gw = all_data.get(message_id)
        if not gw:
            await interaction.response.send_message("⚠️ そのIDのGiveawayが見つかりません", ephemeral=True)
            return
        if gw.get("ended"):
            await interaction.response.send_message("⚠️ 既に終了しています", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        winners = await self._end_giveaway(int(message_id), gw, manual=True)
        await interaction.followup.send(
            f"✅ 終了しました。当選者: {len(winners)} 名", ephemeral=True
        )

    @giveaway_group.command(name="reroll", description="終了したGiveawayの当選者を再抽選")
    @app_commands.describe(message_id="対象の Giveaway メッセージID", winners="再抽選人数 (任意)")
    async def reroll(
        self,
        interaction: discord.Interaction,
        message_id: str,
        winners: Optional[int] = None,
    ) -> None:
        if not message_id.isdigit():
            await interaction.response.send_message("⚠️ message_id is invalid", ephemeral=True)
            return
        all_data = _load_all()
        gw = all_data.get(message_id)
        if not gw:
            await interaction.response.send_message("⚠️ そのIDのGiveawayが見つかりません", ephemeral=True)
            return
        if not gw.get("ended"):
            await interaction.response.send_message(
                "⚠️ まだ終了していません。先に /giveaway end してください", ephemeral=True
            )
            return
        entries = gw.get("entries", [])
        if not entries:
            await interaction.response.send_message("⚠️ 参加者なし", ephemeral=True)
            return
        n = min(winners or gw.get("winner_count", 1), len(entries))
        new_winners = random.sample(entries, n)
        gw["winners"] = new_winners
        all_data[message_id] = gw
        _save_all(all_data)
        ch = self.bot.get_channel(gw["channel_id"])
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            await ch.send(
                f"🔁 **REROLL**: 🎁 **{gw['prize']}** の当選者を再抽選しました\n"
                f"🏆 {', '.join(f'<@{w}>' for w in new_winners)}"
            )
        await interaction.response.send_message(
            f"✅ {len(new_winners)} 名再抽選しました", ephemeral=True
        )

    @giveaway_group.command(name="list", description="進行中のGiveaway一覧")
    async def list_active(self, interaction: discord.Interaction) -> None:
        all_data = _load_all()
        actives = [(mid, gw) for mid, gw in all_data.items() if not gw.get("ended")]
        if not actives:
            await interaction.response.send_message("進行中のGiveawayはありません", ephemeral=True)
            return
        lines = ["📋 **進行中のGiveaway**"]
        now = datetime.now(timezone.utc)
        for mid, gw in actives:
            try:
                ends = _parse_iso(gw["ends_at"])
                remain = int((ends - now).total_seconds())
                lines.append(
                    f"・🎁 {gw['prize']} (id `{mid}`) — {len(gw.get('entries',[]))}人参加 — "
                    f"残 {fmt_duration(max(remain,0))}"
                )
            except Exception:
                continue
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GiveawayCog(bot))
