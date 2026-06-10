"""Feature B: Intent classification + advisory reply for free-talk channel."""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands
from langdetect import DetectorFactory, detect

from services import storage
from services.claude_client import ClaudeClient
from services.i18n import normalize_locale, t

DetectorFactory.seed = 0
log = logging.getLogger(__name__)

CLASSIFY_SYSTEM = (
    "You classify Discord community messages. Respond with JSON ONLY in this format:\n"
    '{"label": "product_inquiry|shipping_inquiry|general_chat|other", '
    '"confidence": 0.0-1.0, "reason": "short reason"}\n\n'
    "Labels:\n"
    "- product_inquiry: asking about product stock/price/condition/reservation/purchase intent\n"
    "- shipping_inquiry: asking about shipping cost/tracking/customs/delivery time\n"
    "- general_chat: greetings/casual conversation/sharing\n"
    "- other: anything else (announcements, off-topic, errors)\n\n"
    "Output ONLY the JSON. No markdown, no preface."
)

URL_RE = re.compile(r"https?://\S+")

Label = Literal["product_inquiry", "shipping_inquiry", "general_chat", "other"]


class SuggesterCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.claude = ClaudeClient()
        self.min_conf = float(os.getenv("SUGGEST_MIN_CONFIDENCE", "0.70"))
        community_id = os.getenv("COMMUNITY_CHANNEL_ID", "")
        self.community_channel_id = int(community_id) if community_id.isdigit() else None

        self.product_inquiry_ch = os.getenv("PRODUCT_INQUIRY_CHANNEL_ID", "")
        self.shipping_guide_ch = os.getenv("SHIPPING_GUIDE_CHANNEL_ID", "")

        # Advice texts may contain newlines; .env stores them as literal "\n"
        # which we decode here so the reply actually wraps in Discord.
        def _adv(name: str) -> str:
            return os.getenv(name, "").replace("\\n", "\n").format(
                PRODUCT_INQUIRY_CHANNEL_ID=self.product_inquiry_ch,
                SHIPPING_GUIDE_CHANNEL_ID=self.shipping_guide_ch,
            )
        self.product_advice_ja = _adv("PRODUCT_ADVICE_TEXT")
        self.shipping_advice_ja = _adv("SHIPPING_ADVICE_TEXT")
        self.product_advice_en = _adv("PRODUCT_ADVICE_TEXT_EN")
        self.shipping_advice_en = _adv("SHIPPING_ADVICE_TEXT_EN")

        state = storage.load("suggest_state.json", default={"enabled": True, "advised": []})
        self.enabled: bool = bool(state.get("enabled", True))
        advised_list = state.get("advised", [])
        if isinstance(advised_list, list):
            self.advised_ids: set = set(advised_list[-200:])
        else:
            self.advised_ids = set()

    def _save_state(self) -> None:
        storage.save("suggest_state.json", {
            "enabled": self.enabled,
            "advised": list(self.advised_ids)[-200:],
        })

    @staticmethod
    def _detect_lang(text: str) -> str:
        try:
            stripped = URL_RE.sub(" ", text)
            return detect(stripped)
        except Exception:
            return "ja"

    def _related_faq_hint(self, label: str, is_en: bool) -> str:
        """Look up FAQs tagged with the intent label and produce a short hint line.

        Returns empty string when no FAQs match — so the existing advice text
        stays untouched in fresh deployments. Tag convention:
          - product_inquiry → FAQs tagged `product`
          - shipping_inquiry → FAQs tagged `shipping`
        Admins control participation just by tagging their FAQ entries.
        """
        tag = "product" if label == "product_inquiry" else "shipping"
        try:
            from cogs.faq import find_faqs_by_tag
        except ImportError:
            return ""
        slugs = find_faqs_by_tag(tag, limit=3)
        if not slugs:
            return ""
        # 短く控えめに。slug を inline code で並べるだけ。
        slug_list = ", ".join(f"`{s}`" for s in slugs)
        if is_en:
            return f"💡 You might also find answers via `/faq`: {slug_list}"
        return f"💡 `/faq` でも答えが見つかるかも： {slug_list}"

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not self.enabled or message.author.bot or not message.guild:
            return
        if self.community_channel_id is None or message.channel.id != self.community_channel_id:
            return
        if not message.content or len(message.content) < 4:
            return
        if message.id in self.advised_ids:
            return

        try:
            reply = await self.claude.chat(
                system=CLASSIFY_SYSTEM, user=message.content, max_tokens=200
            )
        except Exception:
            log.exception("classify failed")
            return

        try:
            parsed = json.loads(reply.text)
            label: Label = parsed.get("label", "other")  # type: ignore[assignment]
            confidence = float(parsed.get("confidence", 0))
        except Exception:
            log.warning("Could not parse classifier output: %s", reply.text)
            return

        if confidence < self.min_conf:
            return
        if label not in ("product_inquiry", "shipping_inquiry"):
            return

        # detect user's language for the advice text
        msg_lang = self._detect_lang(message.content)
        is_en = msg_lang == "en"

        if label == "product_inquiry":
            advice = self.product_advice_en if is_en else self.product_advice_ja
        else:
            advice = self.shipping_advice_en if is_en else self.shipping_advice_ja

        # 関連 FAQ があれば末尾に1行だけ添える（介入最小化の範囲内：誘導のみ）
        faq_hint = self._related_faq_hint(label, is_en)

        ai_note = t("suggester.ai_note", "en" if is_en else "ja")
        body = f"{message.author.mention} {advice}"
        if faq_hint:
            body += f"\n{faq_hint}"
        body += f"\n_{ai_note} (confidence {confidence:.2f})_"

        try:
            await message.reply(
                body, mention_author=False, allowed_mentions=discord.AllowedMentions.none()
            )
            self.advised_ids.add(message.id)
            self._save_state()
            from services import digest_store
            digest_store.append("intent_detected", {"label": label, "lang": "en" if is_en else "ja"})
        except discord.HTTPException:
            log.exception("suggest reply failed")

    # ---------- slash commands ----------
    suggest_group = app_commands.Group(
        name="suggest",
        description="意図判定アドバイス機能の設定 / Intent advisory settings",
        default_permissions=discord.Permissions(administrator=True),
    )

    @suggest_group.command(name="on", description="意図判定＆アドバイスを有効化")
    async def suggest_on(self, interaction: discord.Interaction) -> None:
        self.enabled = True
        self._save_state()
        lang = normalize_locale(str(interaction.locale))
        await interaction.response.send_message(
            t("suggester.panel_on", lang, ch=self.community_channel_id or 0), ephemeral=True
        )

    @suggest_group.command(name="off", description="意図判定＆アドバイスを停止")
    async def suggest_off(self, interaction: discord.Interaction) -> None:
        self.enabled = False
        self._save_state()
        lang = normalize_locale(str(interaction.locale))
        await interaction.response.send_message(
            t("suggester.panel_off", lang, ch=self.community_channel_id or 0), ephemeral=True
        )

    @suggest_group.command(name="status", description="現在の有効状態")
    async def suggest_status(self, interaction: discord.Interaction) -> None:
        lang = normalize_locale(str(interaction.locale))
        state = "ON 🟢" if self.enabled else "OFF ⚪"
        await interaction.response.send_message(
            t("suggester.panel_status", lang, state=state, ch=self.community_channel_id or 0),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SuggesterCog(bot))
