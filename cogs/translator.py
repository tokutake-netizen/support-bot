"""Feature A: Bidirectional EN<->JA auto translation cog."""
from __future__ import annotations

import logging
import os
import re
from typing import Literal, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands
from langdetect import DetectorFactory, detect_langs
from langdetect.lang_detect_exception import LangDetectException

from services import storage
from services.claude_client import ClaudeClient
from services.deepl_client import DeeplClient
from services.i18n import normalize_locale, t

DetectorFactory.seed = 0
log = logging.getLogger(__name__)

JA_FLAG = "🇯🇵"
EN_FLAG = "🇺🇸"
PROCESSED_PREFIXES = (JA_FLAG + " ", EN_FLAG + " ")

# Skip patterns
URL_RE = re.compile(r"https?://\S+")
MENTION_RE = re.compile(r"<[@#:][^>]+>|@everyone|@here")
EMOJI_ONLY_RE = re.compile(r"^[\s\W_]+$", re.UNICODE)
CODEBLOCK_RE = re.compile(r"```[\s\S]*?```")

Mode = Literal["both", "en2ja", "ja2en", "off"]
SYSTEM_PROMPT_EN_TO_JA = (
    "You are a professional translator. Translate the user's English text into natural, "
    "fluent Japanese. Output only the translation, no preface or explanation. "
    "Keep proper nouns, code blocks, URLs, mentions and emojis untouched."
)
SYSTEM_PROMPT_JA_TO_EN = (
    "You are a professional translator. Translate the user's Japanese text into natural, "
    "fluent English. Output only the translation, no preface or explanation. "
    "Keep proper nouns, code blocks, URLs, mentions and emojis untouched."
)


class TranslatorCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.provider = os.getenv("TRANSLATE_PROVIDER", "deepl").strip().lower()
        self.budget_usd = float(os.getenv("MONTHLY_BUDGET_USD", "10"))
        self.min_conf = float(os.getenv("TRANSLATE_MIN_CONFIDENCE", "0.85"))
        self.default_mode: Mode = os.getenv("TRANSLATE_DEFAULT_MODE", "both")  # type: ignore[assignment]

        # Lazy clients - only init the one we need
        self.claude: Optional[ClaudeClient] = None
        self.deepl: Optional[DeeplClient] = None
        if self.provider == "deepl":
            try:
                self.deepl = DeeplClient()
                log.info("Translator: using DeepL (free tier=%s)", self.deepl.is_free_tier())
            except Exception:
                log.exception("DeepL init failed, falling back to Claude")
                self.provider = "claude"
        if self.provider == "claude" or self.deepl is None:
            try:
                self.claude = ClaudeClient()
                log.info("Translator: using Claude")
            except Exception:
                log.exception("Claude init failed - translator disabled")

        # Static channel IDs from .env
        ids_str = os.getenv("TRANSLATE_CHANNEL_IDS", "")
        self.static_channels = {int(x) for x in ids_str.split(",") if x.strip().isdigit()}
        cat_str = os.getenv("TICKET_CATEGORY_IDS", "")
        self.ticket_categories = {int(x) for x in cat_str.split(",") if x.strip().isdigit()}

        # Per-channel state {channel_id: mode}
        self.channel_modes: dict[str, Mode] = storage.load("translate_channels.json", default={})

        self._stopped_for_budget = False

    # ---------- helpers ----------
    def _channel_mode(self, channel: Union[discord.abc.GuildChannel, discord.Thread]) -> Optional[Mode]:
        cid = channel.id
        # explicit user setting wins
        if str(cid) in self.channel_modes:
            return self.channel_modes[str(cid)]
        if cid in self.static_channels:
            return self.default_mode
        # ticket category check
        parent_id = getattr(channel, "category_id", None)
        if parent_id and parent_id in self.ticket_categories:
            return self.default_mode
        return None

    def _set_channel_mode(self, channel_id: int, mode: Optional[Mode]) -> None:
        if mode is None or mode == "off":
            self.channel_modes.pop(str(channel_id), None)
        else:
            self.channel_modes[str(channel_id)] = mode
        storage.save("translate_channels.json", self.channel_modes)

    @staticmethod
    def _strip_for_detect(text: str) -> str:
        text = CODEBLOCK_RE.sub(" ", text)
        text = URL_RE.sub(" ", text)
        text = MENTION_RE.sub(" ", text)
        return text.strip()

    @staticmethod
    def _is_processed_translation(text: str) -> bool:
        return text.startswith(PROCESSED_PREFIXES)

    # ---------- main listener ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        content = message.content or ""
        if not content or content.startswith(("!", "/")):
            return
        if self._is_processed_translation(content):
            return
        if len(content) < 2 or len(content) > 2000:
            return

        mode = self._channel_mode(message.channel)
        if mode is None or mode == "off":
            return

        if self._stopped_for_budget:
            return
        # Budget guard only meaningful for Claude
        if self.provider == "claude" and ClaudeClient.monthly_usd_used() >= self.budget_usd:
            await self._notify_budget_exceeded()
            return

        stripped = self._strip_for_detect(content)
        if len(stripped) < 2 or EMOJI_ONLY_RE.match(stripped):
            return

        try:
            langs = detect_langs(stripped)
        except LangDetectException:
            return
        if not langs:
            return
        top = langs[0]
        if top.prob < self.min_conf:
            return
        lang = top.lang  # 'en', 'ja', etc.

        if lang == "en":
            if mode not in ("both", "en2ja"):
                return
            target_lang = "ja"
        elif lang == "ja":
            if mode not in ("both", "ja2en"):
                return
            target_lang = "en"
        else:
            return

        translated = await self._do_translate(content, target_lang)
        if translated is None:
            try:
                await message.add_reaction("⚠️")
            except discord.HTTPException:
                pass
            return

        flag = JA_FLAG if target_lang == "ja" else EN_FLAG
        from services import digest_store
        digest_store.append("translation", {"direction": target_lang, "chars": len(content)})
        try:
            await message.reply(
                f"{flag} {translated}",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.exception("reply failed")

    async def _do_translate(self, text: str, target_lang: str) -> Optional[str]:
        """Translate via the configured provider. Returns None on failure."""
        try:
            if self.provider == "deepl" and self.deepl:
                src = "EN" if target_lang == "ja" else "JA"
                tgt = "JA" if target_lang == "ja" else "EN-US"
                r = await self.deepl.translate(text, target_lang=tgt, source_lang=src)
                return r.text
            if self.claude:
                sys_prompt = SYSTEM_PROMPT_EN_TO_JA if target_lang == "ja" else SYSTEM_PROMPT_JA_TO_EN
                r = await self.claude.chat(system=sys_prompt, user=text, max_tokens=1024)
                return r.text
        except Exception:
            log.exception("translation failed (provider=%s)", self.provider)
        return None

    async def _notify_budget_exceeded(self) -> None:
        if self._stopped_for_budget:
            return
        self._stopped_for_budget = True
        message = t("translator.budget_exceeded", "ja", budget=f"{self.budget_usd:.2f}")

        # Prefer posting to moderator channel; fallback to admin DM.
        ch_id = os.getenv("MODERATOR_CHANNEL_ID")
        if ch_id and ch_id.isdigit():
            try:
                channel = self.bot.get_channel(int(ch_id)) or await self.bot.fetch_channel(int(ch_id))
                await channel.send(message)
                return
            except Exception:
                log.exception("moderator channel post failed")

        admin_id = os.getenv("ADMIN_USER_ID")
        if not admin_id:
            return
        try:
            user = await self.bot.fetch_user(int(admin_id))
            await user.send(message)
        except Exception:
            log.exception("budget DM failed")

    # ---------- slash commands ----------
    translate_group = app_commands.Group(
        name="translate",
        description="自動翻訳の設定 / Auto-translation settings",
        default_permissions=discord.Permissions(administrator=True),
    )

    @translate_group.command(name="on", description="このチャンネルの自動翻訳を有効化")
    async def translate_on(self, interaction: discord.Interaction) -> None:
        self._set_channel_mode(interaction.channel_id, self.default_mode)
        lang = normalize_locale(str(interaction.locale))
        await interaction.response.send_message(
            t("translator.cmd_on", lang, ch=interaction.channel_id), ephemeral=True
        )

    @translate_group.command(name="off", description="このチャンネルの自動翻訳を停止")
    async def translate_off(self, interaction: discord.Interaction) -> None:
        self._set_channel_mode(interaction.channel_id, None)
        lang = normalize_locale(str(interaction.locale))
        await interaction.response.send_message(
            t("translator.cmd_off", lang, ch=interaction.channel_id), ephemeral=True
        )

    @translate_group.command(name="mode", description="翻訳方向を変更")
    @app_commands.choices(direction=[
        app_commands.Choice(name="both 双方向", value="both"),
        app_commands.Choice(name="en2ja 英→日のみ", value="en2ja"),
        app_commands.Choice(name="ja2en 日→英のみ", value="ja2en"),
    ])
    async def translate_mode(
        self, interaction: discord.Interaction, direction: app_commands.Choice[str]
    ) -> None:
        self._set_channel_mode(interaction.channel_id, direction.value)  # type: ignore[arg-type]
        lang = normalize_locale(str(interaction.locale))
        await interaction.response.send_message(
            t("translator.cmd_mode", lang, ch=interaction.channel_id, mode=direction.value),
            ephemeral=True,
        )

    @translate_group.command(name="status", description="翻訳ON チャンネル一覧")
    async def translate_status(self, interaction: discord.Interaction) -> None:
        lang = normalize_locale(str(interaction.locale))
        lines = [t("translator.cmd_status", lang)]
        lines.append(f"プロバイダ: `{self.provider}`")
        for cid, mode in self.channel_modes.items():
            lines.append(f"・ <#{cid}> : `{mode}`")
        for cid in self.static_channels:
            if str(cid) not in self.channel_modes:
                lines.append(f"・ <#{cid}> : `{self.default_mode}` (env)")
        for cat in self.ticket_categories:
            lines.append(f"・ Tickets配下 (カテゴリ <#{cat}> 動的) : `{self.default_mode}`")

        # Usage stats per provider
        if self.provider == "deepl" and self.deepl:
            local = DeeplClient.monthly_chars_used()
            try:
                remote = self.deepl.get_remote_usage()
                lines.append(
                    f"\n📊 DeepL: 今月 (BOT記録) `{local:,}` 文字 / "
                    f"DeepL残量 `{remote['character_count']:,} / {remote['character_limit']:,}` chars"
                )
            except Exception:
                lines.append(f"\n📊 DeepL: 今月 (BOT記録) `{local:,}` 文字")
        else:
            used = ClaudeClient.monthly_usd_used()
            lines.append(f"\n💴 Claude: 今月使用 `${used:.4f}` / 予算 `${self.budget_usd:.2f}`")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @translate_group.command(name="text", description="任意の文字列を即時翻訳")
    @app_commands.describe(text="翻訳したい文字列", direction="既定: 自動判定")
    @app_commands.choices(direction=[
        app_commands.Choice(name="自動判定 auto", value="auto"),
        app_commands.Choice(name="英→日 en2ja", value="en2ja"),
        app_commands.Choice(name="日→英 ja2en", value="ja2en"),
    ])
    async def translate_text(
        self,
        interaction: discord.Interaction,
        text: str,
        direction: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        target_lang = "ja"
        if direction is None or direction.value == "auto":
            try:
                langs = detect_langs(text)
                lang_code = langs[0].lang if langs else "en"
                target_lang = "ja" if lang_code == "en" else "en"
            except Exception:
                target_lang = "ja"
        else:
            target_lang = "ja" if direction.value == "en2ja" else "en"

        translated = await self._do_translate(text, target_lang)
        if translated is None:
            await interaction.followup.send("⚠️ 翻訳に失敗しました", ephemeral=True)
            return
        flag = JA_FLAG if target_lang == "ja" else EN_FLAG
        await interaction.followup.send(f"{flag} {translated}", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TranslatorCog(bot))
