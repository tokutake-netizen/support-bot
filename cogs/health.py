"""Feature: /health slash command.

Returns an ephemeral status snapshot answering "is the bot alive and
all subsystems healthy?". Anyone with admin can run it. Useful when
something looks off and you need a quick "yes the bot is up, here's
what each subsystem reports" without tailing logs.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from services import digest_store

log = logging.getLogger(__name__)


def _read_json(name: str) -> Optional[dict]:
    p = Path("data") / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return None


def _fmt_uptime(secs: float) -> str:
    secs = int(secs)
    d, secs = divmod(secs, 86400)
    h, secs = divmod(secs, 3600)
    m, _ = divmod(secs, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


def _fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class HealthCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.started_at = time.time()

    @app_commands.command(
        name="health",
        description="Show bot health snapshot (cogs / API budgets / recent activity)",
    )
    @app_commands.default_permissions(administrator=True)
    async def health(self, interaction: discord.Interaction) -> None:
        bot = self.bot
        uptime_s = time.time() - self.started_at
        latency_ms = round(bot.latency * 1000, 1) if bot.latency else 0

        # Loaded cogs
        cog_names = sorted(bot.cogs.keys())

        # Recent activity (last 24h)
        day_ago = time.time() - 86400
        recent = digest_store.events_in_range(day_ago)
        recent_counts: dict[str, int] = {}
        for e in recent:
            recent_counts[e.get("type", "?")] = recent_counts.get(e.get("type", "?"), 0) + 1

        # API usage
        anth = _read_json("usage.json") or {}
        deepl = _read_json("deepl_usage.json") or {}
        try:
            budget = float(os.getenv("MONTHLY_BUDGET_USD", "10"))
        except ValueError:
            budget = 10.0
        anth_cost = float(anth.get("total_usd", 0) or 0)

        # Compose embed
        embed = discord.Embed(
            title="🏥 Bot Health",
            color=0x23A55A if uptime_s > 30 and latency_ms < 500 else 0xF0B232,
        )
        embed.add_field(
            name="Process",
            value=(
                f"・uptime: **{_fmt_uptime(uptime_s)}**\n"
                f"・gateway latency: **{latency_ms} ms**\n"
                f"・guilds: **{len(bot.guilds)}**\n"
                f"・cogs loaded: **{len(cog_names)}**"
            ),
            inline=False,
        )
        embed.add_field(
            name="API budget (this month)",
            value=(
                f"・Anthropic: **${anth_cost:.2f}** / ${budget:.2f}"
                f" ({(anth_cost / budget * 100):.0f}%)\n" if budget else ""
                f"・DeepL chars: **{int(deepl.get('chars', 0)):,}**"
            ),
            inline=True,
        )
        if recent_counts:
            lines = [f"・{k}: **{v}**" for k, v in sorted(recent_counts.items(), key=lambda x: -x[1])]
            embed.add_field(name="Last 24h", value="\n".join(lines), inline=True)
        else:
            embed.add_field(name="Last 24h", value="_no events_", inline=True)

        embed.add_field(
            name="Cogs loaded",
            value="`" + "` `".join(cog_names) + "`" if cog_names else "—",
            inline=False,
        )
        embed.set_footer(text=f"Generated at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HealthCog(bot))
