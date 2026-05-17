"""Feature M: Safety nets — minimal-intervention defenses.

3 つの軽い防衛機構を1つの cog にまとめる。すべて「アドバイス止まり」原則を維持：
削除・キックは行わず、運営通知 or 当人へのDM のみ。

1. **新規アカウント警告** (on_member_join)
   - アカウント作成 < NEW_ACCOUNT_THRESHOLD_DAYS（既定7日）のメンバーが入室
   - MODERATOR_CHANNEL_ID に注意喚起を投稿
   - キックや自動BANは一切しない

2. **Audit ログミラー** (on_audit_log_entry_create)
   - チャンネル／ロールの create/delete/update、メンバーのkick/ban、ロール変更等を
     MODERATOR_CHANNEL_ID にミラー投稿
   - 複数モデで運営している時の「誰がいつ何した」可視化
   - bot に View Audit Log 権限が必要

3. **PII 検知 DM** (on_message)
   - クレジットカード番号（13〜19桁、Luhn検証）
   - 日本マイナンバー（12桁 + キーワード文脈）
   - 検出時：チャンネルからは消さない、投稿者にDMで削除推奨
   - 既定: OFF（SAFETY_PII_ENABLED=1 で ON）

env:
  NEW_ACCOUNT_THRESHOLD_DAYS=7
  SAFETY_AUDIT_MIRROR=1
  SAFETY_PII_ENABLED=0
  MODERATOR_CHANNEL_ID=<必須>
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)


def _env_int(key: str, fallback: int) -> int:
    raw = (os.getenv(key) or "").strip()
    try:
        return int(raw) if raw else fallback
    except ValueError:
        return fallback


def _env_flag(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _mod_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    cid = (os.getenv("MODERATOR_CHANNEL_ID") or "").strip()
    if not cid.isdigit():
        return None
    ch = guild.get_channel(int(cid))
    return ch if isinstance(ch, discord.TextChannel) else None


# ============== PII detection ==============

# クレジットカード：13〜19桁、ハイフン/スペース許容
_CC_RE = re.compile(r"(?:\d[ -]?){12,18}\d")
# マイナンバー：12 連続数字（コンテキストキーワードと併用）
_MYNUMBER_RE = re.compile(r"\b\d{12}\b")
_MYNUMBER_KEYWORDS = ("マイナンバー", "個人番号", "my number", "mynumber")
# URL 内の数字を誤検知しないように
_URL_RE = re.compile(r"https?://\S+")


def _luhn_check(digits: str) -> bool:
    """Luhnアルゴリズムでカード番号の妥当性を確認。"""
    s = 0
    parity = len(digits) % 2
    for i, c in enumerate(digits):
        n = int(c)
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        s += n
    return s % 10 == 0


def _scan_pii(content: str) -> list[str]:
    """検出されたPIIの種別を返す（最大数件）。空 = クリーン。"""
    hits: list[str] = []
    # URLを除外
    cleaned = _URL_RE.sub(" ", content)

    for m in _CC_RE.finditer(cleaned):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_check(digits):
            hits.append("credit_card")
            break  # 1つで十分

    if any(kw in content.lower() for kw in _MYNUMBER_KEYWORDS):
        if _MYNUMBER_RE.search(cleaned):
            hits.append("mynumber")

    return hits


# ============== Audit log mirror ==============

# 興味のある action を絞り込み（議論ノイズを減らす）
_INTERESTING_ACTIONS = {
    discord.AuditLogAction.channel_create,
    discord.AuditLogAction.channel_delete,
    discord.AuditLogAction.channel_update,
    discord.AuditLogAction.overwrite_create,
    discord.AuditLogAction.overwrite_update,
    discord.AuditLogAction.overwrite_delete,
    discord.AuditLogAction.role_create,
    discord.AuditLogAction.role_delete,
    discord.AuditLogAction.role_update,
    discord.AuditLogAction.kick,
    discord.AuditLogAction.ban,
    discord.AuditLogAction.unban,
    discord.AuditLogAction.member_role_update,
    discord.AuditLogAction.member_update,  # nickname etc.
    discord.AuditLogAction.guild_update,
    discord.AuditLogAction.emoji_create,
    discord.AuditLogAction.emoji_delete,
}


def _format_audit_entry(entry: discord.AuditLogEntry) -> str:
    user = entry.user.mention if entry.user else "_unknown_"
    action = entry.action.name
    target = ""
    if entry.target is not None:
        try:
            target = f" → {entry.target}"
        except Exception:
            target = ""
    reason = f" _(reason: {entry.reason})_" if entry.reason else ""
    return f"🕵️ **{action}** by {user}{target}{reason}"


# ============== Cog ==============


class SafetyCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ---------- 1. New-account warning ----------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        threshold_days = _env_int("NEW_ACCOUNT_THRESHOLD_DAYS", 7)
        if threshold_days <= 0:
            return
        if member.created_at is None:
            return
        now = datetime.now(timezone.utc)
        age = now - member.created_at
        if age.days >= threshold_days:
            return
        ch = _mod_channel(member.guild)
        if ch is None:
            return
        try:
            embed = discord.Embed(
                title="🆕 New-account advisory",
                description=(
                    f"{member.mention} (`{member}`, id `{member.id}`) "
                    f"just joined."
                ),
                color=0xF0B232,
            )
            embed.add_field(
                name="Account age",
                value=f"{age.days}d {age.seconds // 3600}h "
                      f"(created <t:{int(member.created_at.timestamp())}:R>)",
                inline=False,
            )
            embed.add_field(
                name="Threshold",
                value=f"< {threshold_days}d → flagged for moderator review",
                inline=False,
            )
            embed.set_footer(
                text="No auto-action taken. Decide manually whether to monitor or take action."
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            await ch.send(embed=embed)
            log.info(
                "safety: new-account flag user=%s age_days=%d", member.id, age.days
            )
        except discord.HTTPException:
            log.exception("safety: new-account post failed")

    # ---------- 2. Audit log mirror ----------

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        if not _env_flag("SAFETY_AUDIT_MIRROR", True):
            return
        if entry.action not in _INTERESTING_ACTIONS:
            return
        # 自分（bot自身）の操作はノイズになるので除外
        if entry.user is not None and entry.user.id == self.bot.user.id:
            return
        ch = _mod_channel(entry.guild)
        if ch is None:
            return
        try:
            text = _format_audit_entry(entry)
            await ch.send(text[:1900])
        except discord.HTTPException:
            log.exception("safety: audit mirror post failed")

    # ---------- 3. PII detection ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not _env_flag("SAFETY_PII_ENABLED", False):
            return
        if message.author.bot or message.guild is None:
            return
        if not message.content:
            return
        hits = _scan_pii(message.content)
        if not hits:
            return
        # 投稿者へDM。チャンネルからは消さない。
        try:
            kind_label = {
                "credit_card": "credit card number",
                "mynumber": "Japanese MyNumber (個人番号)",
            }
            kinds = ", ".join(kind_label.get(h, h) for h in hits)
            body = (
                f"⚠️ **Sensitive info detected in your recent message**\n"
                f"In <#{message.channel.id}> you appear to have posted: **{kinds}**.\n\n"
                f"For your safety, please delete that message and never share "
                f"such information in public channels. If you need to share it "
                f"with the seller, do so through a private ticket or DM.\n\n"
                f"_This is an automated, private notice. No staff member has been "
                f"alerted; the channel was not modified._"
            )
            await message.author.send(body)
            log.info(
                "safety: PII DM sent user=%s kinds=%s msg=%s",
                message.author.id, kinds, message.id,
            )
        except discord.Forbidden:
            log.info(
                "safety: PII DM blocked by user=%s (DMs closed)", message.author.id
            )
        except discord.HTTPException:
            log.exception("safety: PII DM failed")

    # ---------- Admin status command ----------

    safety_group = app_commands.Group(
        name="safety",
        description="Safety nets status",
        default_permissions=discord.Permissions(administrator=True),
    )

    @safety_group.command(name="status", description="Show safety feature status")
    async def status(self, interaction: discord.Interaction) -> None:
        threshold = _env_int("NEW_ACCOUNT_THRESHOLD_DAYS", 7)
        audit = _env_flag("SAFETY_AUDIT_MIRROR", True)
        pii = _env_flag("SAFETY_PII_ENABLED", False)
        mod = (os.getenv("MODERATOR_CHANNEL_ID") or "").strip()
        lines = [
            "🛡️ **Safety nets status**",
            f"・New-account flag: **{'ON' if threshold > 0 else 'OFF'}** "
            f"(threshold = {threshold}d)",
            f"・Audit log mirror: **{'ON' if audit else 'OFF'}**",
            f"・PII detector → DM: **{'ON' if pii else 'OFF'}**",
            f"・Moderator channel: {'<#' + mod + '>' if mod.isdigit() else '_not set_'}",
        ]
        if not mod.isdigit():
            lines.append(
                "\n⚠️ `MODERATOR_CHANNEL_ID` is not configured — "
                "new-account and audit-mirror posts will be silently dropped."
            )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SafetyCog(bot))
