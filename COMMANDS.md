# Commands Guide / コマンド一覧

全 11 グループ・35 コマンド超(2026-05-17 時点)。

`A 翻訳` / `B 意図判定` / `C 送料計算` / `D チケット` / `E Welcome` /
`F Giveaway` / `G オークション` / `H 招待トラッキング` /
`I 週次ダイジェスト` / `J 設定バックアップ` / `K ヘルスチェック`

---

## A. 翻訳 `/translate`

英⇄日 双方向自動翻訳。DeepL Free or Anthropic Claude を切替使用。

| コマンド | 説明 | 権限 |
|---------|------|------|
| `/translate on` | このチャンネルで自動翻訳を有効化 | Admin |
| `/translate off` | このチャンネルの自動翻訳を停止 | Admin |
| `/translate mode <direction>` | 翻訳方向を変更<br>`both` (双方向) / `en2ja` (英→日のみ) / `ja2en` (日→英のみ) | Admin |
| `/translate status` | 有効チャンネル一覧、現在のプロバイダ、月使用量を表示 | Admin |
| `/translate text <text> [direction]` | 任意の文字列を即時翻訳して ephemeral 返信<br>`direction`: `auto` (既定) / `en2ja` / `ja2en` | Admin |

**自動翻訳の仕組み**
- `.env` の `TRANSLATE_CHANNEL_IDS` に登録 or `/translate on` 実行で有効化
- 英文 → 🇯🇵 日本語訳をreply / 日本文 → 🇺🇸 英訳をreply
- URL／絵文字のみ／2文字未満／bot自身のreply はスキップ
- `langdetect` の信頼度が `TRANSLATE_MIN_CONFIDENCE` (既定0.85) 未満は無視

**例**
```
/translate on
/translate text text:Hello, how are you?
/translate mode direction:en2ja
```

---

## B. 意図判定 `/suggest`

`COMMUNITY_CHANNEL_ID` の発言を Claude で分類し、商品問合せ／配送相談を検知して誘導文をreply。

| コマンド | 説明 | 権限 |
|---------|------|------|
| `/suggest on` | 意図判定を有効化 | Admin |
| `/suggest off` | 意図判定を停止 | Admin |
| `/suggest status` | 現在の有効状態を確認 | Admin |

**自動判定の挙動**
- ラベル: `product_inquiry` / `shipping_inquiry` / `general_chat` / `other`
- 信頼度 ≥ `SUGGEST_MIN_CONFIDENCE` (既定0.70) のみ反応
- 商品問合せ → `PRODUCT_INQUIRY_CHANNEL_ID` 誘導
- 配送相談 → `/shipping` ＋ `SHIPPING_GUIDE_CHANNEL_ID` 誘導
- 同一メッセージへの reply は1回限り
- 投稿者の言語に応じて日本語／英語を出し分け

---

## C. 送料計算 `/shipping` / `/shippingadmin`

商品×数量×宛先国 をドロップダウン選択して Google Sheets から料金を取得。

| コマンド | 説明 | 権限 |
|---------|------|------|
| `/shipping` | 送料計算パネルを起動 (ephemeral) | 全員 |
| `/shippingadmin reload` | 商品マスタとスプシキャッシュを再読込 | Admin |

**計算パネル操作 (`/shipping`)**

| コンポーネント | 役割 |
|---------------|------|
| 🎁 商品 SelectMenu | 6商品から選択（CASE/BOX/PSA/Single） |
| 🔢 数量 SelectMenu | 1, 2, 3, 5, 10, 20, 50, 100 / その他 (Modal入力) |
| 🏳️ 宛先 SelectMenu | リージョン → 国 の二段選択 / 🔍 直接検索 |
| 📥 送信ボタン | 商品＋数量＋宛先 全部入力後にカートへ追加 |
| 🗑 最後を削除 | カート末尾を削除 |
| 🔄 全クリア | カート全削除 |
| 🧮 送料を計算 | 結果カード表示 |
| 📤 公開する | チャンネルに通常メッセージで再投稿 |
| 🔁 やり直す | カートとフォームを初期化 |
| 🌐 JP/EN | UI言語切替 |

**計算ロジック**
- 合計商品重量 + 梱包材1kg = 総重量
- 0.5kg刻みで切り上げ
- 20kg超は分割発送（自動で複数箱に分割し料金合算）
- スプシ「比較表US基準」の `安い方` 列で DHL/FedEx を自動選択

**例**
```
/shipping             ← パネル起動
/shippingadmin reload ← スプシ更新後に再読込
```

---

## D. チケット `/ticket`

Mee6風のサポートチケット。ボタンクリックで個別チャンネル作成。

| コマンド | 説明 | 権限 |
|---------|------|------|
| `/ticket panel` | このチャンネルにチケット起票パネルを設置 | Admin |
| `/ticket close` | このチケットチャンネルを閉じる（5秒後削除） | Admin |

**動作仕様**
- パネル「🎫 チケットを開く」押下 → `Created Tickets` カテゴリ配下に `0001-username` 形式で個別チャンネル作成
- 開設者＋運営ロール（`TICKET_STAFF_ROLE_IDS`）のみ閲覧可、他メンバーには非公開
- 同一ユーザーは **1チケットのみ** 開設可。重複時は既存チケットを案内
- スレッド内の「🔒 クローズ」ボタンで5秒カウント後に削除

**チケット内のボタン**
| ボタン | 役割 |
|--------|------|
| 🔒 クローズ | 5秒後にチャンネル削除＋owner解除 |

---

## E. Welcome `/welcome`

新規メンバーへの自動ウェルカム投稿。

| コマンド | 説明 | 権限 |
|---------|------|------|
| `/welcome test` | 自分にプレビュー表示（ephemeral） | Admin |
| `/welcome status` | 現在の設定を確認 | Admin |
| `/welcome setbanner [image] [url]` | バナー画像を設定。引数なし＝クリア | Admin |

**自動動作**
- 新規メンバー入室時に `WELCOME_CHANNEL_ID` へEmbedを投稿
- ユーザーアイコンをサムネイル表示
- `WELCOME_AUTOROLE_IDS` 設定でロール自動付与
- バナー画像、タイトル、色、本文テキスト全て `.env` でカスタマイズ可能

**カスタマイズ用 `.env` 変数**
```
WELCOME_BANNER_URL=         # 大きな画像
WELCOME_THUMBNAIL_URL=      # サムネ（既定: ユーザーアイコン）
WELCOME_TITLE=              # Embed タイトル
WELCOME_COLOR=0x57F287      # 色 (16進)
WELCOME_DESCRIPTION=        # 本文テンプレ
```

**プレースホルダ** (本文に使用可):
`{user_mention}` `{user_name}` `{guild_name}` `{member_count}` `{rules}` `{intro}`

---

## F. Giveaway `/giveaway`

時間指定の抽選ツール。参加ボタン式。

| コマンド | 説明 | 権限 |
|---------|------|------|
| `/giveaway create prize duration [winners] [required_role] [image] [image_url]` | 新規Giveaway開始 | Admin |
| `/giveaway end message_id` | 早期終了して即座に当選者発表 | Admin |
| `/giveaway reroll message_id [winners]` | 終了済み抽選の再抽選 | Admin |
| `/giveaway list` | 進行中のGiveaway一覧 | Admin |

**期間フォーマット** (duration)
| 入力 | 意味 |
|------|------|
| `30s` | 30秒 |
| `5m` | 5分 |
| `1h` | 1時間 |
| `1d` | 1日 |
| `1d2h30m` | 組み合わせ可 |

**例**
```
/giveaway create prize:Amazonギフト1000円 duration:1h winners:3
/giveaway create prize:限定カード duration:1d winners:1 image:[画像添付] required_role:@VIP
/giveaway end message_id:1502...
/giveaway reroll message_id:1502... winners:2
```

**参加者向け**
- Embed下の「🎉 参加 / Enter」ボタンを押すだけ
- 同じボタンを再度押すと参加取消（トグル）
- 残量・残り時間が逐次更新
- 期限切れで自動的に当選者抽選＆発表（チャンネルにメンション）

---

## G. オークション `/auction`

ボタン入札式の非同期オークション。Forum Channel または Text Channel に対応、アンチスナイプ自動延長付き。

| コマンド | 説明 | 権限 |
|---------|------|------|
| `/auction create title duration starting_bid [...]` | 新規オークション開始 | Admin / Manager |
| `/auction end <message_id>` | 早期終了して落札者確定 | Host / Admin |
| `/auction cancel <message_id>` | キャンセル（落札者なし） | Host / Admin |
| `/auction list` | 進行中オークション一覧 | 全員 |
| `/auction history <message_id>` | 入札履歴の表示（ephemeral） | 全員 |

**`/auction create` パラメータ**

| 引数 | 既定値 | 説明 |
|------|------|------|
| `title` | — | 商品名（例: `PSA 10 Charizard 1st Ed.`） |
| `starting_bid` | — | 開始価格（JPY、整数） |
| `duration` | — | `12h` / `1d` / `6h30m` 等 |
| `description` | 空 | 商品説明 |
| `image` | — | 画像添付 |
| `image_url` | — | URL指定（添付の代替） |
| `min_increment` | `100` | 絶対最小増分（円）。5%ルールと両立 |
| `anti_snipe_window` | `60` | 終了X秒前なら延長発動 |
| `anti_snipe_extend` | `60` | スナイプ時に追加する秒数 |

**通貨**：JPY 固定。

**動作**
- Forum Channel で実行 → スレッド自動作成＆オークション Embed 投稿
- Text Channel で実行 → そのチャンネルに Embed 投稿
- ボタン `💰 Place Bid` → モーダルで金額入力。Host は自分のオークションに入札不可
- **入札条件（ちょいノリ防止）**：次の入札は **現最高値の +5% 以上** かつ **`min_increment` 以上の上乗せ** を両方満たす必要がある
  - 例: 現最高値 ¥10,000 / `min_increment=100` → 次の必要額は `max(10100, ceil(10000×1.05)) = 10,500`
  - 例: 現最高値 ¥500 / `min_increment=100` → `max(600, 525) = 600`（小額時は min_increment が支配的）
  - 初回入札のみ `starting_bid` 以上であれば OK
- アンチスナイプ：終了 `anti_snipe_window` 秒以内に入札があったら ends_at に `anti_snipe_extend` 秒追加
- ボタン `📜 Bid history` で履歴 ephemeral 表示
- 期限切れで自動終了 → Embed をグレーアウト＋勝者発表
- 落札時、**`TICKET_CATEGORY_ID`（または `AUCTION_TICKET_CATEGORY_ID`）配下に落札者×Host×Staff のみが見える private channel を自動作成**（minimal-intervention 原則の明示的な例外。決済・発送をここで個別調整）
- 落札者にDM通知（best-effort、DM閉じている場合はスキップ）

**重要な注意**
- 決済処理は bot 側で行わない。落札後のやり取りは deal channel で人間が手動調整
- 入札データは `deployments/<server>/data/auctions.json` に永続化（bot 再起動でも継続）
- 古物営業法・特定商取引法の遵守は運営側責任。中古再販ならライセンス確認

**例**
```
/auction create title:PSA 10 リザードン 1st ED. starting_bid:50000 duration:1d
/auction create title:Pokemon Box Sealed starting_bid:8000 duration:6h min_increment:500 image:[添付]
/auction list
/auction end message_id:1502...
```

---

## H. 招待トラッキング `/invite`

入室時に「どの招待コードが +1 されたか」を差分判定して、Welcome embed と運営ログに自動記録。複数 Instagram アカウントの流入元判別に。

| コマンド | 説明 | 権限 |
|---------|------|------|
| `/invite list` | 現在の招待コード+使用回数+招待主+チャンネル一覧 | Admin |
| `/invite create [channel] [max_uses] [max_age_hours]` | 招待リンク発行 | Admin or `INVITE_CREATOR_ROLE_IDS` |

**自動動作**
- 全 invite の使用回数を起動時にキャッシュ
- `on_member_join` で再 fetch → +1 した code を特定
- Welcome embed に `✨ Invited via <code> by @inviter` フィールドを差し込み
- `INVITE_LOG_CHANNEL_ID`(または `MODERATOR_CHANNEL_ID`)に監査ログ

**bot に必要な権限**: `Manage Server`(`guild.invites()`)

---

## I. 週次ダイジェスト `/digest`

毎週月曜 08:00 JST に過去 7 日のサマリ embed を投稿。

| コマンド | 説明 | 権限 |
|---------|------|------|
| `/digest now` | 過去 7 日分の preview を ephemeral 表示 | Admin |
| `/digest post` | 即時にチャンネルへ投稿 | Admin |

**集計項目**
- 🎫 新規チケット数 / 🌐 翻訳回数 / 👥 新規参加者 / 📦 送料見積数 / 🤖 意図検出数
- 🌍 上位質問国(`/shipping` で選ばれた国の上位 5)
- 🚪 上位招待コード(`member_join` で +1 した上位 5)

**送信先**: `DIGEST_CHANNEL_ID`(未設定なら `MODERATOR_CHANNEL_ID`)

**仕組み**
- `services/digest_store.py` が `data/digest_events.json` (31日ローリング)に各 cog のイベントを append-only で記録
- 月曜の自動ポストと `/health` の "Last 24h" はこのストアを共有

---

## J. 設定バックアップ `/backup`

毎日 03:00 JST に全 guild のチャンネル/ロール/.env をスナップショットして JSON 添付で投稿。

| コマンド | 説明 | 権限 |
|---------|------|------|
| `/backup now` | 今この瞬間の状態を即時スナップショット | Admin |

**スナップショット内容**
- roles(id/name/color/hoist/mentionable/permissions/position/managed)
- categories(id/name/position + 権限オーバーライド)
- channels(id/name/type/topic/nsfw/slowmode + 権限オーバーライド)
- `.env` の中身(`TOKEN` / `API_KEY` / `SECRET` / `PASSWORD` 系は `<redacted>` でマスク)

**送信先**: `BACKUP_CHANNEL_ID`(未設定なら `MODERATOR_CHANNEL_ID`)

**用途**
- 「気付いたら誰かが ch 削除/設定変更してしまった」時の復元用
- `scripts/clone_server.py` (適用方向)の **保存方向**

---

## K. ヘルスチェック `/health`

| コマンド | 説明 | 権限 |
|---------|------|------|
| `/health` | BOT 状態スナップショットを ephemeral 表示 | Admin |

**表示項目**
- Process: uptime / gateway latency / guild 数 / 読込済み cog 数
- API budget: Anthropic 月予算消費率, DeepL 月文字数
- Last 24h: digest_store から過去 24 時間のイベント種別ごとの集計
- Cogs loaded: 全 cog 名

---

## 共通事項

### 権限
- **Admin** = `Administrator` 権限を持つメンバーのみ実行可
- 非Adminが打つと Discord側で「You do not have permission」と弾かれbotには届かない
- `/shipping` のみ全員実行可
- `/giveaway` `/invite create` `/auction` は専用ロール(`*_MANAGER_ROLE_IDS` / `INVITE_CREATOR_ROLE_IDS`)でも実行可

### Ephemeral応答
多くの管理コマンドは「あなたにのみ表示」のephemeral応答。チャンネルを汚さない。
公開したい結果は `📤 公開する` ボタンや `/giveaway create` 等の意図的な公開コマンド経由で投稿。

### エラーマーク
- ⚠️ メッセージへのリアクション = 翻訳API失敗（`support_bot.log` に詳細）
- ❌ メッセージ = ユーザー入力エラー（重量超過・該当データなしなど）

### ログ
すべての操作・エラーは `deployments/<server>/support_bot.log` に記録。
