"""In-bot /guide command — interactive dropdown guide.

A single ephemeral message with a Select menu. Picking a feature swaps the
embed to that feature's explanation (mirrors features_guide.html). Read-only:
no actions are taken, it only explains. Complements the simpler /help.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

# key -> feature card. `access`: 全員 / Admin / 自動 etc. `lines`: body bullets.
FEATURES: dict[str, dict] = {
    "translate": {
        "label": "A. 自動翻訳",
        "emoji": "🌐",
        "title": "🌐 A. 自動翻訳 `/translate`",
        "color": 0x5865F2,
        "access": "Admin（/translate text も Admin）",
        "lines": [
            "有効化したチャンネルで英⇄日を双方向自動翻訳。英文→🇯🇵 / 日本文→🇺🇸 をreply",
            "プロバイダは DeepL Free と Claude を切替可能。`status` で月使用量を確認",
            "URL・絵文字のみ・2文字未満・Bot自身のreplyはスキップ",
            "",
            "`on` / `off` / `mode`(both・en2ja・ja2en) / `status` / `text`(即時翻訳)",
        ],
    },
    "suggest": {
        "label": "B. 意図判定アドバイス",
        "emoji": "🤖",
        "title": "🤖 B. 意図判定アドバイス `/suggest`",
        "color": 0xFEE75C,
        "access": "Admin",
        "lines": [
            "監視チャンネルの発言を Claude で分類し、商品問合せ・配送相談を検知して誘導文をreply",
            "商品問合せ → #ticket へ誘導 / 配送相談 → `/shipping`＋#shipping-guide へ誘導",
            "介入は誘導テキストのみ（能動アクションはしない）。同一メッセージへの反応は1回限り",
            "投稿者の言語に合わせて日本語／英語を出し分け",
            "",
            "`on` / `off` / `status`",
        ],
    },
    "shipping": {
        "label": "C. 送料計算",
        "emoji": "📦",
        "title": "📦 C. 送料計算 `/shipping`",
        "color": 0x57F287,
        "access": "全員（管理は /shippingadmin = Admin）",
        "lines": [
            "商品×数量×宛先国をドロップダウンで選ぶカート式UI（ephemeral、「📤 公開する」で共有可）",
            "料金は Google Sheets「比較表US基準」から取得し、DHL/FedEx の安い方を自動選択",
            "計算: 商品重量合計＋梱包材1kg → 0.5kg刻み切り上げ → 20kg超は自動分割発送で合算",
            "宛先はリージョン→国の二段選択＋🔍直接検索（80カ国以上）。JP/EN切替対応",
            "",
            "`/shipping`（パネル起動）/ `/shippingadmin reload`（再読込）",
        ],
    },
    "ticket": {
        "label": "D. チケット",
        "emoji": "🎫",
        "title": "🎫 D. チケット `/ticket`",
        "color": 0xEB459E,
        "access": "Admin（パネル操作は全員）",
        "lines": [
            "Mee6風。パネルの「🎫 チケットを開く」で `0001-username` 形式の個別チャンネルを自動作成",
            "開設者＋運営ロールのみ閲覧可。1ユーザー1チケット制限（重複時は既存を案内）",
            "🔒ボタンまたは `/ticket close` で5秒後に削除",
            "",
            "`/ticket panel` / `/ticket close`",
        ],
    },
    "welcome": {
        "label": "E. Welcome",
        "emoji": "🎊",
        "title": "🎊 E. Welcome `/welcome`",
        "color": 0xED4245,
        "access": "Admin",
        "lines": [
            "新規メンバー入室時にウェルカムEmbedを自動投稿（バナー・タイトル・色・本文を .env でカスタマイズ）",
            "オートロール（ロール自動付与）対応。本文にメンション等のプレースホルダ使用可",
            "",
            "`test`(プレビュー) / `status` / `setbanner`",
        ],
    },
    "giveaway": {
        "label": "F. Giveaway",
        "emoji": "🎉",
        "title": "🎉 F. Giveaway `/giveaway`",
        "color": 0xF1C40F,
        "access": "Admin / 専用ロール",
        "lines": [
            "時間指定の抽選。🎉ボタンで参加（再押下で取消）、期限切れで自動抽選・当選発表",
            "期間は `30s` `5m` `1h` `1d` `1d2h30m` 形式。当選人数・参加必須ロール・賞品画像を指定可",
            "",
            "`create` / `end` / `reroll` / `list`",
        ],
    },
    "auction": {
        "label": "G. オークション",
        "emoji": "🔨",
        "title": "🔨 G. オークション `/auction`",
        "color": 0xE67E22,
        "access": "Admin / 専用ロール（履歴・一覧は全員）",
        "lines": [
            "ボタン入札式の非同期オークション（JPY固定）。Forum / Text チャンネル両対応",
            "入札ルール: 現最高値の＋5%以上 かつ 最小増分以上の上乗せ（小刻み入札防止）",
            "アンチスナイプ: 終了5分以内の入札で30秒自動延長（繰り返し可）",
            "最低落札価格（reserve）未達なら落札なし。落札時は取引チャンネル自動作成＋DM",
            "決済はBotでは行わず、取引チャンネルで人間が調整",
            "",
            "`create` / `end` / `cancel` / `list` / `history`",
        ],
    },
    "invite": {
        "label": "H. 招待トラッキング",
        "emoji": "🚪",
        "title": "🚪 H. 招待トラッキング `/invite`",
        "color": 0x3498DB,
        "access": "Admin / 専用ロール",
        "lines": [
            "入室時にどの招待コードが+1されたかを差分判定し、Welcome Embedと運営ログに記録",
            "複数Instagramアカウントの流入元判別に使用",
            "",
            "`list`(コード・使用回数・招待主) / `create`(リンク発行)",
        ],
    },
    "digest": {
        "label": "I. 週次ダイジェスト",
        "emoji": "📰",
        "title": "📰 I. 週次ダイジェスト `/digest`",
        "color": 0x9B59B6,
        "access": "Admin（自動: 毎週月曜 08:00 JST）",
        "lines": [
            "過去7日のサマリEmbedを自動投稿",
            "集計: 新規チケット数・翻訳回数・新規参加者・送料見積数・意図検出数・上位質問国・上位招待コード",
            "",
            "`now`(プレビュー) / `post`(即時投稿)",
        ],
    },
    "backup": {
        "label": "J. 設定バックアップ",
        "emoji": "💾",
        "title": "💾 J. 設定バックアップ `/backup`",
        "color": 0x95A5A6,
        "access": "Admin（自動: 毎日 03:00 JST）",
        "lines": [
            "全ロール・カテゴリ・チャンネル・権限・.env をJSONスナップショットして投稿",
            "秘密情報（TOKEN/KEY/SECRET系）は自動マスク。誤削除・誤変更時の復元用",
            "",
            "`now`(即時スナップショット)",
        ],
    },
    "health": {
        "label": "K. ヘルスチェック",
        "emoji": "🩺",
        "title": "🩺 K. ヘルスチェック `/health`",
        "color": 0x1ABC9C,
        "access": "Admin",
        "lines": [
            "Bot状態をephemeral表示: uptime・レイテンシ・guild数・読込cog数",
            "API予算: Anthropic月予算消費率／DeepL月文字数。過去24時間のイベント集計も",
            "",
            "`/health`",
        ],
    },
    "faq": {
        "label": "L. FAQ",
        "emoji": "❓",
        "title": "❓ L. FAQ `/faq`",
        "color": 0x2ECC71,
        "access": "全員（管理は /faqadmin = Admin）",
        "lines": [
            "JP/EN両対応のナレッジベース。海外バイヤーの定型質問を自己解決させる",
            "ユーザーのDiscord言語設定で自動切替。slugはautocomplete対応",
            "",
            "`show` / `list` ・ `/faqadmin add` / `remove` / `show`",
        ],
    },
    "safety": {
        "label": "M. Safety nets",
        "emoji": "🛡️",
        "title": "🛡️ M. Safety nets `/safety`",
        "color": 0x607D8B,
        "access": "Admin（削除・キック・自動BANは一切しない）",
        "lines": [
            "介入最小化を保った3つの防衛機構",
            "① 新規アカウント警告: 作成7日未満の入室者を運営チャンネルへ通知（判断は人間）",
            "② Auditログミラー: ch/ロール/メンバー等の変更を運営チャンネルへ転送（既定ON）",
            "③ PII検知: クレカ番号・マイナンバー検知時に本人へDMで削除推奨（既定OFF）",
            "",
            "`/safety status`",
        ],
    },
    "fuel": {
        "label": "N. 燃油サーチャージ",
        "emoji": "⛽",
        "title": "⛽ N. 燃油サーチャージ `/fuelsurcharge`",
        "color": 0xD35400,
        "access": "Admin（自動: 毎週月曜 04:00 JST）",
        "lines": [
            "DHL / FedEx 日本ページから燃油サーチャージ%を自動取得しキャッシュ",
            "取得失敗時は前回値を維持し、運営チャンネルへ警告",
            "",
            "`refresh`(即時取得) / `show`(現在値表示)",
        ],
    },
    "dashboard": {
        "label": "O. 管理ダッシュボード (Web)",
        "emoji": "🖥️",
        "title": "🖥️ O. 管理ダッシュボード（Web）",
        "color": 0x34495E,
        "access": "運営（ブラウザ）",
        "lines": [
            "ブラウザからBotの設定・状態を管理するWeb UI（Railwayのwebプロセス）",
            "オークション既定値・画像転送設定・Giveaway等の管理、Discord連携認証",
        ],
    },
}

_ORDER = list(FEATURES.keys())


def _build_overview() -> discord.Embed:
    embed = discord.Embed(
        title="📚 サポートBot — 機能ガイド",
        description=(
            "下のメニューから機能を選ぶと、詳しい説明が表示されます。\n"
            "このメッセージはあなたにだけ見えています（ephemeral）。\n\n"
            "**機能一覧**\n"
            + "\n".join(f"{f['emoji']} {f['label']}" for f in FEATURES.values())
        ),
        color=0x5865F2,
    )
    embed.set_footer(text="多くの管理コマンドはAdmin権限必須 / /shipping・/faq は全員可")
    return embed


def _build_feature(key: str) -> discord.Embed:
    f = FEATURES[key]
    embed = discord.Embed(
        title=f["title"],
        description="\n".join(f["lines"]),
        color=f["color"],
    )
    embed.add_field(name="権限", value=f["access"], inline=False)
    embed.set_footer(text="メニューで他の機能も見られます")
    return embed


class GuideSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(label="📚 概要 (一覧に戻る)", value="__overview__")
        ] + [
            discord.SelectOption(
                label=FEATURES[k]["label"],
                value=k,
                emoji=FEATURES[k]["emoji"],
            )
            for k in _ORDER
        ]
        super().__init__(
            placeholder="機能を選んでください…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        choice = self.values[0]
        if choice == "__overview__":
            embed = _build_overview()
        else:
            embed = _build_feature(choice)
        await interaction.response.edit_message(embed=embed, view=self.view)


class GuideView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=300)
        self.add_item(GuideSelect())


class GuideCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="guide",
        description="機能ガイドをメニューで表示 / Interactive feature guide",
    )
    async def guide_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=_build_overview(), view=GuideView(), ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GuideCog(bot))
