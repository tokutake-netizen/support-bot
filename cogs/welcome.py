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


def _build_embed(
    member: discord.Member,
    used_invite: Optional[discord.Invite] = None,
) -> discord.Embed:
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

    # WELCOME_DESCRIPTION is stored in .env with literal "\n" escapes
    # (single-line .env values). Decode them back to real newlines before
    # rendering so multi-line templates wrap correctly in the embed.
    template = (os.getenv("WELCOME_DESCRIPTION") or (
        "🎉 **Welcome, {user_mention}!**\n\n"
        "・📜 Rules: {rules}\n"
        "・🙋 Introduce yourself: {intro}\n"
        "・🎫 Need help? Open a ticket in #📩ticket"
    )).replace("\\n", "\n")
    desc = template.format(
        user_mention=member.mention,
        user_name=member.name,
        guild_name=member.guild.name,
        member_count=member.guild.member_count,
        rules=rule_link,
        intro=intro_link,
    )

    embed = discord.Embed(title=title, description=desc, color=color)

    # Invite attribution (shown only when we successfully detected the source)
    if used_invite is not None:
        inviter = used_invite.inviter.mention if used_invite.inviter else "(unknown)"
        embed.add_field(
            name="✨ Invited via",
            value=f"`{used_invite.code}` by {inviter}",
            inline=False,
        )

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

    async def post_welcome(
        self,
        member: discord.Member,
        used_invite: Optional[discord.Invite] = None,
    ) -> None:
        """Post welcome embed + assign autoroles. Called by invite_tracker on join.

        Safe to call directly when invite_tracker is missing — used_invite just stays None.
        """
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
                embed=_build_embed(member, used_invite=used_invite),
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

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        # Fallback path: only fires if invite_tracker is not loaded.
        # When invite_tracker is loaded it calls post_welcome() directly with invite info.
        if self.bot.get_cog("InviteTrackerCog") is not None:
            return
        await self.post_welcome(member)

    welcome_group = app_commands.Group(
        name="welcome",
        description="Welcome tool",
        default_permissions=discord.Permissions(administrator=True),
    )

    @welcome_group.command(name="test", description="Preview the welcome message for yourself")
    async def test(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("⚠️ Guild-only command.", ephemeral=True)
            return
        embed = _build_embed(interaction.user)
        await interaction.response.send_message(
            content=interaction.user.mention,
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @welcome_group.command(name="status", description="Show current welcome settings")
    async def status(self, interaction: discord.Interaction) -> None:
        ch_id = _channel_id("WELCOME_CHANNEL_ID")
        rules = _channel_id("WELCOME_RULES_CHANNEL_ID")
        intro = _channel_id("WELCOME_INTRO_CHANNEL_ID")
        autoroles = _autorole_ids()
        banner = os.getenv("WELCOME_BANNER_URL", "")
        thumb = os.getenv("WELCOME_THUMBNAIL_URL", "")
        lines = [
            "📋 **Welcome settings**",
            f"・Welcome channel: <#{ch_id}>" if ch_id else "・Welcome channel: (not set)",
            f"・Rules: <#{rules}>" if rules else "・Rules: (not set)",
            f"・Introduce: <#{intro}>" if intro else "・Introduce: (not set)",
            f"・Auto roles: {len(autoroles)}",
            f"・Banner: {'set' if banner else '(not set)'}",
            f"・Thumbnail: {'override set' if thumb else 'user avatar'}",
        ]
        if autoroles:
            for rid in autoroles:
                lines.append(f"  - <@&{rid}>")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @welcome_group.command(name="setbanner", description="Set the banner image (attach or URL)")
    @app_commands.describe(
        image="Upload a banner image",
        url="Or specify an image URL",
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
                    "⚠️ The uploaded file is not an image.", ephemeral=True
                )
                return
            new_url = image.url
        elif url:
            new_url = url.strip()
        else:
            # Clear
            _set_env_var("WELCOME_BANNER_URL", "")
            await interaction.response.send_message(
                "✅ Banner cleared.", ephemeral=True
            )
            return
        _set_env_var("WELCOME_BANNER_URL", new_url)
        await interaction.response.send_message(
            f"✅ Banner set\n{new_url}", ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WelcomeCog(bot))
