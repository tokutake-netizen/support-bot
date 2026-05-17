# Support Bot ダッシュボード — デプロイ手順

3 ステップで Railway に公開します。所要 10〜15 分。

## 1. Discord アプリの登録(OAuth 用)

ダッシュボードは「ユーザーが Discord アカウントでログインして、自分が admin のサーバーを設定する」仕組みです。そのために OAuth 用の Discord アプリを 1 つ用意します(各サーバーの bot 本体とは別)。

1. https://discord.com/developers/applications を開く → **New Application** で適当な名前(例: `Support Bot Dashboard`)で作成
2. 左メニュー **OAuth2** タブを開く
3. **Client information** セクション:
   - `CLIENT ID` をコピー
   - 「Reset Secret」を押して **Client Secret** を生成 → コピー(一度しか表示されない)
4. **Redirects** セクション → **Add Redirect** ボタン → 以下を貼り付け → 一番下の **Save Changes**:
   - 本番(Railway): `https://<your-railway-domain>/oauth/callback`
   - 開発(ローカル): `http://localhost:8000/oauth/callback`

> ⚠️ **`Save Changes` を押すまで効きません** — 緑色のバナーが下に出るので、それを押し忘れない。
> ⚠️ URL は **完全一致**必要 — 末尾スラッシュなし、`http`/`https` も一致、ポート番号も一致。
> ⚠️ ここで作るのは「ダッシュボードログイン用」のアプリ。各 Discord サーバーで動かす BOT 本体は別の Discord アプリで、ダッシュボードからトークンを入れて使います。

## 2. Railway へデプロイ

1. https://railway.app にログイン → **New Project** → **Deploy from GitHub repo** → `tokutake-netizen/support-bot` を選択
2. **Variables** タブで以下を設定:

   ```
   DISCORD_CLIENT_ID      = (手順1でコピーした Client ID)
   DISCORD_CLIENT_SECRET  = (手順1でコピーした Client Secret)
   DASHBOARD_BASE_URL     = https://<このプロジェクトのRailway URL>
   DASHBOARD_SECRET       = (任意の 32文字以上の文字列)
   DEPLOYMENTS_ROOT       = /data/deployments
   DASHBOARD_AUTOSTART    = 1
   ```

   `DASHBOARD_SECRET` は以下で生成できます:
   ```
   openssl rand -hex 32
   ```

3. **Settings → Volumes** で新規ボリュームを追加:
   - Mount path: `/data`
   - これで設定が再デプロイ後も残ります

4. **Settings → Networking → Generate Domain** で公開 URL を発行

5. Step 1 の Discord アプリ → OAuth2 → Redirects に発行された URL の `/oauth/callback` を追加 → **Save Changes** を必ず押す
   - 例: `https://support-bot-production-xxxx.up.railway.app/oauth/callback`
   - これを忘れるとログイン時に「OAuth2 redirect_uri が無効です」エラー

6. Railway の Variables の `DASHBOARD_BASE_URL` を実際の URL に更新 → 再デプロイ

## 3. 利用開始

1. `https://<your-app>.up.railway.app/` をブラウザで開く
2. 「Discord でログイン」
3. あなたが admin のサーバー一覧が出る → サーバーを選択
4. **Discord BOT トークン**を貼って保存
   - トークン: https://discord.com/developers/applications → 別アプリを作成 → Bot → Reset Token
   - BOT 招待 URL もそのアプリ画面の OAuth2 → URL Generator で生成し、サーバーに招待
5. トークンが入ると、Discord のチャンネル一覧・ロール一覧が自動取得されてプルダウンに表示される
6. 各機能を設定 → 「💾 保存して BOT を再起動」 → BOT が立ち上がる

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| `/healthz` が 200 を返さない | Railway のログを確認、`DASHBOARD_SECRET` が未設定の可能性 |
| 「OAuth2 redirect_uri が無効です」 | Discord アプリ → OAuth2 → Redirects に `<DASHBOARD_BASE_URL>/oauth/callback` を**完全一致**で追加 + **Save Changes** を押したか確認。末尾スラッシュなし、`https://` で `http` ではない |
| OAuth で redirect_uri mismatch | 上に同じ。`DASHBOARD_BASE_URL` の env 値と Discord Redirects の URL がバイト単位で一致してるかチェック |
| ログイン後にサーバー一覧が空 | 対象サーバーで「Administrator」権限を持っていますか? |
| 「Discord 連携失敗」(チャンネル一覧が出ない) | BOT トークンを保存しましたか? BOT を対象サーバーに招待しましたか? |
| 設定が再デプロイ後に消える | Volume を `/data` にマウントし、`DEPLOYMENTS_ROOT=/data/deployments` を設定したか確認 |

## ローカル開発

```bash
pip install -r requirements.txt

export DISCORD_CLIENT_ID=...
export DISCORD_CLIENT_SECRET=...
export DASHBOARD_BASE_URL=http://localhost:8000
export DASHBOARD_SECRET=$(openssl rand -hex 32)
export DASHBOARD_INSECURE_COOKIE=1  # HTTPでもセッションCookieを許可
export DEPLOYMENTS_ROOT=./deployments

uvicorn dashboard.app:app --reload --port 8000
```

ブラウザで http://localhost:8000 を開く。
