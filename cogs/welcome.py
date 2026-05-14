"""Feature E: Welcome bot.

- Posts a welcome embed in WELCOME_CHANNEL_ID when a new member joins
- Optionally assigns auto-roles
- Bilingual (ja+en) message body
- /welcome test command (admin) to preview
- /welcome setbanner to upload/set banner image
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)


def _set_env_var(key: str, value: str) -> None:
    """Update or append a key in the deployment's .env file (CWD)."""
    os.environ[key] = value
    env_path = Path(".env")
    if not env_path.exists():
        env_path.write_text(f"{key}={value}\n", "utf-8")
        return
    content = env_path.read_text("utf-8")
    pattern = rf"^{re.escape(key)}=.*$"
    if re.search(pattern, content, flags=re.M):
        content = re.sub(pattern, f"{key}={value}", content, flags=re.M)
    else:
        if not content.endswith("\n"):
            content += "\n"
        content += f"{key}={value}\n"
    env_path.write_text(content, "utf-8")


def _channel_id(name: str) -> Optional[int]:
    v = os.getenv(name, "").strip()
    return int(v) if v.isdigit() else None


def _autorole_ids() -> list[int]:
    raw = os.getenv("WELCOME_AUTOROLE_IDS", "")
    out: list[int] = []
    for tok in raw.split(","):
        t = tok.strip()
        if t.isdigit():
            out.append(int(t))
    return out


def _build_embed(member: discord.Member) -> discord.Embed:
    rule_id = _channel_id("WELCOME_RULES_CHANNEL_ID")
    intro_id = _channel_id("WELCOME_INTRO_CHANNEL_ID")
    rule_link = f"<#{rule_id}>" if rule_id else "#rule-guide"
    intro_link = f"<#{intro_id}>" if intro_id else "#your-profile"

    title = os.getenv("WELCOME_TITLE", "🎊 Welcome to the server!")
    color_hex = os.getenv("WELCOME_COLOR", "0x57F287")
    try:
        color = int(color_hex, 16) if color_hex.startswith("0x") else int(color_hex, 16)
    except Exception:
        color = 0x57F287

    template = os.getenv("WELCOME_DESCRIPTION") or (
        "🎉 **{user_mention} さん、ようこそ！** / **Welcome, {user_mention}!**\n\n"
        "・📜 ルール / Rules: {rules}\n"
        "・🙋 自己紹介 / Introduce yourself: {intro}\n"
        "・🎫 困ったら #📩ticket でお気軽にどうぞ / Need help? Open a ticket"
    )
    desc = template.format(
        user_mention=member.mention,
        user_name=member.name,
        guild_name=member.guild.name,
        member_count=member.guild.member_count,
        rules=rule_link,
        intro=intro_link,
    )

    embed = discord.Embed(title=title, description=desc, color=color)

    # Banner image (large)
    banner_url = os.getenv("WELCOME_BANNER_URL", "").strip()
    if banner_url:
        embed.set_image(url=banner_url)

    # Thumbnail: by default user avatar; can override via env (e.g. server logo)
    thumb_url = os.getenv("WELCOME_THUMBNAIL_URL", "").strip()
    if thumb_url:
        embed.set_thumbnail(url=thumb_url)
    elif member.display_avatar:
        embed.set_thumbnail(url=member.display_avatar.url)

    embed.set_footer(
        text=f"You are member #{member.guild.member_count} • Joined {member.created_at:%Y-%m-%d}"
    )
    return embed


class WelcomeCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        ch_id = _channel_id("WELCOME_CHANNEL_ID")
        if not ch_id:
            return
        channel = member.guild.get_channel(ch_id)
        if not isinstance(channel, discord.TextChannel):
            log.warning("welcome channel not found or wrong type")
            return
        try:
            await channel.send(
                content=member.mention,
                embed=_build_embed(member),
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.HTTPException:
            log.exception("welcome post failed")

        # Auto-role assignment
        for role_id in _autorole_ids():
            role = member.guild.get_role(role_id)
            if role:
                try:
                    await member.add_roles(role, reason="Auto role on join")
                except discord.HTTPException:
                    log.exception("autorole failed for %s", role_id)

    welcome_group = app_commands.Group(
        name="welcome",
        description="Welcomeツール / Welcome tool",
        default_permissions=discord.Permissions(administrator=True),
    )

    @welcome_group.command(name="test", description="自分自身へのウェルカム表示プレビュー")
    async def test(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("⚠️ guild only", ephemeral=True)
            return
        embed = _build_embed(interaction.user)
        await interaction.response.send_message(
            content=interaction.user.mention,
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @welcome_group.command(name="status", description="現在の設定")
    async def status(self, interaction: discord.Interaction) -> None:
        ch_id = _channel_id("WELCOME_CHANNEL_ID")
        rules = _channel_id("WELCOME_RULES_CHANNEL_ID")
        intro = _channel_id("WELCOME_INTRO_CHANNEL_ID")
        autoroles = _autorole_ids()
        banner = os.getenv("WELCOME_BANNER_URL", "")
        thumb = os.getenv("WELCOME_THUMBNAIL_URL", "")
        lines = [
            "📋 **Welcome設定**",
            f"・Welcomeチャンネル: <#{ch_id}>" if ch_id else "・Welcomeチャンネル: 未設定",
            f"・ルール: <#{rules}>" if rules else "・ルール: 未設定",
            f"・自己紹介: <#{intro}>" if intro else "・自己紹介: 未設定",
            f"・自動付与ロール: {len(autoroles)}個",
            f"・バナー画像: {'設定済み' if banner else '未設定'}",
            f"・サムネ画像: {'設定済み (override)' if thumb else 'ユーザーアイコン'}",
        ]
        if autoroles:
            for rid in autoroles:
                lines.append(f"  - <@&{rid}>")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @welcome_group.command(name="setbanner", description="バナー画像を設定（添付 or URL）")
    @app_commands.describe(
        image="バナー画像をアップロード",
        url="または画像URL",
    )
    async def setbanner(
        self,
        interaction: discord.Interaction,
        image: Optional[discord.Attachment] = None,
        url: Optional[str] = None,
    ) -> None:
        new_url: Optional[str] = None
        if image is not None:
            if not (image.content_type or "").startswith("image/"):
                await interaction.response.send_message(
                    "⚠️ 画像ファイルではありません", ephemeral=True
                )
                return
            new_url = image.url
        elif url:
            new_url = url.strip()
        else:
            # Clear
            _set_env_var("WELCOME_BANNER_URL", "")
            await interaction.response.send_message(
                "✅ バナー画像をクリアしました", ephemeral=True
            )
            return
        _set_env_var("WELCOME_BANNER_URL", new_url)
        await interaction.response.send_message(
            f"✅ バナー画像を設定しました\n{new_url}", ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WelcomeCog(bot))
