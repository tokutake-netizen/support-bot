"""Feature L: FAQ knowledge base.

非エンジニアの運営も触れる FAQ。`/faq <slug>` で定型回答を出力。
ja/en の両方持ち、ユーザーロケールで出し分け。

データ: `deployments/<server>/data/faqs.json`
  {
    "<slug>": {
      "ja": "...日本語の本文...",
      "en": "...English body...",
      "tags": ["shipping", "送料", ...],
      "added_by": <user_id>,
      "updated_at": "<iso>"
    },
    ...
  }

Commands:
  /faq <slug>              - 誰でも実行。一致する FAQ を ephemeral 返信
  /faq list                - 全 slug 列挙（誰でも）
  /faqadmin add            - admin
  /faqadmin remove         - admin
  /faqadmin show           - admin（生データ確認）

このcogは「能動アクションせずアドバイス止まり」の方針に従い、suggesterから自動投下したりはしない。
ユーザーが明示的に `/faq` を打った時のみ反応。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from services import storage
from services.i18n import get_ui_lang

log = logging.getLogger(__name__)

FAQS_FILE = "faqs.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    return storage.load(FAQS_FILE, default={})


def _save(data: dict) -> None:
    storage.save(FAQS_FILE, data)


def find_faqs_by_tag(tag: str, limit: int = 3) -> list[str]:
    """Return slugs of FAQs tagged with `tag` (case-insensitive). Public helper —
    used by suggester.py to point intent-detected users at relevant FAQs."""
    if not tag:
        return []
    tag_l = tag.lower()
    out: list[str] = []
    for slug, entry in _load().items():
        tags_l = [str(t).lower() for t in entry.get("tags", [])]
        if tag_l in tags_l:
            out.append(slug)
            if len(out) >= limit:
                break
    return out


def _user_lang(interaction: discord.Interaction) -> str:
    locale = str(interaction.locale) if interaction.locale else ""
    return "ja" if locale.startswith("ja") else "en"


async def _slug_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    data = _load()
    cur = (current or "").lower()
    out: list[app_commands.Choice[str]] = []
    for slug, entry in data.items():
        hay = slug.lower() + " " + " ".join(entry.get("tags", [])).lower()
        if cur in hay:
            label = slug
            if entry.get("tags"):
                label = f"{slug} — {', '.join(entry['tags'][:3])}"
            out.append(app_commands.Choice(name=label[:100], value=slug))
        if len(out) >= 25:
            break
    return out


class FaqCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    faq_group = app_commands.Group(name="faq", description="FAQ / よくある質問")

    @faq_group.command(name="show", description="Show a FAQ entry by slug")
    @app_commands.describe(slug="Entry slug (autocomplete suggests matches)")
    @app_commands.autocomplete(slug=_slug_autocomplete)
    async def show(self, interaction: discord.Interaction, slug: str) -> None:
        data = _load()
        entry = data.get(slug)
        if not entry:
            # Fuzzy fallback: try case-insensitive then tag match
            slug_l = slug.lower()
            for k, v in data.items():
                if k.lower() == slug_l:
                    entry = v
                    break
                if slug_l in [t.lower() for t in v.get("tags", [])]:
                    entry = v
                    break
        if not entry:
            await interaction.response.send_message(
                f"⚠️ No FAQ entry matched `{slug}`. Try `/faq list`.",
                ephemeral=True,
            )
            return
        lang = _user_lang(interaction)
        body = entry.get(lang) or entry.get("en") or entry.get("ja") or "_(empty)_"
        await interaction.response.send_message(body, ephemeral=True)

    @faq_group.command(name="list", description="List all FAQ slugs")
    async def list_entries(self, interaction: discord.Interaction) -> None:
        data = _load()
        if not data:
            await interaction.response.send_message(
                "_No FAQ entries yet. An admin can add one with `/faqadmin add`._",
                ephemeral=True,
            )
            return
        lines = ["📚 **FAQ entries**"]
        for slug, entry in sorted(data.items()):
            tags = ", ".join(entry.get("tags", [])) or "—"
            lines.append(f"・`{slug}` — {tags}")
        await interaction.response.send_message(
            "\n".join(lines)[:1900], ephemeral=True
        )

    # ============== Admin CRUD ==============

    faqadmin_group = app_commands.Group(
        name="faqadmin",
        description="FAQ entry management (admin)",
        default_permissions=discord.Permissions(administrator=True),
    )

    @faqadmin_group.command(name="add", description="Add or replace a FAQ entry")
    @app_commands.describe(
        slug="Short identifier (lowercase, hyphens). E.g. 'shipping'",
        ja="Japanese body (use \\n for newlines)",
        en="English body (use \\n for newlines)",
        tags="Comma-separated tags (optional)",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        slug: str,
        ja: str,
        en: str,
        tags: Optional[str] = None,
    ) -> None:
        slug_clean = slug.strip().lower()
        if not slug_clean or not slug_clean.replace("-", "").replace("_", "").isalnum():
            await interaction.response.send_message(
                "⚠️ slug は英数字とハイフン/アンダースコアのみ。例: `shipping`, `psa-grading`",
                ephemeral=True,
            )
            return
        data = _load()
        entry = data.get(slug_clean, {})
        entry.update({
            "ja": ja.replace("\\n", "\n"),
            "en": en.replace("\\n", "\n"),
            "tags": [t.strip() for t in (tags or "").split(",") if t.strip()],
            "added_by": interaction.user.id,
            "updated_at": _now_iso(),
        })
        data[slug_clean] = entry
        _save(data)
        log.info("faq: %s saved by user_id=%s", slug_clean, interaction.user.id)
        await interaction.response.send_message(
            f"✅ Saved FAQ `{slug_clean}`. Tags: {', '.join(entry['tags']) or '—'}",
            ephemeral=True,
        )

    @faqadmin_group.command(name="remove", description="Remove a FAQ entry")
    @app_commands.describe(slug="Slug to delete")
    @app_commands.autocomplete(slug=_slug_autocomplete)
    async def remove(self, interaction: discord.Interaction, slug: str) -> None:
        data = _load()
        if slug not in data:
            await interaction.response.send_message(
                f"⚠️ No entry named `{slug}`.", ephemeral=True
            )
            return
        del data[slug]
        _save(data)
        log.info("faq: %s removed by user_id=%s", slug, interaction.user.id)
        await interaction.response.send_message(
            f"🗑️ Removed FAQ `{slug}`.", ephemeral=True
        )

    @faqadmin_group.command(name="show", description="Show raw FAQ data (admin debug)")
    @app_commands.describe(slug="Slug to inspect")
    @app_commands.autocomplete(slug=_slug_autocomplete)
    async def show_raw(self, interaction: discord.Interaction, slug: str) -> None:
        data = _load()
        entry = data.get(slug)
        if not entry:
            await interaction.response.send_message(
                f"⚠️ No entry `{slug}`.", ephemeral=True
            )
            return
        ja = entry.get("ja", "")
        en = entry.get("en", "")
        tags = ", ".join(entry.get("tags", [])) or "—"
        updated = entry.get("updated_at", "—")
        body = (
            f"📄 **`{slug}`** (updated {updated})\n"
            f"Tags: {tags}\n\n"
            f"**🇯🇵 ja**:\n```\n{ja[:500]}\n```\n"
            f"**🇺🇸 en**:\n```\n{en[:500]}\n```"
        )
        await interaction.response.send_message(body[:1900], ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FaqCog(bot))
