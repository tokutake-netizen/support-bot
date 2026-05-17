# Support Bot — 仕様書

最終更新: 2026-05-17
対象: `Desktop/ClaudeCode/Discordbot/support_bot/` (稼働デプロイメント: `deployments/test/`)
リポジトリ: https://github.com/tokutake-netizen/support-bot

---

## 1. 概要

英語圏(英語ファースト)のオンライン トレカ販売 Discord サーバー向けサポート BOT、および**それを Web ブラウザから運用するための管理ダッシュボード**。

| 機能 | 役割 |
|---|---|
| translator | DeepL / Claude 自動翻訳 |
| suggester | Claude による意図判定 → 誘導 reply |
| shipping | Google Sheets ベースの送料計算 |
| ticket | Mee6 風のサポートチケット起票 |
| welcome | 新規入室時のウェルカム embed (招待元込み) |
| giveaway | ボタン式抽選ツール (ダッシュボードから作成可) |
| invite_tracker | 招待元の自動判定 + 招待リンク発行 |
| **auction** | 入札ボタン式のオークション(snipe 対策付き)+ 当選者用 deal ch 自動作成 |
| **digest** | 月曜朝に週次ダイジェスト embed を投稿 |
| **backup** | 毎日 03:00 にチャンネル/ロール/.env を JSON で保存 |
| **health** | `/health` で各サブシステムの健康状態を ephemeral 返却 |
| **dashboard** (FastAPI) | 上記 全部を Web から GUI 設定・運用 |

---

## 2. 言語ポリシー

- BOT 出力は全機能 **英語固定**(`i18n/ja.json` の `ticket` セクションは `en.json` をミラー)
- 翻訳機能だけは「英文 → 🇯🇵」「日本文 → 🇺🇸」を返す目的なので双方向出力
- per-feature override 用 env var が用意されている (`FORCE_UI_LANG_TICKET`, `FORCE_UI_LANG_SHIPPING`)
- ダッシュボード UI は**日本語**(運営=日本人想定)

---

## 3. コマンド一覧

`/help` で表示される 12 個の slash command 群。**Admin** は Discord の Administrator 権限保有者を指す。

| コマンド | 権限 | 使用可能 ch | 用途 |
|---|---|---|---|
| `/help` | 全員 | 任意 | コマンドガイド表示 |
| `/translate on/off/mode/status/text` | Admin | 任意 | 自動翻訳の制御 |
| `/suggest on/off/status` | Admin | 任意 | 意図判定の制御 |
| `/shipping` | 全員 | **#shipping-guide / #ticket / #request / 任意チケットch** | 送料計算パネル起動 |
| `/shippingadmin reload` | Admin | 任意 | スプシ再読込 |
| `/ticket panel` | Admin | 任意(#ticketに設置推奨) | チケット起票パネル設置 |
| `/ticket close` | Admin | チケットch内のみ | チケットクローズ |
| `/welcome test/status/setbanner` | Admin | 任意 | Welcome設定の確認・調整 |
| `/giveaway create/end/reroll/list` | Admin or `GIVEAWAY_MANAGER_ROLE_IDS` | **#raffle のみ** | 抽選の作成・運用 |
| `/invite list` | Admin | **invite ch のみ** | 招待コードと使用回数の一覧 |
| `/invite create` | Admin or `INVITE_CREATOR_ROLE_IDS` | 同上 | 招待リンク発行 |
| `/auction create/end/cancel/list/history` | Admin or `AUCTION_MANAGER_ROLE_IDS` | `ALLOW_CH_AUCTION` | オークションの作成・運用 |
| `/digest now/post` | Admin | 任意 | 週次ダイジェストの preview / 即時投稿 |
| `/backup now` | Admin | 任意 | 設定スナップショットを即時保存 |
| `/health` | Admin | 任意 | BOT の状態 ephemeral |

「使用可能 ch」は `services/channel_guard.py` が `.env` の `ALLOW_CH_<KEY>` / `ALLOW_CAT_<KEY>` を参照して enforce する。未設定キーは制限なし。

---

## 4. 自動動作 (slash command 非依存)

- **on_member_join**: `invite_tracker` が一元処理
  - 招待差分検出 → どの code が +1 したか確定
  - Welcome embed に「✨ Invited via `code` by @inviter」フィールドを差し込んで投稿
  - `MODERATOR_CHANNEL_ID`(または `INVITE_LOG_CHANNEL_ID`)へ監査ログ
  - `WELCOME_AUTOROLE_IDS` のロールを自動付与
  - digest 用に `member_join` イベント記録
- **on_invite_create / on_invite_delete**: invite キャッシュを差分維持
- **on_message** (translator): `TRANSLATE_CHANNEL_IDS` または `/translate on` 済 ch、および `TICKET_CATEGORY_IDS` 配下を対象に自動翻訳 reply。digest 用に `translation` 記録
- **on_message** (suggester): `COMMUNITY_CHANNEL_ID` を監視、Claude で `product_inquiry` / `shipping_inquiry` を検知すると誘導文 reply。digest 用に `intent_detected` 記録
- **背景タスク**:
  - giveaway: 30秒毎に期限切れ giveaway → 自動抽選+発表
  - auction: 30秒毎に終了時刻チェック → ending → ended で deal ch 自動作成
  - digest: 毎日 08:00 JST 起動、月曜のみ週次 embed を投稿(同日重複防止)
  - backup: 毎日 03:00 JST スナップショット投稿

---

## 5. チケット仕様

- 1 ユーザー 1 チケットまで(重複時は既存リンクを案内、`data/ticket_owners.json` で管理)
- チャンネル名: `{NNNN}-{username-ascii}` 形式(`0001-toco-123` 等)
- 配置先カテゴリ: `TICKET_CATEGORY_ID`
- 権限: 開設者 + `TICKET_STAFF_ROLE_IDS` のロール + bot のみ閲覧可
- panel embed: `CARD_GAME_CHANNEL_ID` が設定されていれば「#card-game で気になるカードを見つけたらここでオーダー」誘導文を表示
- welcome 文言: 「Thanks for opening this private chat with us! We're here to support your business...」(英語固定)
- 5 秒カウント後にチャンネル削除でクローズ

---

## 6. 送料計算仕様

- Google Sheets `比較表US基準` シートから DHL / FedEx の安い方を自動選択
- 合計商品重量 + 1kg(梱包材) を 0.5kg 刻みで切り上げ
- 20kg超は自動で分割発送(複数箱に料金合算)
- 商品マスタはコード内ハードコード(6 SKU: CASE/BOX/PSA/Single)
- 「比較表US基準」は 26 ブロック × 3 列構成、20kg分割・発送除外列あり
- ダッシュボードから設定する場合は **スプシ URL 貼り付けだけ**(ID 自動抽出)、サービスアカウントはプラットフォーム共有

---

## 7. Giveaway 仕様

- duration 書式: `30s`, `5m`, `1h`, `1d2h30m` の組合せ
- 参加は `🎉 Enter` ボタン押下、再押下で取消(トグル)
- 必要ロール (`required_role`) 指定可
- 画像: アップロード attachment または URL のいずれか
- 終了時にチャンネル(giveaway 投稿先)に当選者をメンションして発表
- **ダッシュボードからも作成可** — フォーム → REST API で投稿 → 永続 view が button を処理 → 期限到来で背景タスクが自動終了

---

## 8. Invite Tracker 仕様

- 起動時に全 invite の uses をキャッシュ
- 新規 join → 全 invite 再 fetch → `uses` が +1 した code を特定
- 特定不能ケース(vanity URL, 発見タブ, screening 経由)は「invite source unknown」
- bot に **Manage Server** 権限が必須
- `/invite create` は admin または `INVITE_CREATOR_ROLE_IDS` 所持者のみ実行可
- audit log: 招待発行は moderator-only に「🆕 Invite `code` created by @user for #ch」

---

## 9. Auction 仕様

- 状態遷移: `pending → live → ending → ended`
  - `live`: 入札受付中
  - `ending`: 最後の `anti_snipe_threshold` 秒間。この間の bid は `anti_snipe_seconds` だけ終了時刻を延長(スナイプ対策)
  - `ended`: 落札確定
- 落札時に自動で「deal channel」(prive な落札者+運営チャンネル)を作成
  - 配置先: `AUCTION_TICKET_CATEGORY_ID`(未設定なら `TICKET_CATEGORY_ID`)
- 永続化: `data/auctions.json` (bot 再起動後も persistent view で button が機能)
- 決済機能なし — host が手動で請求対応
- 実行可能ロール: admin または `AUCTION_MANAGER_ROLE_IDS`
- 実行可能 ch: `ALLOW_CH_AUCTION` / `ALLOW_CAT_AUCTION` で制限可

---

## 10. 週次ダイジェスト (digest)

- `tasks.loop(time=08:00 JST)` で毎日チェック → **月曜のみ** embed を投稿
- 送信先: `DIGEST_CHANNEL_ID`(未設定なら `MODERATOR_CHANNEL_ID`)
- 集計対象: **過去 7 日**
  - 🎫 新規チケット数 / 🌐 翻訳回数 / 👥 新規参加者 / 📦 送料見積数 / 🤖 意図検出数
  - 🌍 上位質問国(`shipping_quote.country` の上位 5)
  - 🚪 上位招待コード(`member_join.invite_code` の上位 5)
- 入力源: `services/digest_store.py` が管理する `data/digest_events.json`(31日ローリング)
- 各 cog が 1 行 `digest_store.append(...)` するだけで自動的に集計される
- 重複投稿防止: `data/digest_last_run.json` に当日 date 記録
- 手動: `/digest now`(preview)/ `/digest post`(即時投稿)

---

## 11. 設定スナップショット / バックアップ (backup)

- 毎日 03:00 JST に全ギルド分のスナップショットを取得
- 投稿先: `BACKUP_CHANNEL_ID`(未設定なら `MODERATOR_CHANNEL_ID`)
- 内容(`backup_YYYY-MM-DD_<guild_id>.json` ファイル添付):
  - roles (id/name/color/hoist/mentionable/permissions/position)
  - categories (id/name/position + 権限オーバーライド)
  - channels (id/name/type/parent_id/topic/nsfw/slowmode + 権限オーバーライド)
  - `.env` の中身(`TOKEN` / `API_KEY` / `SECRET` / `PASSWORD` 系は `<redacted>`)
- `scripts/clone_server.py` の **逆方向**(あちらは「適用」、こちらは「保存」)
- 手動: `/backup now`

---

## 12. Health チェック (health)

- `/health`(admin 限定、ephemeral)
- 表示項目:
  - Process: uptime, gateway latency, guild 数, 読込済み cog 数
  - API budget: Anthropic 月予算消費率, DeepL 月文字数
  - Last 24h: digest_store から過去 24 時間のイベント種別ごとの集計
  - Cogs loaded: 全 cog 名

---

## 13. 管理ダッシュボード (FastAPI)

`dashboard/` 配下、Discord OAuth でログインして自分が admin のサーバーの BOT を Web 上で全設定可能。

### 13.1 画面構成

| URL | 内容 |
|---|---|
| `/login` | Discord OAuth ボタン |
| `/oauth/callback` | Discord OAuth コールバック処理 |
| `/dashboard` | admin として参加しているギルド一覧 |
| `/guild/<id>/setup` | 全機能の設定(チャンネル/ロール/メッセージ編集) |
| `/guild/<id>/status` | BOT 稼働状況、live log、▶起動/↻再起動/■停止 |
| `/guild/<id>/giveaway` | 抽選作成フォーム + 進行中/過去一覧 |
| `/guild/<id>/onboarding` | Discord ネイティブ onboarding 編集(ルール ch・設問最大 5 問×各 8 選択肢・各選択肢に role/channel 紐付け) |
| `/healthz` | Railway 用ヘルスチェック |

### 13.2 アーキテクチャ

```
dashboard/
├── app.py              FastAPI 全ルート + 起動時 autostart
├── auth.py             Discord OAuth (identify + guilds, admin判定)
├── discord_api.py      bot トークンで guild/channels/roles/onboarding 操作
├── config_store.py     deployments/<id>/.env CRUD + URL→ID 抽出
├── bot_manager.py      subprocess.Popen 経由で bot を start/stop/restart, log tail
├── giveaway_helpers.py giveaway 作成・終了時の embed/component 構築
├── static/style.css    Discord 風ダーク UI
└── templates/
    ├── base.html
    ├── login.html
    ├── dashboard.html
    ├── setup.html
    ├── status.html
    ├── giveaway.html
    └── onboarding.html
```

### 13.3 必要環境変数(ダッシュボード本体)

| key | 用途 |
|---|---|
| `DISCORD_CLIENT_ID` | ダッシュボード用 Discord App の Client ID |
| `DISCORD_CLIENT_SECRET` | 同 Secret |
| `DASHBOARD_BASE_URL` | 公開 URL(例: `https://your-app.up.railway.app`) |
| `DASHBOARD_SECRET` | セッション cookie 署名鍵(32文字以上) |
| `DEPLOYMENTS_ROOT` | デプロイメントファイル保存先(Railway: `/data/deployments`) |
| `DASHBOARD_AUTOSTART` | `=1` で起動時に全 guild の bot を自動起動 |
| `DASHBOARD_INSECURE_COOKIE` | `=1` で開発時 HTTP cookie を許可 |

### 13.4 デプロイ(Railway)

詳細: `DASHBOARD_DEPLOY.md`

1. https://discord.com/developers/applications で「ダッシュボード用」アプリ作成
2. Railway に GitHub 連携 → このリポを deploy
3. Variables に上記環境変数を設定
4. Settings → Volumes で `/data` をマウント
5. Generate Domain → Discord App の Redirects に `<URL>/oauth/callback` を追加
6. ブラウザでダッシュボード URL を開いてログイン

---

## 14. .env 設定リファレンス (test deployment)

### Discord 基本
| key | 用途 |
|---|---|
| `DISCORD_TOKEN_SUPPORT` | bot トークン |
| `GUILD_ID` | 対象サーバーID(slash command sync 高速化) |
| `ADMIN_USER_ID` | (任意)個人通知 |
| `MODERATOR_CHANNEL_ID` | システム通知の既定送信先 |

### A. Translation
`TRANSLATE_PROVIDER`(deepl/claude), `DEEPL_API_KEY`, `TRANSLATE_CHANNEL_IDS`, `TICKET_CATEGORY_IDS`, `TRANSLATE_MIN_CONFIDENCE`, `TRANSLATE_DEFAULT_MODE`

### B. Suggester
`COMMUNITY_CHANNEL_ID`, `SUGGEST_MIN_CONFIDENCE`, `PRODUCT_INQUIRY_CHANNEL_ID`, `SHIPPING_GUIDE_CHANNEL_ID`, `PRODUCT_ADVICE_TEXT(_EN)`, `SHIPPING_ADVICE_TEXT(_EN)`

### C. Shipping
`SHIPPING_SHEET_ID`, `SHIPPING_SHEET_NAME`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `PACKAGING_WEIGHT_G`, `MAX_BOX_TOTAL_KG`, `MAX_SHEET_WEIGHT_KG`

### D. Ticket
`TICKET_CATEGORY_ID`, `TICKET_STAFF_ROLE_IDS`, `CARD_GAME_CHANNEL_ID`, `FORCE_UI_LANG_TICKET`

### E. Welcome
`WELCOME_CHANNEL_ID`, `WELCOME_AUTOROLE_IDS`, `WELCOME_RULES_CHANNEL_ID`, `WELCOME_INTRO_CHANNEL_ID`, `WELCOME_BANNER_URL`, `WELCOME_THUMBNAIL_URL`, `WELCOME_TITLE`, `WELCOME_COLOR`, `WELCOME_DESCRIPTION`

### F. Anthropic
`ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `MONTHLY_BUDGET_USD`

### G. Invite Tracker
`INVITE_LOG_CHANNEL_ID`, `INVITE_CREATOR_ROLE_IDS`, `GIVEAWAY_MANAGER_ROLE_IDS`

### H. Digest / Backup
`DIGEST_CHANNEL_ID`, `BACKUP_CHANNEL_ID`

### I. Auction
`AUCTION_MANAGER_ROLE_IDS`, `AUCTION_TICKET_CATEGORY_ID`

### Channel Guard
`ALLOW_CH_SHIPPING`, `ALLOW_CAT_SHIPPING`, `ALLOW_CH_GIVEAWAY`, `ALLOW_CH_INVITE`, `ALLOW_CH_AUCTION`, `ALLOW_CAT_AUCTION`

---

## 15. ディレクトリ構成

```
support_bot/
├── main.py                       bot entry
├── cogs/                          機能別 cog (translator, suggester, shipping, ticket, welcome,
│                                  giveaway, invite_tracker, auction, digest, backup, health, help)
├── services/                      共通 (i18n, channel_guard, sheets_client, claude_client,
│                                  deepl_client, country_search, storage, digest_store)
├── i18n/                          en.json / ja.json (ja.ticket は en.ticket ミラー)
├── dashboard/                     FastAPI 管理ダッシュボード
├── deployments/
│   ├── template/                  雛形 (.env.example, credentials/.gitkeep)
│   └── test/                      稼働中 (.env, credentials/service_account.json, data/*.json)
├── scripts/clone_server.py        サーバー構造を他サーバーへ複製
├── run.sh                         `./run.sh deployments/test`
├── Procfile                       web (dashboard) + worker (bot) 両方
├── railway.json                   Railway 設定
├── requirements.txt
├── DASHBOARD_DEPLOY.md            ダッシュボードのデプロイ手順
└── SPECIFICATION.md               本文書
```

各 deployment は完全に独立(別 bot トークン、別 API キー、別 state、別ログ)。

---

## 16. 永続データ

`deployments/<name>/data/` 配下:

| ファイル | 内容 |
|---|---|
| `ticket_counter.json` | チケット連番(`{"next": N}`) |
| `ticket_owners.json` | `{user_id: channel_id}` (1ユーザー1チケット重複防止) |
| `giveaways.json` | 進行中・終了済の giveaway 全件 |
| `translate_channels.json` | `/translate on` で動的に有効化された ch |
| `usage.json` | Anthropic API 使用量 |
| `deepl_usage.json` | DeepL API 使用量 |
| `digest_events.json` | digest/health 用イベントログ(31日ローリング) |
| `digest_last_run.json` | digest の同日重複投稿防止 |
| `backup_last_run.json` | backup の同日重複防止 |
| `auctions.json` | 進行中/終了済の auction 全件 |

---

## 17. 運用フロー

### bot のみ運用(従来通り)
- env 変更は `deployments/<name>/.env` を直接編集
- 反映: `pkill -f "main.py --env-dir deployments/<name>" && ./run.sh deployments/<name>`

### ダッシュボード経由
- ブラウザでダッシュボードを開く → guild 選択 → 設定変更 → 「💾 保存して BOT を再起動」
- ダッシュボードが subprocess を kill → 再起動

### ログ
- `deployments/<name>/support_bot.log` に全件
- ダッシュボードからは `/guild/<id>/status` でリアルタイム表示(10秒ごと自動更新)

### Discord intent
`default` + `message_content` + `members`(プライベイレッジド)。`invites` は default に含まれる

---

## 18. メモ (今回の開発で確定したこと)

- 既存ticket(#0001〜#0005)の welcome は 5/12 時点の旧文言で固定(retroactive 編集不可)
- `MODERATOR_CHANNEL_ID = 1499307489831682058`
- `ALLOW_CH_INVITE = 1499307490175746088`(専用 invite-ops チャンネル)
- bot ロールに **Manage Server** 権限が付与済(invite 一覧取得に必要)
- onboarding を使うには Discord 側で「コミュニティ機能」有効化が前提
- Repo: `https://github.com/tokutake-netizen/support-bot` (main 直 push 運用)
