"""FastAPI dashboard for support_bot.

Run locally:
    DISCORD_CLIENT_ID=... DISCORD_CLIENT_SECRET=... \
    DASHBOARD_BASE_URL=http://localhost:8000 \
    DASHBOARD_SECRET=$(openssl rand -hex 32) \
    uvicorn dashboard.app:app --reload --port 8000

Production (Railway): see DASHBOARD_DEPLOY.md
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

import httpx
from fastapi import (
    Cookie,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

import random
from datetime import datetime, timezone

from . import (
    auth,
    bot_manager,
    config_store,
    forward_store,
    giveaway_helpers as gh,
    auction_helpers as ah,
    users as user_store,
    mailer,
    server_template,
)
from .discord_api import DiscordREST, assignable_roles, channels_grouped

# Reach into the bot-side cmd_queue helper too.
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from services import cmd_queue  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
log = logging.getLogger("dashboard")

app = FastAPI(title="Support Bot Dashboard")


@app.on_event("startup")
async def autostart_bots() -> None:
    """If DASHBOARD_AUTOSTART=1, start every configured deployment on boot.

    Needed on Railway: containers get restarted on deploy, and we want bots
    to come back up without manual intervention.
    """
    if os.environ.get("DASHBOARD_AUTOSTART") != "1":
        return
    for gid in config_store.list_deployments():
        try:
            bot_manager.start(gid)
            log.info("autostarted bot for guild %s", gid)
        except Exception:
            log.exception("autostart failed for guild %s", gid)

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


# -------------------------- session helpers --------------------------

def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(os.environ["DASHBOARD_SECRET"], salt="dashboard-session")


def set_session(resp: Response, data: dict) -> None:
    token = _serializer().dumps(data)
    resp.set_cookie(
        "session",
        token,
        httponly=True,
        samesite="lax",
        secure=os.environ.get("DASHBOARD_INSECURE_COOKIE") != "1",
        max_age=60 * 60 * 24 * 7,  # 7 days
    )


def get_session(session_cookie: Optional[str]) -> Optional[dict]:
    if not session_cookie:
        return None
    try:
        return _serializer().loads(session_cookie)
    except BadSignature:
        return None


def require_session(session_cookie: Optional[str]) -> dict:
    sess = get_session(session_cookie)
    if not sess:
        raise HTTPException(status_code=401, detail="login required")
    return sess


def require_admin_for_guild(sess: dict, guild_id: str) -> dict:
    # Email-auth users with allowed=True get blanket access to any guild
    # that has a deployment dir on the volume — same as a Discord admin.
    if sess.get("auth_method") == "email":
        return {"id": guild_id, "name": guild_id}
    # Discord auth: session stores admin-only guilds; presence == admin.
    for g in sess.get("guilds", []):
        if str(g["id"]) == str(guild_id):
            return g
    raise HTTPException(status_code=403, detail="not an admin of that guild")


def require_root(sess: dict) -> None:
    if not sess.get("is_root"):
        raise HTTPException(status_code=403, detail="root admin only")


# -------------------------- routes --------------------------

@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def root(request: Request, session: Optional[str] = Cookie(None)):
    if get_session(session):
        return RedirectResponse("/dashboard")
    return RedirectResponse("/login")


@app.get("/login", response_class=HTMLResponse)
async def login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/oauth/start")
async def oauth_start(response: Response):
    state = auth.gen_state()
    try:
        url = auth.build_authorize_url(state)
    except RuntimeError:
        # OAuth 未設定（DISCORD_CLIENT_ID 等なし）。500 で落とさずログインへ戻す。
        return RedirectResponse("/login?err=oauth_unconfigured", status_code=303)
    resp = RedirectResponse(url)
    resp.set_cookie(
        "oauth_state",
        state,
        httponly=True,
        samesite="lax",
        secure=os.environ.get("DASHBOARD_INSECURE_COOKIE") != "1",
        max_age=600,
    )
    return resp


@app.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    oauth_state: Optional[str] = Cookie(None),
):
    if not code or not state or state != oauth_state:
        raise HTTPException(status_code=400, detail="invalid OAuth callback")
    token = await auth.exchange_code(code)
    access_token = token["access_token"]
    user = await auth.fetch_user(access_token)
    raw_guilds = await auth.fetch_user_guilds(access_token)
    # Only keep admin guilds — non-admin ones aren't actionable and bloat the
    # session cookie past the 4 KB browser limit when the user belongs to
    # many servers, causing the cookie to be silently dropped and the login
    # to loop back through /oauth/start. id+name only for the same reason.
    # Keep icon hash (small) so the dashboard can render server avatars.
    guilds = [
        {"id": g["id"], "name": g["name"], "icon": g.get("icon")}
        for g in raw_guilds if auth.is_admin(g)
    ]
    # Grant root to Discord accounts listed in the in-app root allowlist
    # (DASHBOARD_ROOT_DISCORD_ID env bootstrap ∪ entries added from the
    # ユーザー管理 page). Without this, Discord-OAuth logins are never root and
    # root-only pages (e.g. 画像転送) stay hidden.
    sess = {
        "user_id": user["id"],
        "username": user.get("global_name") or user.get("username"),
        "is_root": user_store.is_discord_root(user["id"]),
        "guilds": guilds,
    }
    resp = RedirectResponse("/dashboard")
    set_session(resp, sess)
    resp.delete_cookie("oauth_state")
    return resp


@app.get("/register", response_class=HTMLResponse)
async def register_form(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


@app.post("/register")
async def register_submit(email: str = Form(...)):
    try:
        user_store.request_access(email)
    except ValueError as e:
        return RedirectResponse(f"/register?err={e}", status_code=303)
    return RedirectResponse("/register?ok=1", status_code=303)


@app.get("/forgot", response_class=HTMLResponse)
async def forgot_form(request: Request):
    return templates.TemplateResponse("forgot.html", {"request": request})


@app.post("/forgot")
async def forgot_submit(email: str = Form(...)):
    base = os.environ.get("DASHBOARD_BASE_URL", "").rstrip("/")
    login_url = f"{base}/login" if base else "/login"
    new_pw = user_store.regenerate_password(email)
    if new_pw is None:
        # Always say "if the email exists, we sent it" so we don't leak who's registered.
        return RedirectResponse("/forgot?ok=1", status_code=303)
    if mailer.smtp_configured():
        subject, body = mailer.render_reset_email(email, new_pw, login_url)
        ok, msg = mailer.send(email, subject, body)
        if ok:
            return RedirectResponse("/forgot?ok=1", status_code=303)
        log.warning("forgot: email send failed: %s", msg)
    return RedirectResponse("/forgot?ok=1", status_code=303)


@app.post("/auth/login")
async def auth_login(
    email: str = Form(...),
    password: str = Form(...),
):
    """Email + password authentication, runs alongside Discord OAuth.
    Allowed users (or env-bootstrapped root admin) get a session cookie
    with auth_method="email" and access to every configured guild.
    """
    user = user_store.authenticate(email, password)
    if not user:
        return RedirectResponse("/login?err=auth", status_code=303)

    # For email users, populate the visible guild list from disk so the
    # dashboard.html iteration works the same way as Discord-auth.
    guilds = []
    bot_token = os.environ.get("DISCORD_TOKEN_DEFAULT")
    for gid in config_store.list_deployments():
        name = gid  # placeholder; we don't fetch from Discord here to keep login fast
        env_vals = config_store.read_env(gid)
        tok = env_vals.get("DISCORD_TOKEN_SUPPORT") or bot_token
        if tok:
            try:
                rest = DiscordREST(tok)
                info = await rest.get_guild(gid)
                if info and info.get("name"):
                    name = info["name"]
            except Exception:
                pass
        guilds.append({"id": gid, "name": name, "icon": None})

    sess = {
        "auth_method": "email",
        "username": user["email"],
        "user_id": user["email"],
        "is_root": user.get("is_root", False),
        "guilds": guilds,
    }
    resp = RedirectResponse("/dashboard")
    set_session(resp, sess)
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login")
    resp.delete_cookie("session")
    return resp


# -------------------------- admin: user allowlist --------------------------

@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users(request: Request, session: Optional[str] = Cookie(None)):
    sess = require_session(session)
    require_root(sess)
    return templates.TemplateResponse(
        "admin_users.html",
        {
            "request": request,
            "session": sess,
            "users": user_store.list_users(),
            "discord_roots": user_store.list_discord_roots(),
        },
    )


@app.post("/admin/discord-roots/add")
async def admin_discord_roots_add(
    user_id: str = Form(...),
    label: str = Form(""),
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_root(sess)
    try:
        user_store.add_discord_root(
            user_id, label=label, added_by=sess.get("username") or "root"
        )
    except ValueError as e:
        return RedirectResponse(f"/admin/users?err={e}", status_code=303)
    return RedirectResponse("/admin/users?droot_added=1", status_code=303)


@app.post("/admin/discord-roots/remove")
async def admin_discord_roots_remove(
    user_id: str = Form(...),
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_root(sess)
    user_store.remove_discord_root(user_id)
    return RedirectResponse("/admin/users?droot_removed=1", status_code=303)


@app.post("/admin/users/add")
async def admin_users_add(
    email: str = Form(...),
    password: str = Form(...),
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_root(sess)
    try:
        user_store.add_user(email, password, added_by=sess.get("username") or "root")
    except ValueError as e:
        return RedirectResponse(f"/admin/users?err={e}", status_code=303)
    return RedirectResponse("/admin/users?added=1", status_code=303)


@app.post("/admin/users/remove")
async def admin_users_remove(
    email: str = Form(...),
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_root(sess)
    user_store.remove_user(email)
    return RedirectResponse("/admin/users?removed=1", status_code=303)


@app.post("/admin/users/approve")
async def admin_users_approve(
    email: str = Form(...),
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_root(sess)
    try:
        user_rec, password = user_store.approve_user(email, approved_by=sess.get("username") or "root")
    except ValueError as e:
        return RedirectResponse(f"/admin/users?err={e}", status_code=303)
    base = os.environ.get("DASHBOARD_BASE_URL", "").rstrip("/")
    login_url = f"{base}/login" if base else "/login"
    if mailer.smtp_configured():
        subject, body = mailer.render_approval_email(user_rec["email"], password, login_url)
        ok, msg = mailer.send(user_rec["email"], subject, body)
        if not ok:
            # Fall back to showing the password on the admin screen so the
            # admin can deliver it manually.
            return RedirectResponse(
                f"/admin/users?approved={email}&password={password}&mail_err={msg}",
                status_code=303,
            )
        return RedirectResponse(f"/admin/users?approved={email}&mailed=1", status_code=303)
    # SMTP not configured — surface the password so admin can hand it over manually
    return RedirectResponse(
        f"/admin/users?approved={email}&password={password}&mail_err=smtp_not_configured",
        status_code=303,
    )


@app.post("/admin/users/reject")
async def admin_users_reject(
    email: str = Form(...),
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_root(sess)
    user_store.reject_user(email)
    return RedirectResponse("/admin/users?rejected=1", status_code=303)


@app.post("/admin/users/toggle")
async def admin_users_toggle(
    email: str = Form(...),
    allowed: str = Form(...),
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_root(sess)
    user_store.set_allowed(email, allowed.lower() in ("1", "true", "on", "yes"))
    return RedirectResponse("/admin/users?toggled=1", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, session: Optional[str] = Cookie(None)):
    sess = get_session(session)
    if not sess:
        return RedirectResponse("/login")
    admin_guilds = list(sess.get("guilds", []))
    # Annotate with bot running status
    for g in admin_guilds:
        g["bot_running"] = bot_manager.is_running(str(g["id"]))
        g["configured"] = (config_store.deployment_dir(str(g["id"])) / ".env").exists()
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "session": sess, "guilds": admin_guilds},
    )


@app.get("/guide", response_class=HTMLResponse)
async def guide(request: Request, session: Optional[str] = Cookie(None)):
    sess = get_session(session)
    if not sess:
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        "guide.html",
        {"request": request, "session": sess},
    )


# -------------------------- AIアシスタント（Gemini） --------------------------

ASSISTANT_SYSTEM_PROMPT = """\
あなたは「Support Bot ダッシュボード」に組み込まれたアシスタントです。
スタッフ（ダッシュボード利用者）の質問に日本語で簡潔に答えてください。

このダッシュボードでできること:
- サーバー一覧: 管理している Discord サーバーごとに Bot の起動/停止/再起動、セットアップ
- 画像転送 (/forwarding, root のみ): 梱包写真を社内チャンネルから顧客サーバーへ自動転送する
  ルールの追加・削除。Discord 側の /forward add/list/remove コマンドでも同じルールを編集可能
- チケット: お客様がパネルから問い合わせチケットを作成。カテゴリが50件で満杯になると
  「Created Tickets 2」のように自動ナンバリングした新カテゴリが作られチケットはそこに入る
- ギブアウェイ/オークション、翻訳、FAQ、ウェルカムメッセージ、招待トラッカー等の各機能設定
- ユーザー管理 (root のみ): ダッシュボードへのログインユーザーの承認・管理

運用の背景: 日本からトレーディングカードを海外顧客に販売する事業。取引の活発さを
顧客に見せるため梱包写真を顧客サーバーに転送している。

わからないことは推測せず「ダッシュボードの該当ページを確認してください」と案内してください。
"""

TRANSLATE_EN_PROMPT = """\
あなたは翻訳ツールです。入力された日本語を、海外のトレーディングカード顧客への
Discordメッセージとして自然でフレンドリーかつプロフェッショナルな英語に翻訳してください。
- 翻訳結果の英文のみを出力する（説明・前置き・引用符は不要）
- 絵文字や記号は原文の雰囲気に合わせて適度に残す
- カード名・PSA等級・発送用語などは業界の慣用表記（PSA 10, raw card, tracked shipping 等）に合わせる
- 入力がすでに英語の場合は、より自然な英語に磨いて出力する
"""

TRANSLATE_JA_PROMPT = """\
あなたは翻訳ツールです。入力された英語（海外顧客からのメッセージ等）を自然な日本語に翻訳してください。
- 翻訳結果のみを出力する（説明・前置きは不要）
- スラング・略語（LMK, WTB, PWE 等）は意味が伝わる日本語にする
- 金額・カード名・等級などの固有情報は正確に保つ
"""

ASSISTANT_MODES = {
    "chat": ASSISTANT_SYSTEM_PROMPT,
    "en": TRANSLATE_EN_PROMPT,
    "ja": TRANSLATE_JA_PROMPT,
}


@app.post("/api/assistant/chat")
async def assistant_chat(request: Request, session: Optional[str] = Cookie(None)):
    require_session(session)
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise HTTPException(
            status_code=503,
            detail="GEMINI_API_KEY が未設定です。Railway の Variables に追加してください。",
        )
    body = await request.json()
    mode = body.get("mode") or "chat"
    system_prompt = ASSISTANT_MODES.get(mode, ASSISTANT_SYSTEM_PROMPT)
    messages = body.get("messages") or []
    contents = []
    for m in messages[-20:]:  # 直近20往復だけ送る
        role = "model" if m.get("role") == "assistant" else "user"
        text = str(m.get("content", ""))[:4000]
        if text.strip():
            contents.append({"role": role, "parts": [{"text": text}]})
    if mode != "chat":
        # 翻訳モードでは過去の会話を混ぜず、最後の入力だけを翻訳する
        contents = [c for c in contents if c["role"] == "user"][-1:]
    if not contents:
        raise HTTPException(status_code=400, detail="メッセージが空です")

    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": 1024, "temperature": 0.7},
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": key},
            json=payload,
        )
    if r.status_code != 200:
        log.error("gemini api error %s: %s", r.status_code, r.text[:300])
        raise HTTPException(status_code=502, detail=f"Gemini API エラー (HTTP {r.status_code})")
    data = r.json()
    try:
        reply = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        log.error("gemini unexpected response: %s", json.dumps(data)[:300])
        raise HTTPException(status_code=502, detail="Gemini の応答を解釈できませんでした")
    return {"reply": reply}


# -------------------------- 画像転送ルート --------------------------

@app.get("/forwarding", response_class=HTMLResponse)
async def forwarding_page(request: Request, session: Optional[str] = Cookie(None)):
    sess = require_session(session)
    require_root(sess)

    token = forward_store.forward_bot_token()
    servers: list = []
    discord_ok = False
    if token:
        try:
            servers = await forward_store.list_servers_with_channels(token)
            discord_ok = bool(servers)
        except Exception as e:
            log.warning("forwarding: failed to list servers: %s", e)

    ch_index = forward_store.index_channels(servers)

    rules = []
    for r in forward_store.load_rules():
        src_id = str(r.get("source"))
        dst_id = str(r.get("dest"))
        src = ch_index.get(src_id)
        dst = ch_index.get(dst_id)
        rules.append(
            {
                "source": src_id,
                "dest": dst_id,
                "source_label": f"{src['server']} ＞ #{src['channel']}" if src else f"(ID: {src_id})",
                "dest_label": f"{dst['server']} ＞ #{dst['channel']}" if dst else f"(ID: {dst_id})",
            }
        )

    return templates.TemplateResponse(
        "forwarding.html",
        {
            "request": request,
            "session": sess,
            "servers": servers,
            "rules": rules,
            "discord_ok": discord_ok,
            "has_token": bool(token),
        },
    )


@app.post("/forwarding/add")
async def forwarding_add(
    request: Request,
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_root(sess)
    form = await request.form()
    try:
        source = int((form.get("source_channel") or "").strip())
        dest = int((form.get("dest_channel") or "").strip())
    except ValueError:
        return RedirectResponse("/forwarding?err=invalid", status_code=303)
    if source == dest:
        return RedirectResponse("/forwarding?err=same", status_code=303)
    added = forward_store.add_rule(source, dest)
    return RedirectResponse(
        "/forwarding?ok=added" if added else "/forwarding?err=dup", status_code=303
    )


@app.post("/forwarding/remove")
async def forwarding_remove(
    request: Request,
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_root(sess)
    form = await request.form()
    try:
        source = int((form.get("source") or "").strip())
        dest = int((form.get("dest") or "").strip())
    except ValueError:
        return RedirectResponse("/forwarding?err=invalid", status_code=303)
    forward_store.remove_rule(source, dest)
    return RedirectResponse("/forwarding?ok=removed", status_code=303)


def _bot_token_for(guild_id: str) -> Optional[str]:
    env = config_store.read_env(guild_id)
    return env.get("DISCORD_TOKEN_SUPPORT") or os.environ.get("DISCORD_TOKEN_DEFAULT")


@app.get("/guild/{guild_id}/setup", response_class=HTMLResponse)
async def guild_setup(
    request: Request,
    guild_id: str,
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)

    config_store.ensure_deployment(guild_id)
    env_vals = config_store.read_env(guild_id)
    bot_token = _bot_token_for(guild_id)
    # Surface the auto-fetched fuel surcharge cache so the Shipping tab can
    # show "last fetched" alongside the manual override fields.
    fuel_cache: dict = {}
    fuel_path = config_store.deployment_dir(guild_id) / "data" / "fuel_surcharge.json"
    if fuel_path.exists():
        try:
            import json
            fuel_cache = json.loads(fuel_path.read_text("utf-8"))
        except Exception:
            fuel_cache = {}

    channels: list = []
    grouped: list = []
    roles: list = []
    discord_ok = False
    if bot_token:
        rest = DiscordREST(bot_token)
        guild_info = await rest.get_guild(guild_id)
        if guild_info:
            discord_ok = True
            channels = await rest.list_channels(guild_id)
            grouped = channels_grouped(channels)
            all_roles = await rest.list_roles(guild_id)
            roles = assignable_roles(all_roles)

    # Message-forwarding rules whose SOURCE channel is in this guild, so each
    # server manages its own forwarding from its settings page (not root-only).
    ch_names = {str(c.get("id")): c.get("name") for c in channels}
    guild_ch_ids = set(ch_names)
    forward_rules = []
    try:
        for r in forward_store.load_rules():
            src = str(r.get("source"))
            if src not in guild_ch_ids:
                continue
            dst = str(r.get("dest"))
            forward_rules.append({
                "source": src,
                "dest": dst,
                "source_label": f"#{ch_names.get(src, src)}",
                "dest_label": f"#{ch_names[dst]}" if dst in ch_names else f"(ID: {dst})",
            })
    except Exception:
        log.warning("setup: failed to load forward rules", exc_info=True)

    return templates.TemplateResponse(
        "setup.html",
        {
            "request": request,
            "session": sess,
            "guild_id": guild_id,
            "guild_name": next(
                (g["name"] for g in sess["guilds"] if str(g["id"]) == str(guild_id)),
                guild_id,
            ),
            "env": env_vals,
            "channels_grouped": grouped,
            "roles": roles,
            "discord_ok": discord_ok,
            "bot_running": bot_manager.is_running(guild_id),
            "fuel_cache": fuel_cache,
            "forward_rules": forward_rules,
        },
    )


@app.post("/guild/{guild_id}/forwarding/add")
async def guild_forwarding_add(
    guild_id: str,
    request: Request,
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    form = await request.form()
    try:
        source = int((form.get("source_channel") or "").strip())
        dest = int((form.get("dest_channel") or "").strip())
    except ValueError:
        return JSONResponse({"ok": False, "error": "チャンネルIDが不正です"}, status_code=400)
    if source == dest:
        return JSONResponse({"ok": False, "error": "送信元と転送先が同じです"}, status_code=400)
    added = forward_store.add_rule(source, dest)
    return JSONResponse({"ok": True, "added": bool(added)})


@app.post("/guild/{guild_id}/forwarding/remove")
async def guild_forwarding_remove(
    guild_id: str,
    request: Request,
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    form = await request.form()
    try:
        source = int((form.get("source") or "").strip())
        dest = int((form.get("dest") or "").strip())
    except ValueError:
        return JSONResponse({"ok": False, "error": "チャンネルIDが不正です"}, status_code=400)
    forward_store.remove_rule(source, dest)
    return JSONResponse({"ok": True})


@app.post("/guild/{guild_id}/save")
async def guild_save(
    request: Request,
    guild_id: str,
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    form = await request.form()

    # Multi-select fields arrive as repeated values; collect them into comma-separated.
    multi_keys = {
        "TRANSLATE_CHANNEL_IDS",
        "TICKET_CATEGORY_IDS",
        "TICKET_STAFF_ROLE_IDS",
        "WELCOME_AUTOROLE_IDS",
        "INVITE_CREATOR_ROLE_IDS",
        "GIVEAWAY_MANAGER_ROLE_IDS",
        "AUCTION_MANAGER_ROLE_IDS",
        "ALLOW_CH_SHIPPING",
        "ALLOW_CAT_SHIPPING",
        "ALLOW_CH_GIVEAWAY",
        "ALLOW_CH_INVITE",
        "ALLOW_CH_AUCTION",
        "ALLOW_CAT_AUCTION",
    }
    updates: dict[str, str] = {}
    for k in form.keys():
        if k in multi_keys:
            vals = [v for v in form.getlist(k) if v]
            updates[k] = ",".join(vals)
        else:
            v = form.get(k)
            if v is None:
                continue
            updates[k] = str(v)

    # Auto-extract Google Sheet ID if user pasted a URL
    if "SHIPPING_SHEET_ID" in updates:
        sid = config_store.extract_sheet_id(updates["SHIPPING_SHEET_ID"])
        if sid:
            updates["SHIPPING_SHEET_ID"] = sid

    config_store.write_env(guild_id, updates)

    # Verify Discord auth right after save so the user gets immediate feedback.
    # We call /guilds/{id} with the bot token; success means the token is valid AND
    # the bot is already in this guild.
    auth_status = "unknown"
    bot_token = (
        updates.get("DISCORD_TOKEN_SUPPORT")
        or _bot_token_for(guild_id)
    )
    if bot_token:
        rest = DiscordREST(bot_token)
        try:
            guild_info = await rest.get_guild(guild_id)
            if guild_info:
                auth_status = "ok"
            else:
                # Token works (no httpx error) but the bot can't see this guild.
                auth_status = "not_in_guild"
        except Exception as e:
            log.warning("post-save Discord auth check failed: %s", e)
            auth_status = "invalid_token"

    bot_manager.restart(guild_id)
    return RedirectResponse(
        f"/guild/{guild_id}/setup?saved=1&auth={auth_status}", status_code=303
    )


@app.post("/guild/{guild_id}/credential")
async def upload_credential(
    guild_id: str,
    session: Optional[str] = Cookie(None),
    service_account: UploadFile = File(...),
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    contents = await service_account.read()
    if not contents:
        raise HTTPException(status_code=400, detail="empty file")
    try:
        json.loads(contents)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"not a JSON file: {e}")
    config_store.write_credential_file(guild_id, "service_account.json", contents)
    config_store.write_env(
        guild_id, {"GOOGLE_SERVICE_ACCOUNT_JSON": "./credentials/service_account.json"}
    )
    return RedirectResponse(f"/guild/{guild_id}/setup?saved=1", status_code=303)


@app.post("/guild/{guild_id}/start")
async def guild_start(guild_id: str, session: Optional[str] = Cookie(None)):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    try:
        st = bot_manager.start(guild_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(st)


@app.post("/guild/{guild_id}/stop")
async def guild_stop(guild_id: str, session: Optional[str] = Cookie(None)):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    return JSONResponse(bot_manager.stop(guild_id))


@app.post("/guild/{guild_id}/restart")
async def guild_restart(guild_id: str, session: Optional[str] = Cookie(None)):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    return JSONResponse(bot_manager.restart(guild_id))


@app.get("/guild/{guild_id}/status", response_class=HTMLResponse)
async def guild_status_page(
    request: Request,
    guild_id: str,
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    bot_token = _bot_token_for(guild_id)
    channels_groups: list = []
    if bot_token:
        rest = DiscordREST(bot_token)
        chs = await rest.list_channels(guild_id)
        channels_groups = channels_grouped(chs)
    base_dir = config_store.deployment_dir(guild_id)
    recent_cmds = cmd_queue.recent(limit=15, base_dir=base_dir)
    # Surface the bot-side fuel surcharge cache so the status page UI knows
    # the auto-fetched values + when they were last fetched.
    fuel_cache: dict = {}
    fuel_path = base_dir / "data" / "fuel_surcharge.json"
    if fuel_path.exists():
        try:
            import json
            fuel_cache = json.loads(fuel_path.read_text("utf-8"))
        except Exception:
            fuel_cache = {}
    return templates.TemplateResponse(
        "status.html",
        {
            "request": request,
            "session": sess,
            "guild_id": guild_id,
            "guild_name": next(
                (g["name"] for g in sess["guilds"] if str(g["id"]) == str(guild_id)),
                guild_id,
            ),
            "status": bot_manager.status(guild_id),
            "log_tail": bot_manager.tail_log(guild_id, lines=200),
            "channels_grouped": channels_groups,
            "recent_cmds": recent_cmds,
            "allowed_actions": sorted(cmd_queue.ALLOWED_ACTIONS),
            "fuel_cache": fuel_cache,
        },
    )


@app.get("/guild/{guild_id}/log.txt")
async def guild_log(
    guild_id: str,
    lines: int = 200,
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    return Response(bot_manager.tail_log(guild_id, lines=lines), media_type="text/plain")


# -------------------------- server template --------------------------

@app.post("/guild/{guild_id}/template/export")
async def template_export(
    request: Request,
    guild_id: str,
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    bot_token = _bot_token_for(guild_id)
    if not bot_token:
        raise HTTPException(status_code=400, detail="BOT token not configured")
    env_vals = config_store.read_env(guild_id)
    ticket_cat = env_vals.get("TICKET_CATEGORY_ID") or None
    snap = await server_template.snapshot_guild(bot_token, guild_id, ticket_category_id=ticket_cat)
    server_template.save_template(snap)
    return RedirectResponse(
        f"/guild/{guild_id}/setup"
        f"?template_exported=1"
        f"&template_cats={len(snap['categories'])}"
        f"&template_chs={sum(len(c['channels']) for c in snap['categories']) + len(snap['orphan_channels'])}",
        status_code=303,
    )


@app.post("/guild/{guild_id}/template/apply")
async def template_apply(
    guild_id: str,
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    bot_token = _bot_token_for(guild_id)
    if not bot_token:
        raise HTTPException(status_code=400, detail="BOT token not configured")
    template = server_template.load_template()
    if not template:
        raise HTTPException(status_code=400, detail="no template saved yet — export from a source guild first")
    summary = await server_template.apply_template(bot_token, guild_id, template)
    import urllib.parse as _u
    return RedirectResponse(
        f"/guild/{guild_id}/setup?template_applied=1"
        f"&created_cats={len(summary['created_categories'])}"
        f"&created_chs={len(summary['created_channels'])}"
        f"&skipped={len(summary['skipped_categories']) + len(summary['skipped_channels'])}"
        f"&errors={_u.quote(' / '.join(summary['errors'][:3]))}",
        status_code=303,
    )


# -------------------------- command queue --------------------------

@app.post("/guild/{guild_id}/cmd")
async def guild_cmd(
    request: Request,
    guild_id: str,
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    form = await request.form()
    action = (form.get("action") or "").strip()
    if not action:
        raise HTTPException(status_code=400, detail="action required")
    params: dict = {}
    # Forward known optional params to the bot-side handler. New whitelist
    # entries here when a new action needs more parameters.
    for k in ("channel_id", "user_id"):
        v = form.get(k)
        if v:
            params[k] = v
    # The admin's own user id is useful for "test post" style actions so
    # the embed renders against a real member. Default to session user.
    if "user_id" not in params:
        params["user_id"] = str(sess.get("user_id") or "")
    base_dir = config_store.deployment_dir(guild_id)
    try:
        cmd_queue.enqueue(action, params, base_dir=base_dir)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(
        f"/guild/{guild_id}/status?queued={action}", status_code=303
    )


@app.get("/guild/{guild_id}/cmd/history.json")
async def guild_cmd_history(
    guild_id: str,
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    base_dir = config_store.deployment_dir(guild_id)
    return JSONResponse(cmd_queue.recent(limit=20, base_dir=base_dir))


# -------------------------- giveaway routes --------------------------

def _split_giveaways(data: dict[str, dict]) -> tuple[list[tuple[str, dict]], list[tuple[str, dict]]]:
    active = [(mid, gw) for mid, gw in data.items() if not gw.get("ended")]
    ended = [(mid, gw) for mid, gw in data.items() if gw.get("ended")]
    # sort active by ends_at asc, ended by ends_at desc
    active.sort(key=lambda x: x[1].get("ends_at", ""))
    ended.sort(key=lambda x: x[1].get("ends_at", ""), reverse=True)
    return active, ended


def _guild_name_from_session(sess: dict, guild_id: str) -> str:
    return next(
        (g["name"] for g in sess["guilds"] if str(g["id"]) == str(guild_id)),
        guild_id,
    )


@app.get("/guild/{guild_id}/giveaway", response_class=HTMLResponse)
async def giveaway_page(
    request: Request,
    guild_id: str,
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    bot_token = _bot_token_for(guild_id)

    channels_groups: list = []
    roles: list = []
    if bot_token:
        rest = DiscordREST(bot_token)
        chs = await rest.list_channels(guild_id)
        channels_groups = channels_grouped(chs)
        rls = await rest.list_roles(guild_id)
        roles = assignable_roles(rls)

    data = gh.load_giveaways(guild_id)
    active, ended = _split_giveaways(data)

    # Pre-compute friendly time-remaining for the template
    now = datetime.now(timezone.utc)
    for mid, gw in active:
        try:
            ends_at = datetime.fromisoformat(gw["ends_at"])
            gw["_remaining"] = gh.fmt_duration(max(0, int((ends_at - now).total_seconds())))
        except Exception:
            gw["_remaining"] = "?"

    return templates.TemplateResponse(
        "giveaway.html",
        {
            "request": request,
            "session": sess,
            "guild_id": guild_id,
            "guild_name": _guild_name_from_session(sess, guild_id),
            "channels_grouped": channels_groups,
            "roles": roles,
            "active": active,
            "ended": ended,
            "discord_ok": bool(bot_token and channels_groups),
        },
    )


@app.post("/guild/{guild_id}/giveaway/create")
async def giveaway_create(
    request: Request,
    guild_id: str,
    session: Optional[str] = Cookie(None),
    prize: str = Form(...),
    duration: str = Form(...),
    winners: int = Form(1),
    channel_id: str = Form(...),
    required_role_id: Optional[str] = Form(None),
    image_url: Optional[str] = Form(None),
    note: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None),
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)

    secs = gh.parse_duration(duration)
    if secs is None:
        raise HTTPException(status_code=400, detail="invalid duration; use 30s / 5m / 1h / 1d2h")
    if winners < 1 or winners > 50:
        raise HTTPException(status_code=400, detail="winners must be 1–50")

    bot_token = _bot_token_for(guild_id)
    if not bot_token:
        raise HTTPException(status_code=400, detail="BOT token not configured for this guild")

    # Resolve image: uploaded file wins over URL field
    image_bytes: Optional[bytes] = None
    image_filename: Optional[str] = None
    resolved_image_url: Optional[str] = None
    if image is not None and image.filename:
        image_bytes = await image.read()
        if image_bytes:
            image_filename = image.filename
            resolved_image_url = f"attachment://{image_filename}"
    elif image_url:
        resolved_image_url = image_url.strip() or None

    gw = {
        "channel_id": int(channel_id),
        "guild_id": int(guild_id),
        "prize": prize,
        "winner_count": winners,
        "ends_at": gh.future_iso(secs),
        "host_id": int(sess["user_id"]),
        "required_role_id": int(required_role_id) if required_role_id and required_role_id.isdigit() else None,
        "image_url": resolved_image_url,
        "note": (note or "").strip() or None,
        "entries": [],
        "ended": False,
        "winners": [],
    }

    rest = DiscordREST(bot_token)
    payload = {
        "embeds": [gh.build_giveaway_embed(gw)],
        "components": [gh.enter_button_component()],
    }
    try:
        msg = await rest.create_message(
            channel_id,
            payload,
            image_bytes=image_bytes,
            image_filename=image_filename,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Discord create_message failed: {e}")

    # If we uploaded a file, swap the placeholder attachment URL for the
    # actual CDN URL Discord returned. That way reload/end re-render works.
    if image_bytes and msg.get("attachments"):
        att = msg["attachments"][0]
        gw["image_url"] = att.get("url")

    gh.add_giveaway(guild_id, msg["id"], gw)
    return RedirectResponse(f"/guild/{guild_id}/giveaway?created={msg['id']}", status_code=303)


@app.post("/guild/{guild_id}/auction/create")
async def auction_create(
    request: Request,
    guild_id: str,
    session: Optional[str] = Cookie(None),
    title: str = Form(...),
    starting_bid: int = Form(...),
    duration: str = Form(...),
    channel_id: str = Form(...),
    description: Optional[str] = Form(None),
    reserve_price: int = Form(0),
    min_increment: int = Form(100),
    anti_snipe_window: int = Form(300),
    anti_snipe_extend: int = Form(30),
    image_url: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None),
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)

    secs = ah.parse_duration(duration)
    if secs is None or secs < 60:
        raise HTTPException(status_code=400, detail="duration must be >= 1m, e.g. 12h / 1d")
    if starting_bid < 1 or min_increment < 1:
        raise HTTPException(status_code=400, detail="starting_bid and min_increment must be positive")
    if reserve_price and reserve_price < starting_bid:
        raise HTTPException(status_code=400, detail="reserve_price must be >= starting_bid (or 0)")

    bot_token = _bot_token_for(guild_id)
    if not bot_token:
        raise HTTPException(status_code=400, detail="BOT token not configured")

    image_bytes: Optional[bytes] = None
    image_filename: Optional[str] = None
    resolved_image_url: Optional[str] = None
    if image is not None and image.filename:
        image_bytes = await image.read()
        if image_bytes:
            image_filename = image.filename
    elif image_url:
        resolved_image_url = image_url.strip() or None

    auction = {
        "guild_id": int(guild_id),
        "channel_id": int(channel_id),
        "thread_id": None,
        "title": title,
        "description": (description or "").strip(),
        "image_url": resolved_image_url,
        "image_filename": image_filename,
        "host_id": int(sess["user_id"]),
        "starting_bid": int(starting_bid),
        "min_increment": int(min_increment),
        "reserve_price": int(reserve_price),
        "currency": "JPY",
        "ends_at": ah.future_iso(secs),
        "anti_snipe_threshold": int(anti_snipe_window),
        "anti_snipe_seconds": int(anti_snipe_extend),
        "bids": [],
        "ended": False,
        "cancelled": False,
        "winner": None,
        "winning_bid": 0,
    }

    rest = DiscordREST(bot_token)
    payload = {"embeds": [ah.build_embed(auction)], "components": ah.view_components()}
    try:
        msg = await rest.create_message(
            channel_id, payload,
            image_bytes=image_bytes, image_filename=image_filename,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Discord create_message failed: {e}")

    # Swap placeholder attachment:// for the real CDN URL Discord returned
    if image_bytes and msg.get("attachments"):
        auction["image_url"] = msg["attachments"][0].get("url")
        auction["image_filename"] = None  # CDN URL takes over; future re-renders use image_url

    ah.add_auction(guild_id, msg["id"], auction)
    return RedirectResponse(
        f"/guild/{guild_id}/setup?auction_created={msg['id']}#tab-auction", status_code=303
    )


@app.post("/guild/{guild_id}/giveaway/{message_id}/end")
async def giveaway_end(
    guild_id: str,
    message_id: str,
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)

    data = gh.load_giveaways(guild_id)
    gw = data.get(str(message_id))
    if not gw:
        raise HTTPException(status_code=404, detail="giveaway not found")
    if gw.get("ended"):
        return RedirectResponse(f"/guild/{guild_id}/giveaway?ended={message_id}", status_code=303)

    # Pick winners now (mirror cogs/giveaway.py _end_giveaway)
    entries: list[int] = gw.get("entries", [])
    n = min(max(1, int(gw.get("winner_count", 1))), len(entries)) if entries else 0
    chosen = random.sample(entries, n) if n else []
    gw["ended"] = True
    gw["winners"] = chosen
    data[str(message_id)] = gw
    gh.save_giveaways(guild_id, data)

    bot_token = _bot_token_for(guild_id)
    if bot_token:
        rest = DiscordREST(bot_token)
        try:
            await rest.patch_message(
                gw["channel_id"],
                message_id,
                {
                    "embeds": [gh.build_giveaway_embed(gw, ended=True)],
                    "components": [gh.enter_button_component(disabled=True)],
                },
            )
        except Exception as e:
            log.warning("could not patch ended giveaway %s: %s", message_id, e)
        # Post winner announcement
        try:
            if chosen:
                mentions = " ".join(f"<@{w}>" for w in chosen)
                await rest.create_message(
                    gw["channel_id"],
                    {"content": f"🎊 Congratulations {mentions}!\n🎁 You won **{gw['prize']}** — the host <@{gw['host_id']}> will reach out shortly."},
                )
            else:
                await rest.create_message(
                    gw["channel_id"], {"content": "⚠️ No entries — no winners this time."}
                )
        except Exception:
            log.exception("winner announcement failed")

    return RedirectResponse(f"/guild/{guild_id}/giveaway?ended={message_id}", status_code=303)


# -------------------------- onboarding routes --------------------------

# Discord's hard limits.
ONBOARDING_MAX_PROMPTS = 5
ONBOARDING_MAX_OPTIONS = 8


def _parse_emoji(text: str) -> Optional[dict]:
    """Convert a single emoji string (unicode or <:name:id>) into Discord's emoji shape."""
    s = (text or "").strip()
    if not s:
        return None
    if s.startswith("<") and s.endswith(">") and ":" in s:
        try:
            inner = s.strip("<>")
            parts = inner.split(":")
            if len(parts) == 3:
                animated = parts[0] == "a"
                return {"id": parts[2], "name": parts[1], "animated": animated}
        except Exception:
            pass
    return {"name": s, "id": None}


def _normalize_prompt(prompt: dict) -> dict:
    """Convert a Discord onboarding prompt into the template's flat dict shape."""
    options = []
    for opt in prompt.get("options", []):
        emoji = opt.get("emoji") or {}
        emoji_str = ""
        if emoji.get("id"):
            ap = "a" if emoji.get("animated") else ""
            emoji_str = f"<{ap}:{emoji.get('name','')}:{emoji['id']}>"
        elif emoji.get("name"):
            emoji_str = emoji["name"]
        options.append({
            "emoji": emoji_str,
            "title": opt.get("title", ""),
            "description": opt.get("description", "") or "",
            "role_ids": [str(x) for x in opt.get("role_ids", [])],
            "channel_ids": [str(x) for x in opt.get("channel_ids", [])],
        })
    while len(options) < ONBOARDING_MAX_OPTIONS:
        options.append({"emoji": "", "title": "", "description": "", "role_ids": [], "channel_ids": []})
    return {
        "title": prompt.get("title", ""),
        "single_select": prompt.get("single_select", True),
        "required": prompt.get("required", True),
        "options": options,
    }


def _empty_prompt() -> dict:
    return {
        "title": "",
        "single_select": True,
        "required": False,
        "options": [
            {"emoji": "", "title": "", "description": "", "role_ids": [], "channel_ids": []}
            for _ in range(ONBOARDING_MAX_OPTIONS)
        ],
    }


@app.get("/guild/{guild_id}/onboarding", response_class=HTMLResponse)
async def onboarding_page(
    request: Request,
    guild_id: str,
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    bot_token = _bot_token_for(guild_id)

    guild_info = None
    onboarding = None
    channels_groups: list = []
    roles: list = []
    if bot_token:
        rest = DiscordREST(bot_token)
        guild_info = await rest.get_guild(guild_id)
        onboarding = await rest.get_onboarding(guild_id)
        chs = await rest.list_channels(guild_id)
        channels_groups = channels_grouped(chs)
        rls = await rest.list_roles(guild_id)
        roles = assignable_roles(rls)

    existing_prompts = (onboarding or {}).get("prompts", []) or []
    prompts = [_normalize_prompt(p) for p in existing_prompts[:ONBOARDING_MAX_PROMPTS]]
    while len(prompts) < ONBOARDING_MAX_PROMPTS:
        prompts.append(_empty_prompt())

    is_community = bool(guild_info and "COMMUNITY" in (guild_info.get("features") or []))

    return templates.TemplateResponse(
        "onboarding.html",
        {
            "request": request,
            "session": sess,
            "guild_id": guild_id,
            "guild_name": _guild_name_from_session(sess, guild_id),
            "channels_grouped": channels_groups,
            "roles": roles,
            "discord_ok": bool(bot_token and channels_groups),
            "is_community": is_community,
            "rules_channel_id": str((guild_info or {}).get("rules_channel_id") or ""),
            "onboarding_enabled": bool(onboarding and onboarding.get("enabled")),
            "default_channel_ids": [str(x) for x in (onboarding or {}).get("default_channel_ids", [])],
            "prompts": prompts,
            "MAX_PROMPTS": ONBOARDING_MAX_PROMPTS,
            "MAX_OPTIONS": ONBOARDING_MAX_OPTIONS,
        },
    )


@app.post("/guild/{guild_id}/onboarding/save")
async def onboarding_save(
    request: Request,
    guild_id: str,
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    bot_token = _bot_token_for(guild_id)
    if not bot_token:
        raise HTTPException(status_code=400, detail="BOT token not configured")

    form = await request.form()
    rest = DiscordREST(bot_token)

    rules_ch = form.get("rules_channel_id") or ""
    try:
        await rest.patch_guild(
            guild_id, {"rules_channel_id": int(rules_ch) if rules_ch else None}
        )
    except Exception as e:
        log.warning("patch_guild rules_channel_id failed: %s", e)

    enabled = form.get("enabled") == "1"
    default_channel_ids = [v for v in form.getlist("default_channel_ids") if v]

    prompts: list[dict] = []
    for pi in range(ONBOARDING_MAX_PROMPTS):
        title = (form.get(f"p{pi}_title") or "").strip()
        if not title:
            continue
        options: list[dict] = []
        for oj in range(ONBOARDING_MAX_OPTIONS):
            opt_title = (form.get(f"p{pi}_opt_title_{oj}") or "").strip()
            if not opt_title:
                continue
            emoji_text = form.get(f"p{pi}_opt_emoji_{oj}") or ""
            desc = (form.get(f"p{pi}_opt_desc_{oj}") or "").strip()
            role_ids = [v for v in form.getlist(f"p{pi}_opt_role_ids_{oj}") if v]
            channel_ids = [v for v in form.getlist(f"p{pi}_opt_channel_ids_{oj}") if v]
            options.append({
                "title": opt_title,
                "description": desc or None,
                "emoji": _parse_emoji(emoji_text),
                "role_ids": role_ids,
                "channel_ids": channel_ids,
            })
        if not options:
            continue
        prompts.append({
            "id": str(pi),
            "type": 0,
            "title": title,
            "options": options,
            "single_select": form.get(f"p{pi}_single_select") == "1",
            "required": form.get(f"p{pi}_required") == "1",
            "in_onboarding": True,
        })

    payload = {
        "prompts": prompts,
        "default_channel_ids": default_channel_ids,
        "enabled": enabled,
        "mode": 0,
    }

    try:
        await rest.put_onboarding(guild_id, payload)
    except Exception as e:
        log.exception("put_onboarding failed")
        raise HTTPException(status_code=502, detail=f"Discord put_onboarding failed: {e}")

    return RedirectResponse(f"/guild/{guild_id}/onboarding?saved=1", status_code=303)
