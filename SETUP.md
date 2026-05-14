# Setup Guide / セットアップ手順

This bot is designed for **multi-server deployment**: each Discord server runs as an independent process with its own bot token and configuration. Configurations live under `deployments/<server-name>/`.

各Discordサーバーごとに **独立したプロセス** として動かす設計。設定は `deployments/<server-name>/` に分離します。

---

## 1. Prerequisites

- Python 3.9+ (or Docker)
- Discord account with permission to add bots to your server
- (Optional) Google Cloud account for shipping calculator
- (Optional) DeepL Free API key for translation
- (Optional) Anthropic API key for intent classification

---

## 2. Per-server external setup

For **each Discord server** you want to run the bot in, repeat:

### 2.1 Create a Discord Bot
1. Open https://discord.com/developers/applications → **New Application**
2. Name it (e.g. `MyServerBot`) → Create
3. Go to **Bot** tab:
   - Click **Reset Token**, copy the token (`MTA...`) — keep it secret
   - **Privileged Gateway Intents** → enable:
     - ☑ MESSAGE CONTENT INTENT
     - ☑ SERVER MEMBERS INTENT
4. **OAuth2 → URL Generator**:
   - SCOPES: `bot`, `applications.commands`
   - BOT PERMISSIONS: View Channels, Send Messages, Send Messages in Threads, Embed Links, Read Message History, Add Reactions, Use Slash Commands, Manage Channels (for tickets)
5. Open the generated URL → invite the bot to your server

### 2.2 Get Discord IDs
Enable **Developer Mode** in Discord (User Settings → Advanced → Developer Mode), then right-click any item → "Copy ID":
- Server (Guild) ID
- Channel IDs (translate target, community, ticket panel, shipping guide, moderator-only, welcome)
- Role IDs (admin, moderator)
- Your user ID (for admin alerts)
- Category IDs (for ticket creation)

### 2.3 (Optional) DeepL API Free
- https://www.deepl.com/pro-api → sign up for **DeepL API Free** plan (1,000,000 chars/month)
- Account → API Keys → copy key (ends with `:fx`)

### 2.4 (Optional) Anthropic Claude
- https://console.anthropic.com/ → create API key
- Top up credits in **Plans & Billing**

### 2.5 (Optional) Google Sheets
For the shipping calculator:
1. https://console.cloud.google.com/ → create project
2. **APIs & Services → Library** → enable **Google Sheets API**
3. **Credentials → Create Credentials → Service Account**:
   - Name it, skip role assignment
   - Click the new account → **Keys → Add Key → JSON** → download
4. Share your shipping rate spreadsheet with the service account email (Viewer role)

---

## 3. Install & configure

```bash
git clone https://github.com/<you>/support-bot.git
cd support-bot
pip install -r requirements.txt

# For each server:
cp -r deployments/template deployments/<server-name>
cp deployments/<server-name>/.env.example deployments/<server-name>/.env
```

Edit `deployments/<server-name>/.env`:
```bash
# Required
DISCORD_TOKEN_SUPPORT=MTA...
GUILD_ID=1499...

# Translation
TRANSLATE_PROVIDER=deepl
DEEPL_API_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx:fx
TRANSLATE_CHANNEL_IDS=1499...,1499...

# Other features (see .env.example for full list)
```

If using shipping calculator, drop the service account JSON at:
```
deployments/<server-name>/credentials/service_account.json
```

---

## 4. Run

### Single instance (foreground)
```bash
./run.sh deployments/<server-name>
# or
python3 main.py --env-dir deployments/<server-name>
```

### Multiple instances in background
```bash
./run.sh deployments/server-tokyo > tokyo.log 2>&1 &
./run.sh deployments/server-osaka > osaka.log 2>&1 &
```

### Docker Compose (recommended for production)
```bash
# Edit docker-compose.yml to define one service per server
docker compose up -d --build
docker compose logs -f bot-tokyo
```

### systemd (Linux server)
Create `/etc/systemd/system/discord-bot@.service`:
```ini
[Unit]
Description=Discord Support Bot (%i)
After=network.target

[Service]
Type=simple
User=botuser
WorkingDirectory=/opt/support-bot
ExecStart=/usr/bin/python3 main.py --env-dir deployments/%i
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable per server: `sudo systemctl enable --now discord-bot@server-tokyo`

---

## 5. Verify it works

In Discord, type `/` — you should see slash commands:
- `/translate`, `/suggest`, `/shipping`, `/shippingadmin`, `/ticket`, `/welcome`, `/giveaway`

Quick checks:
- `/translate status` → shows provider and enabled channels
- Post `Hello` in a translation-enabled channel → bot replies in Japanese
- `/welcome test` → preview your welcome embed
- `/giveaway create prize:Test duration:30s winners:1` → quick raffle test

---

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `PrivilegedIntentsRequired` | Intents not enabled in Developer Portal | Enable MESSAGE CONTENT + SERVER MEMBERS, restart bot |
| ⚠️ on translated message | Translation provider failed (out of credit / quota / network) | Check provider account, see `support_bot.log` |
| Slash commands missing | `GUILD_ID` not set or sync not finished | Set `GUILD_ID` in `.env` for instant per-guild sync |
| `/shipping` returns "block not configured" | Country block name mismatch with sheet header | Edit `i18n/countries.json` `block` field to match Row 2 of your sheet |
| Bot can't see channels | Permissions not granted on invite | Re-invite via OAuth URL with proper permissions |

---

## 7. Updating

```bash
git pull
pip install -r requirements.txt --upgrade
# restart each running instance
```

Per-server data (giveaways, ticket counter, usage) is preserved in `deployments/<name>/data/`.

---

## 8. Security checklist

- [ ] `.env` files are in `.gitignore` (never committed)
- [ ] `credentials/*.json` are in `.gitignore`
- [ ] Each server has its own bot token (don't share across servers in production)
- [ ] DeepL/Anthropic API keys rotated if leaked
- [ ] Bot role positioned below admin/moderator roles in server settings
