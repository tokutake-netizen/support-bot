"""Weekly DHL/FedEx fuel surcharge auto-fetch.

- Every Monday 04:00 JST → scrape DHL + FedEx Japan pages, cache the %
  per carrier in data/fuel_surcharge.json.
- On failure, the previous value stays in cache and a warning is posted
  to MODERATOR_CHANNEL_ID so the admin can update manually.
- Slash command `/fuelsurcharge refresh` (admin) triggers an on-demand
  fetch for previewing the page output without waiting a week.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, time as dtime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    from zoneinfo import ZoneInfo
    JST = ZoneInfo("Asia/Tokyo")
except Exception:  # pragma: no cover
    JST = timezone.utc

from services import fuel_surcharge

log = logging.getLogger(__name__)


def _moderator_channel_id() -> int | None:
    raw = (os.getenv("MODERATOR_CHANNEL_ID") or "").strip()
    return int(raw) if raw.isdigit() else None


def _format_status_msg(status: dict) -> str:
    lines = ["⛽ **燃油サーチャージ 取得結果**"]
    for carrier, info in status.items():
        if info.get("mode") == "manual":
            kept = info.get("kept")
            kept_str = f"{kept:.2f}%" if kept is not None else "未設定"
            lines.append(f"  🔧 {carrier}: 手動モード (ダッシュボード値 {kept_str} を使用)")
            continue
        if info.get("ok"):
            lines.append(f"  ✅ {carrier}: {info['pct']:.2f}%")
        else:
            kept = info.get("kept")
            if kept is not None:
                lines.append(
                    f"  ⚠️ {carrier}: 自動取得失敗 — 既存値 {kept:.2f}% を保持"
                )
            else:
                lines.append(
                    f"  ❌ {carrier}: 自動取得失敗 — ダッシュボードで手動入力してください"
                )
    lines.append(
        f"_次回自動取得: 来週月曜 04:00 JST。今すぐ再取得は `/fuelsurcharge refresh`_"
    )
    return "\n".join(lines)


class FuelSurchargeCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.weekly.start()

    def cog_unload(self) -> None:
        self.weekly.cancel()

    @tasks.loop(time=dtime(hour=4, minute=0, tzinfo=JST))
    async def weekly(self) -> None:
        # Daily 04:00 tick — only do work on Mondays.
        if datetime.now(JST).weekday() != 0:
            return
        log.info("fuel_surcharge: starting weekly refresh")
        status = await fuel_surcharge.refresh_all()
        log.info("fuel_surcharge: result=%s", status)
        await self._post_status(status)

    @weekly.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _post_status(self, status: dict) -> None:
        ch_id = _moderator_channel_id()
        if ch_id is None:
            return
        ch = self.bot.get_channel(ch_id)
        if not isinstance(ch, discord.TextChannel):
            return
        try:
            await ch.send(_format_status_msg(status))
        except discord.HTTPException:
            log.exception("fuel_surcharge: status post failed")

    fuel_group = app_commands.Group(
        name="fuelsurcharge",
        description="燃油サーチャージ管理",
        default_permissions=discord.Permissions(administrator=True),
    )

    @fuel_group.command(name="refresh", description="DHL/FedEx の % を今すぐ取得")
    async def refresh_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        status = await fuel_surcharge.refresh_all()
        await interaction.followup.send(_format_status_msg(status), ephemeral=True)
        # Also broadcast to moderator channel so the audit trail is in one place
        await self._post_status(status)

    @fuel_group.command(name="show", description="現在キャッシュされている値を表示")
    async def show_cmd(self, interaction: discord.Interaction) -> None:
        cache = fuel_surcharge.load_cache()
        if not cache:
            await interaction.response.send_message("(まだ取得していません)", ephemeral=True)
            return
        lines = ["⛽ **キャッシュ済 燃油サーチャージ**"]
        for carrier, info in cache.items():
            ts = info.get("fetched_at", 0)
            when = datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d %H:%M") if ts else "—"
            lines.append(
                f"  ・{carrier}: **{info.get('pct'):.2f}%** "
                f"(source: {info.get('source')}, 取得: {when})"
            )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FuelSurchargeCog(bot))
