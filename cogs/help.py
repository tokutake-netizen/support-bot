"""In-bot /help command - quick command reference inside Discord."""
from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

CATEGORIES = {
    "translate": {
        "title": "🌐 翻訳 / Translation",
        "color": 0x5865F2,
        "lines": [
            "`/translate on` — このチャンネルの自動翻訳ON",
            "`/translate off` — 停止",
            "`/translate mode <both|en2ja|ja2en>` — 方向変更",
            "`/translate status` — 設定一覧＋使用量",
            "`/translate text <text>` — オンデマンド翻訳",
        ],
        "footer": "🇯🇵 = 英→日 / 🇺🇸 = 日→英. URL/絵文字のみ/2文字未満は無視.",
    },
    "suggest": {
        "title": "🤖 意図判定 / Intent advice",
        "color": 0xFEE75C,
        "lines": [
            "`/suggest on` — 自動アドバイスON",
            "`/suggest off` — 停止",
            "`/suggest status` — 状態確認",
        ],
        "footer": "全体交流会の発言から商品/配送相談を検知して誘導文をreply.",
    },
    "shipping": {
        "title": "📦 送料計算 / Shipping",
        "color": 0x57F287,
        "lines": [
            "`/shipping` — カート式パネル起動 (全員)",
            "`/shippingadmin reload` — スプシ再読込 (Admin)",
        ],
        "footer": "商品×数量×宛先 → DHL or FedEx の安い方を自動選択. 20kg超は分割発送.",
    },
    "ticket": {
        "title": "🎫 チケット / Ticket",
        "color": 0xEB459E,
        "lines": [
            "`/ticket panel` — チケット起票パネルを設置 (Admin)",
            "`/ticket close` — このチケットをクローズ (Admin)",
            "─",
            "**ユーザー操作**: パネルの「🎫 チケットを開く」を押下",
            "→ Created Tickets配下に `0001-name` 形式で個別チャンネル作成",
            "→ 1ユーザー1チケットまで, 重複時は既存リンクを案内",
        ],
        "footer": "クローズボタンで5秒後にチャンネル削除.",
    },
    "welcome": {
        "title": "🎊 Welcome",
        "color": 0xED4245,
        "lines": [
            "`/welcome test` — 自分にプレビュー (Admin)",
            "`/welcome status` — 設定確認",
            "`/welcome setbanner [image|url]` — バナー画像設定",
        ],
        "footer": "新規メンバー入室時に WELCOME_CHANNEL_ID へ自動投稿.",
    },
    "giveaway": {
        "title": "🎉 Giveaway",
        "color": 0xF1C40F,
        "lines": [
            "`/giveaway create prize duration [winners] [image]` — 抽選開始",
            "`/giveaway end message_id` — 早期終了",
            "`/giveaway reroll message_id [winners]` — 再抽選",
            "`/giveaway list` — 進行中一覧",
        ],
        "footer": "duration 例: 30s / 5m / 1h / 1d2h30m. 参加者は🎉ボタン押下.",
    },
}


def _build_overview() -> discord.Embed:
    embed = discord.Embed(
        title="📚 Support Bot — コマンドガイド",
        description=(
            "カテゴリ別に詳細を見るには:\n"
            "`/help category:translate` のように指定してください.\n\n"
            "**機能一覧**\n"
            "・🌐 `translate` 翻訳 (英⇄日)\n"
            "・🤖 `suggest` 意図判定\n"
            "・📦 `shipping` 送料計算\n"
            "・🎫 `ticket` チケット\n"
            "・🎊 `welcome` Welcome\n"
            "・🎉 `giveaway` 抽選"
        ),
        color=0x5865F2,
    )
    embed.set_footer(text="多くの管理コマンドはAdmin権限必須. /shipping は全員実行可.")
    return embed


def _build_category(key: str) -> Optional[discord.Embed]:
    cat = CATEGORIES.get(key)
    if not cat:
        return None
    embed = discord.Embed(
        title=cat["title"],
        description="\n".join(cat["lines"]),
        color=cat["color"],
    )
    embed.set_footer(text=cat.get("footer", ""))
    return embed


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="コマンドガイドを表示 / Show command help")
    @app_commands.describe(category="カテゴリ名 (translate, suggest, shipping, ticket, welcome, giveaway)")
    @app_commands.choices(
        category=[
            app_commands.Choice(name="overview (全体)", value="overview"),
            app_commands.Choice(name="🌐 translate 翻訳", value="translate"),
            app_commands.Choice(name="🤖 suggest 意図判定", value="suggest"),
            app_commands.Choice(name="📦 shipping 送料計算", value="shipping"),
            app_commands.Choice(name="🎫 ticket チケット", value="ticket"),
            app_commands.Choice(name="🎊 welcome ウェルカム", value="welcome"),
            app_commands.Choice(name="🎉 giveaway 抽選", value="giveaway"),
        ]
    )
    async def help_cmd(
        self,
        interaction: discord.Interaction,
        category: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        key = category.value if category else "overview"
        if key == "overview":
            embed = _build_overview()
        else:
            embed = _build_category(key)
            if embed is None:
                embed = _build_overview()
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCog(bot))
