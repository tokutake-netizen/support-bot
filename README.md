# Discord Support Bot

A multi-feature Discord bot for community support: bilingual auto-translation, intent-based advice, shipping calculator with Google Sheets, ticket system, welcome and giveaway tools — designed to run **multiple isolated instances**, one per Discord server.

> Powered by [discord.py](https://discordpy.readthedocs.io/), [DeepL API](https://www.deepl.com/pro-api), [Anthropic Claude API](https://www.anthropic.com/), and Google Sheets.

---

## ✨ Features

| | Feature | Slash command | Notes |
|---|---|---|---|
| A | **Auto translate** (EN ⇄ JA) | `/translate on/off/mode/status/text` | DeepL or Claude. Bot reply prefix: 🇯🇵 / 🇺🇸 |
| B | **Intent advice** | `/suggest on/off/status` | Detects product/shipping inquiries, posts a redirect note |
| C | **Shipping calculator** | `/shipping`, `/shippingadmin reload` | Cart UI: product × qty × destination → carrier + price from Google Sheet |
| D | **Tickets** (Mee6-style) | `/ticket panel/close` | Click button → private channel under a category, only opener + staff role can see |
| E | **Welcome** | `/welcome test/status/setbanner` | Auto-greets new members, optional auto-role, banner image |
| F | **Giveaway** | `/giveaway create/end/reroll/list` | Time-based raffle, click 🎉 to enter, auto-pick winners, prize image |
| H | **Help** | `/help [category]` | In-Discord command reference |

📖 **Full reference**: see [`COMMANDS.md`](COMMANDS.md) for every command, options, and examples.

---

## 🚀 Quick start (single server)

```bash
git clone https://github.com/<you>/support-bot.git
cd support-bot
pip install -r requirements.txt

# Copy template and fill in your IDs/keys
cp -r deployments/template deployments/myserver
cp deployments/myserver/.env.example deployments/myserver/.env
# edit deployments/myserver/.env

# Optional: place Google service account JSON at:
# deployments/myserver/credentials/service_account.json

python main.py --env-dir deployments/myserver
```

---

## 🌐 Multi-server deployment

Run a **separate process per Discord server**, each with its own:
- Discord bot token
- DeepL / Anthropic / Google Sheets keys
- Channel IDs, role IDs, message templates
- Local state (giveaways, tickets, usage tracking)

```
support_bot/
├── main.py                    ← shared code
├── cogs/, services/, i18n/    ← shared code
├── data/products.json         ← shared product master
└── deployments/
    ├── template/              ← template (committed, no secrets)
    ├── server-tokyo/          ← per-server config (gitignored)
    │   ├── .env
    │   ├── credentials/
    │   └── data/
    ├── server-osaka/
    └── ...
```

Run all instances:

```bash
python main.py --env-dir deployments/server-tokyo &
python main.py --env-dir deployments/server-osaka &
```

For production, use systemd / Docker Compose / PM2 / Railway-per-service to keep them alive. See [SETUP.md](SETUP.md).

---

## ⚙ Required external setup

### 1. Discord bot
- https://discord.com/developers/applications → **New Application**
- **Bot** tab → Reset Token, enable **MESSAGE CONTENT** + **SERVER MEMBERS** intents
- **OAuth2 → URL Generator** → scopes `bot`, `applications.commands`, permissions: View Channels, Send Messages, Send in Threads, Embed Links, Read History, Add Reactions, Use Slash Commands, Manage Channels (for tickets)

### 2. DeepL API
- https://www.deepl.com/pro-api → sign up for **DeepL API Free** (1M chars/month)
- Copy the key (ends with `:fx`) → `DEEPL_API_KEY`

### 3. Anthropic Claude (optional, for intent classification)
- https://console.anthropic.com/ → create API key → `ANTHROPIC_API_KEY`

### 4. Google Sheets API (for shipping calculator)
- https://console.cloud.google.com/ → enable Sheets API, create service account, download JSON key
- Share the spreadsheet with the service account email as **Viewer**

### 5. Discord IDs (developer mode → right-click → Copy ID)
- `GUILD_ID`, `MODERATOR_CHANNEL_ID`, `COMMUNITY_CHANNEL_ID`, etc.

See [`deployments/template/.env.example`](deployments/template/.env.example) for the full list.

---

## 🗂 Project layout

```
.
├── main.py                # entrypoint
├── cogs/
│   ├── translator.py      # A. translation
│   ├── suggester.py       # B. intent advice
│   ├── shipping.py        # C. shipping calculator
│   ├── ticket.py          # D. tickets
│   ├── welcome.py         # E. welcome
│   └── giveaway.py        # F. giveaway
├── services/
│   ├── claude_client.py
│   ├── deepl_client.py
│   ├── sheets_client.py
│   ├── country_search.py  # multi-stage country fuzzy search
│   ├── i18n.py
│   └── storage.py
├── i18n/
│   ├── ja.json, en.json   # UI strings
│   └── countries.json     # 80+ countries with aliases
├── data/
│   └── products.json      # default product master (CASE/BOX/PSA/Single)
├── deployments/           # per-deployment config (gitignored except template)
└── scripts/               # helper scripts (find roles/categories, post panel)
```

---

## 🔒 Security

- **Never commit `.env` files** or `credentials/*.json`. `.gitignore` enforces this.
- Each deployment has its own bot token, isolating breach blast radius.
- Translations only run in channels explicitly enabled (env or `/translate on`).
- `/translate`, `/suggest`, `/ticket`, `/welcome`, `/giveaway`, `/shippingadmin` require **Administrator** permission.
- `/shipping` is open to all members.

---

## 📚 Documentation

| Doc | Purpose |
|-----|---------|
| [README.md](README.md) | This file. Project overview & quick start |
| [COMMANDS.md](COMMANDS.md) | Complete slash-command reference |
| [SETUP.md](SETUP.md) | Multi-server deployment guide (Docker, systemd, etc.) |
| [`deployments/template/`](deployments/template/) | Template to copy for new servers |

---

## 🪪 License

[MIT](LICENSE) © 2026 yamazakiayaka

---

# 日本語

複数機能のDiscordサポートBOT。**サーバーごとに完全に独立したプロセス** で動かせる構成。

## 機能
- A. 英⇄日 自動翻訳 (DeepL or Claude)
- B. 意図判定アドバイス (商品問合せ・配送相談を自動誘導)
- C. 送料計算 (Google Sheets連携・カート式UI・国検索)
- D. チケット (Mee6風、カテゴリ配下に個別チャンネル作成)
- E. Welcome (新規メンバーへの自動歓迎)
- F. Giveaway (時間指定抽選・参加ボタン)

## 起動

```bash
pip install -r requirements.txt
cp -r deployments/template deployments/myserver
# deployments/myserver/.env を編集
python main.py --env-dir deployments/myserver
```

## 複数サーバー運用

`deployments/<サーバー名>/` を増やして、それぞれ別プロセスで起動するだけ。
詳細は [SETUP.md](SETUP.md) を参照。
