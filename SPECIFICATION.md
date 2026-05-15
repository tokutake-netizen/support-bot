# Support Bot — 仕様書

最終更新: 2026-05-16
対象: `Desktop/ClaudeCode/Discordbot/support_bot/` (deployment: `deployments/test/`)

---

## 1. 概要

英語圏(英語ファースト)のオンライン トレカ販売 Discord サーバー向けサポート BOT。
1 プロセスで 7 機能を提供する。

| 機能 | 役割 |
|---|---|
| translator | DeepL / Claude 自動翻訳 |
| suggester | Claude による意図判定→誘導 |
| shipping | Google Sheets ベースの送料計算 |
| ticket | Mee6 風のサポートチケット起票 |
| welcome | 新規入室時のウェルカム embed |
| giveaway | ボタン式抽選ツール |
| invite_tracker | 招待元の自動判定＋招待リンク発行 |

---

## 2. 言語ポリシー

- BOT 出力は全機能 **英語固定**(`ja.json` の `ticket` セクションは `en.json` をミラー)
- 翻訳機能だけは「英文 → 🇯🇵日本語訳」「日本文 → 🇺🇸英訳」を返す目的なので双方向出力
- per-feature override 用 env var が用意されている (`FORCE_UI_LANG_TICKET`, `FORCE_UI_LANG_SHIPPING`)

---

## 3. コマンド一覧

`/help` で表示される 9 グループ + 1 ヘルパ。**Admin** は Discord の Administrator 権限保有者を指す。

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
| `/giveaway create/end/reroll/list` | Admin | **#raffle のみ** | 抽選の作成・運用 |
| `/invite list` | Admin | **invite ch のみ** (`1499307490175746088`) | 招待コードと使用回数の一覧 |
| `/invite create` | Admin or 指定ロール | 同上 | 招待リンク発行 |

「使用可能 ch」は `services/channel_guard.py` が `.env` の `ALLOW_CH_<KEY>` / `ALLOW_CAT_<KEY>` を参照して enforce する。未設定キーは制限なし。

---

## 4. 自動動作 (slash command 非依存)

- **on_member_join**: `invite_tracker` が一元処理
  - 招待差分検出 → どの code が +1 したか確定
  - Welcome embed に「✨ Invited via `code` by @inviter」フィールドを差し込んで投稿
  - `MODERATOR_CHANNEL_ID`(または `INVITE_LOG_CHANNEL_ID`)へ監査ログ
  - `WELCOME_AUTOROLE_IDS` のロールを自動付与
- **on_invite_create / on_invite_delete**: invite キャッシュを差分維持
- **on_message** (translator): `TRANSLATE_CHANNEL_IDS` または `/translate on` 済 ch、および `TICKET_CATEGORY_IDS` 配下を対象に自動翻訳 reply
- **on_message** (suggester): `COMMUNITY_CHANNEL_ID` を監視、Claude で `product_inquiry` / `shipping_inquiry` を検知すると誘導文 reply
- **背景タスク** (giveaway): 30 秒毎に期限切れ giveaway を検出 → 自動で当選者抽選 + 発表

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

---

## 7. Giveaway 仕様

- duration 書式: `30s`, `5m`, `1h`, `1d2h30m` の組合せ
- 参加は `🎉 Enter` ボタン押下、再押下で取消(トグル)
- 必要ロール (`required_role`) 指定可
- 画像: アップロード attachment または URL のいずれか
- 終了時にチャンネル(giveaway 投稿先)に当選者をメンションして発表

---

## 8. Invite Tracker 仕様

- 起動時に全 invite の uses をキャッシュ
- 新規 join → 全 invite 再 fetch → `uses` が +1 した code を特定
- 特定不能ケース(vanity URL, 発見タブ, screening 経由)は「invite source unknown」
- bot に **Manage Server** 権限が必須(`guild.invites()` で必要)
- `/invite create` は admin または `INVITE_CREATOR_ROLE_IDS` 所持者のみ実行可
- audit log: 招待発行は moderator-only に「🆕 Invite `code` created by @user for #ch」

---

## 9. .env 設定リファレンス (test deployment)

### Discord 基本
| key | 用途 |
|---|---|
| `DISCORD_TOKEN_SUPPORT` | bot トークン |
| `GUILD_ID` | 対象サーバーID(slash command sync 高速化用) |
| `ADMIN_USER_ID` | (任意)個人通知用 |
| `MODERATOR_CHANNEL_ID` | システム通知の既定送信先 |

### A. Translation
| key | 用途 |
|---|---|
| `TRANSLATE_PROVIDER` | `deepl` または `claude` |
| `DEEPL_API_KEY` | DeepL Free / Pro のキー |
| `TRANSLATE_CHANNEL_IDS` | 自動翻訳対象ch(明示) |
| `TICKET_CATEGORY_IDS` | 自動翻訳対象カテゴリ(チケット動的ch全部) |
| `TRANSLATE_MIN_CONFIDENCE` | `langdetect` の最小信頼度 (既定 0.85) |
| `TRANSLATE_DEFAULT_MODE` | `both` / `en2ja` / `ja2en` |

### B. Suggester
| key | 用途 |
|---|---|
| `COMMUNITY_CHANNEL_ID` | 意図判定対象ch |
| `SUGGEST_MIN_CONFIDENCE` | 最小信頼度 (既定 0.70) |
| `PRODUCT_INQUIRY_CHANNEL_ID` | 商品問合せの誘導先(= `#ticket`) |
| `SHIPPING_GUIDE_CHANNEL_ID` | 配送相談の誘導先(= `#shipping-guide`) |
| `PRODUCT_ADVICE_TEXT(_EN)` | 誘導文テンプレ |
| `SHIPPING_ADVICE_TEXT(_EN)` | 誘導文テンプレ |

### C. Shipping
| key | 用途 |
|---|---|
| `SHIPPING_SHEET_ID` | Google Sheet ID |
| `SHIPPING_SHEET_NAME` | シート名(既定: `比較表US基準`) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | サービスアカウントjsonへのパス |
| `PACKAGING_WEIGHT_G` | 梱包材重量g(既定: 1000) |
| `MAX_BOX_TOTAL_KG` | 1箱最大kg(既定: 20) |
| `MAX_SHEET_WEIGHT_KG` | シートに載っている最大重量 |

### D. Ticket
| key | 用途 |
|---|---|
| `TICKET_CATEGORY_ID` | チケット ch を配置するカテゴリ |
| `TICKET_STAFF_ROLE_IDS` | チケット閲覧可ロール |
| `CARD_GAME_CHANNEL_ID` | panel 文言で言及される ch |
| `FORCE_UI_LANG_TICKET` | `en` 固定推奨 |

### E. Welcome
| key | 用途 |
|---|---|
| `WELCOME_CHANNEL_ID` | welcome embed 投稿先 |
| `WELCOME_AUTOROLE_IDS` | 自動付与ロール |
| `WELCOME_RULES_CHANNEL_ID` | 案内文中の rules link |
| `WELCOME_INTRO_CHANNEL_ID` | 案内文中の intro link |
| `WELCOME_BANNER_URL` | バナー画像 URL(`/welcome setbanner` 経由更新) |
| `WELCOME_THUMBNAIL_URL` | サムネ override(既定はユーザーアイコン) |
| `WELCOME_TITLE` / `WELCOME_COLOR` / `WELCOME_DESCRIPTION` | embed 上書き(任意) |

### F. Anthropic
| key | 用途 |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API キー |
| `ANTHROPIC_MODEL` | 既定 `claude-haiku-4-5` |
| `MONTHLY_BUDGET_USD` | 月予算超過で自動停止 |

### G. Invite Tracker
| key | 用途 |
|---|---|
| `INVITE_LOG_CHANNEL_ID` | 招待ログ送信先(未設定なら `MODERATOR_CHANNEL_ID`) |
| `INVITE_CREATOR_ROLE_IDS` | `/invite create` を許可するロール |

### Channel Guard
| key | 用途 |
|---|---|
| `ALLOW_CH_SHIPPING` | `/shipping` の許可ch (例: `#shipping-guide,#ticket,#request`) |
| `ALLOW_CAT_SHIPPING` | `/shipping` の許可カテゴリ (例: `TICKET_CATEGORY_ID`) |
| `ALLOW_CH_GIVEAWAY` | `/giveaway *` の許可ch (例: `#raffle`) |
| `ALLOW_CH_INVITE` | `/invite *` の許可ch (例: dedicated invite-ops ch) |

---

## 10. デプロイメント

```
support_bot/
├── main.py                    # entry
├── cogs/                       # 機能別 (translator, suggester, shipping, ticket, welcome, giveaway, invite_tracker, help)
├── services/                   # 共通 (i18n, channel_guard, sheets_client, claude_client, deepl_client, country_search, storage)
├── i18n/                       # en.json / ja.json (現状 ja.ticket は en.ticket ミラー)
├── deployments/
│   ├── template/               # 雛形 (.env.example, credentials/.gitkeep)
│   └── test/                   # 稼働中(`.env`, `credentials/service_account.json`, `data/*.json`)
├── run.sh                      # `./run.sh deployments/test`
├── requirements.txt
└── SPECIFICATION.md            # 本文書
```

各 deployment は完全に独立(別 bot トークン、別 API キー、別 state、別ログ)。

---

## 11. 運用フロー

- env 変更は `deployments/<name>/.env` を直接編集
- 反映には `pkill -f "main.py --env-dir deployments/<name>" && ./run.sh deployments/<name>` を実行
- ログ: `deployments/<name>/support_bot.log` に全件
- 永続データ: `deployments/<name>/data/*.json`
  - `ticket_counter.json` — チケット連番
  - `ticket_owners.json` — `{user_id: channel_id}` (重複防止)
  - `giveaways.json` — 進行中・終了済の giveaway 全件
  - `translate_channels.json` — `/translate on` で動的に有効化された ch
  - `usage.json` / `deepl_usage.json` — API 使用量
- 起動時の Discord intent: `default` + `message_content` + `members`(プライベイレッジド)

---

## 12. 未配置/未確定の設定

優先度順:

- `ALLOW_CH_GIVEAWAY` の `#raffle` チャンネル ID
- `ALLOW_CH_SHIPPING` への `#request` チャンネル ID 追加
- `INVITE_CREATOR_ROLE_IDS` — `/invite create` を許可するロール(未設定なら admin 限定)

---

## 13. メモ (今回の開発で確定したこと)

- 既存ticket(#0001〜#0005)の welcome は 5/12 時点の旧文言で固定(retroactive 編集不可)
- `MODERATOR_CHANNEL_ID = 1499307489831682058`
- `ALLOW_CH_INVITE = 1499307490175746088`(専用 invite-ops チャンネル)
- bot ロールに **Manage Server** 権限が付与済(invite 一覧取得に必要)
- Repo: `https://github.com/tokutake-netizen/support-bot` (main直 push 運用)
