"""FastAPI dashboard for support_bot.

Run locally:
    DISCORD_CLIENT_ID=... DISCORD_CLIENT_SECRET=... \
    DASHBOARD_BASE_URL=http://localhost:8000 \
    DASHBOARD_SECRET=$(openssl rand -hex 32) \
    uvicorn dashboard.app:app --reload --port 8000

Production (Railway): see DASHBOARD_DEPLOY.md
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
import urllib.parse
import json
import logging
import os
from pathlib import Path
from typing import Optional

import httpx
from fastapi import (
    BackgroundTasks,
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
    legal,
    giveaway_helpers as gh,
    auction_helpers as ah,
    users as user_store,
    mailer,
    line_forward,
    member_stats,
    products_store,
    schedule_store,
    server_template,
    shipping_master,
    signups,
    tenants,
)
from .discord_api import DiscordREST, assignable_roles, channels_grouped

# Reach into the bot-side cmd_queue helper too.
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from services import cmd_queue  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
log = logging.getLogger("dashboard")

app = FastAPI(title="Musubot Dashboard")


@app.on_event("startup")
async def bootstrap_tenants() -> None:
    """起動時にテナント台帳を用意し、旧ユーザーの担当サーバーを確定させる。

    担当が未設定のユーザーは「全サーバーが見える」フォールバックで動く。
    1社運用のうちは正しかったが、他社を迎えたあとは新しい会社のサーバーまで
    見えてしまう。起動のたびに、取りこぼしたユーザーへ現時点の担当を付与する。
    """
    try:
        tenants.ensure_bootstrap()
        n = user_store.migrate_existing_users(
            tenants.guilds_for_tenant(tenants.DEFAULT_SLUG)
        )
        if n:
            log.info("担当サーバーが未設定だった %d 人に、既定会社の担当を付与しました", n)
    except Exception:  # noqa: BLE001 — 起動そのものは止めない
        log.exception("テナント台帳の初期化に失敗しました")


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

@app.exception_handler(401)
async def unauthorized_to_login(request: Request, exc: HTTPException):
    """ログインが必要なページに未ログインで来たら、ログイン画面へ返す。

    ロボット個別ページのような深いURLをブックマークしてもらう作りなので、
    生の 401 JSON ではなくログイン画面を見せる。API 呼び出し（fetch）には
    従来どおり JSON を返す。
    """
    accepts_html = "text/html" in (request.headers.get("accept") or "")
    if accepts_html and request.method == "GET":
        return RedirectResponse("/login", status_code=303)
    return JSONResponse({"detail": exc.detail}, status_code=401)


HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


def _asset_version() -> str:
    """style.css の内容ハッシュ。

    StaticFiles は Cache-Control を付けないため、ブラウザはヒューリスティックに
    キャッシュし、デプロイ後も古い CSS を再検証せず使い続けることがある
    （実際に発生した）。URL に ?v=<ハッシュ> を付けて、中身が変わったときだけ
    別 URL になるようにする。
    """
    try:
        data = (HERE / "static" / "style.css").read_bytes()
    except OSError:
        return "dev"
    return hashlib.sha256(data).hexdigest()[:10]


templates.env.globals["asset_v"] = _asset_version()



# 初期パスワードのまま他のページを触らせない。発行された文字列は管理者も
# 知っているので、本人が変えるまでは操作をさせない。
_PW_EXEMPT = ("/account/password", "/logout", "/login", "/static", "/healthz",
              "/oauth", "/register", "/signup", "/forgot", "/line/webhook",
              "/terms", "/privacy")


@app.middleware("http")
async def force_password_change(request: Request, call_next):
    path = request.url.path
    if not path.startswith(_PW_EXEMPT):
        sess = get_session(request.cookies.get("session"))
        if sess and sess.get("must_change_password"):
            if request.method == "GET":
                return RedirectResponse("/account/password", status_code=303)
            raise HTTPException(status_code=403, detail="先にパスワードを変更してください")
    return await call_next(request)

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


# Discord のサーバーIDは17〜20桁の数字。パス結合の前に必ず通す。
_GUILD_ID_RE = re.compile(r"^\d{17,20}$")


def require_admin_for_guild(sess: dict, guild_id: str) -> dict:
    """このセッションが、そのサーバーを触ってよいかを判定する。

    以前はメール認証というだけで全サーバーを素通しにしていた。複数社が
    使う前提だと、URL のサーバーIDを差し替えるだけで他社の APIキーが
    見える状態だったため、担当サーバーの照合を必須にした。

    判定は毎回ユーザーストアを読む。Cookie に焼き込むと、担当から外した
    あとも 7 日間有効な古いセッションで入れてしまう。
    """
    if not _GUILD_ID_RE.match(str(guild_id)):
        raise HTTPException(status_code=404, detail="unknown guild")

    if sess.get("is_root"):
        return {"id": guild_id, "name": guild_id}

    if sess.get("auth_method") == "email":
        email = sess.get("user_id") or sess.get("username") or ""
        if user_store.can_access_guild(email, guild_id):
            return {"id": guild_id, "name": guild_id}
        log.warning("担当外のサーバーへのアクセスを拒否しました（%s → %s）", email, guild_id)
        raise HTTPException(status_code=403, detail="このサーバーの担当ではありません")

    # Discord 認証: セッションには管理者権限を持つサーバーだけが入っている
    for g in sess.get("guilds", []):
        if str(g["id"]) == str(guild_id):
            return g
    raise HTTPException(status_code=403, detail="not an admin of that guild")


def _discord_user_id(sess: dict) -> int:
    """Discord のユーザーID。メール認証には無いので 0 を返す。

    以前は int(sess["user_id"]) としており、メール認証だと user_id が
    メールアドレスなので ValueError で 500 になっていた。抽選と競りの
    作成が、マルチテナントの主対象であるメールユーザーで使えなかった。
    """
    uid = str(sess.get("user_id") or "")
    return int(uid) if uid.isdigit() else 0


def require_guild_admin(sess: dict, guild_id: str) -> dict:
    """APIキーやBOTの起動停止など、会社の管理者だけに許す操作のガード。

    招待画面で「スタッフ（日々の運用のみ）」と説明している以上、スタッフが
    APIキーを読める状態にしてはいけない。role を実際に照合する。
    """
    g = require_admin_for_guild(sess, guild_id)
    if sess.get("is_root") or sess.get("auth_method") != "email":
        return g
    email = sess.get("user_id") or ""
    if user_store.role_of(email) != "tenant_admin":
        log.warning("スタッフ権限で管理者専用の操作を試みました（%s → %s）", email, guild_id)
        raise HTTPException(
            status_code=403,
            detail="この操作は会社の管理者のみが行えます。管理者にご依頼ください。",
        )
    return g


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


@app.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request):
    return templates.TemplateResponse(
        "terms.html", {"request": request, "legal": legal.info()}
    )


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    return templates.TemplateResponse(
        "privacy.html", {"request": request, "legal": legal.info()}
    )


@app.get("/signup", response_class=HTMLResponse)
async def signup_form(request: Request):
    """導入の申し込みフォーム。ログイン不要で誰でも書ける。"""
    return templates.TemplateResponse("signup.html", {"request": request})


@app.post("/signup")
async def signup_submit(request: Request):
    form = await request.form()
    try:
        signups.submit(
            company=str(form.get("company") or ""),
            contact=str(form.get("contact") or ""),
            email=str(form.get("email") or ""),
            phone=str(form.get("phone") or ""),
            guild_id=str(form.get("guild_id") or ""),
            note=str(form.get("note") or ""),
        )
    except ValueError as e:
        return RedirectResponse(f"/signup?err={urllib.parse.quote(str(e))}", status_code=303)
    return RedirectResponse("/signup?ok=1", status_code=303)


# -------------------------- 申し込みの承認（スーパー管理者） --------------------------


@app.post("/admin/signups/{sid}/approve")
async def signup_approve(
    request: Request, sid: str, session: Optional[str] = Cookie(None)
):
    """申し込みを承認し、会社と担当者アカウントを同時に作る。"""
    sess = require_session(session)
    require_root(sess)

    app_rec = signups.get(sid)
    if not app_rec or app_rec.get("status") != "pending":
        return RedirectResponse("/admin?err=対象の申し込みが見つかりません", status_code=303)

    tn = tenants.add(app_rec["company"], note=app_rec.get("note", "")[:120])
    if app_rec.get("guild_id"):
        tenants.set_guilds(tn["slug"], [app_rec["guild_id"]])

    try:
        _, password = user_store.invite(
            app_rec["email"], tn["slug"], role="tenant_admin",
            added_by=sess.get("username", ""),
        )
    except ValueError as e:
        return RedirectResponse(f"/admin?err={urllib.parse.quote(str(e))}", status_code=303)

    signups.mark(sid, "approved", by=sess.get("username", ""))
    sent = _mail_password(app_rec["email"], password)
    q = urllib.parse.urlencode({
        "ok": f"{app_rec['company']} を開設しました" + ("（初期パスワードをメール送信済み）" if sent else ""),
        "pw": "" if sent else _stash_secret(password),
    })
    return RedirectResponse(f"/admin/tenants/{tn['slug']}?{q}", status_code=303)


@app.post("/admin/signups/{sid}/reject")
async def signup_reject(
    request: Request, sid: str, session: Optional[str] = Cookie(None)
):
    sess = require_session(session)
    require_root(sess)
    signups.mark(sid, "rejected", by=sess.get("username", ""))
    return RedirectResponse("/admin?ok=申し込みを却下しました", status_code=303)


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
    # 担当サーバーだけを出す。以前は全デプロイを列挙していたため、
    # 他社のサーバー名まで一覧に並んでいた。
    if user.get("is_root"):
        allowed = None
    elif user.get("tenant"):
        allowed = tenants.guilds_for_tenant(user["tenant"])
    else:
        allowed = user_store.allowed_guilds(user["email"])
    guilds = []
    bot_token = os.environ.get("DISCORD_TOKEN_DEFAULT")
    for gid in config_store.list_deployments():
        if allowed is not None and str(gid) not in allowed:
            continue
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
        "tenant": user.get("tenant"),
        "must_change_password": user.get("must_change_password", False),
        "guilds": guilds,
    }
    # 303 でないと 307 が POST を引き継ぎ、GET 専用の /dashboard が 405 を返す。
    resp = RedirectResponse("/dashboard", status_code=303)
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
            # パスワードは URL に載せない（アクセスログと履歴に残るため）
            return RedirectResponse(
                f"/admin/users?approved={email}&pw={_stash_secret(password)}&mail_err={msg}",
                status_code=303,
            )
        return RedirectResponse(f"/admin/users?approved={email}&mailed=1", status_code=303)
    # SMTP not configured — surface the password so admin can hand it over manually
    return RedirectResponse(
        f"/admin/users?approved={email}&pw={_stash_secret(password)}&mail_err=smtp_not_configured",
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


# -------------------------- 会員ページ（0枚目・スーパー管理者のみ） --------------------------


@app.get("/admin", response_class=HTMLResponse)
async def admin_home(request: Request, session: Optional[str] = Cookie(None)):
    """会社の一覧。Musubot を提供している相手をここで管理する。"""
    sess = require_session(session)
    require_root(sess)

    rows = []
    for tn in tenants.list_tenants():
        us = user_store.users_of_tenant(tn["slug"])
        rows.append({
            **tn,
            "user_count": len(us),
            "running": sum(1 for g in tn["guilds"] if bot_manager.is_running(g)),
        })
    return templates.TemplateResponse(
        "admin_tenants.html",
        {"request": request, "session": sess, "tenants": rows,
         "unassigned": tenants.unassigned_guilds(),
         "signups": signups.list_all("pending"),
         "handled": signups.list_all()[:0] if False else
                    [s for s in signups.list_all() if s["status"] != "pending"][:10],
         "page": "admin"},
    )


@app.post("/admin/tenants/add")
async def admin_tenant_add(
    request: Request, session: Optional[str] = Cookie(None)
):
    sess = require_session(session)
    require_root(sess)
    form = await request.form()
    name = str(form.get("name") or "").strip()
    if not name:
        return RedirectResponse("/admin?err=会社名を入力してください", status_code=303)
    tn = tenants.add(name, str(form.get("note") or ""))
    return RedirectResponse(f"/admin/tenants/{tn['slug']}?ok=会社を追加しました", status_code=303)


@app.get("/admin/tenants/{slug}", response_class=HTMLResponse)
async def admin_tenant_detail(
    request: Request, slug: str, session: Optional[str] = Cookie(None)
):
    sess = require_session(session)
    require_root(sess)
    tn = tenants.get(slug)
    if not tn:
        raise HTTPException(status_code=404, detail="会社が見つかりません")

    guilds = []
    for gid in tn["guilds"]:
        guilds.append({
            "id": gid,
            "running": bot_manager.is_running(gid),
            "configured": (config_store.deployment_dir(gid) / ".env").exists(),
        })
    return templates.TemplateResponse(
        "admin_tenant.html",
        {"request": request, "session": sess, "tenant": tn, "guilds": guilds,
         "users": user_store.users_of_tenant(slug),
         "unassigned": tenants.unassigned_guilds(), "page": "admin"},
    )


@app.post("/admin/tenants/{slug}/update")
async def admin_tenant_update(
    request: Request, slug: str, session: Optional[str] = Cookie(None)
):
    sess = require_session(session)
    require_root(sess)
    form = await request.form()
    tenants.update(slug, name=str(form.get("name") or ""), note=str(form.get("note") or ""))
    return RedirectResponse(f"/admin/tenants/{slug}?ok=保存しました", status_code=303)


@app.post("/admin/tenants/{slug}/status")
async def admin_tenant_status(
    request: Request, slug: str, session: Optional[str] = Cookie(None)
):
    """利用停止・再開。停止すると、その会社の人は全サーバーに入れなくなる。"""
    sess = require_session(session)
    require_root(sess)
    form = await request.form()
    status = str(form.get("status") or "active")
    tenants.set_status(slug, status)
    if status == "suspended" and str(form.get("stop_bots") or "") == "1":
        for gid in tenants.guilds_for_tenant(slug):
            try:
                bot_manager.stop(gid)
            except Exception:  # noqa: BLE001 — 停止できなくても台帳は更新済み
                log.warning("利用停止に伴う BOT 停止に失敗しました（%s）", gid)
    msg = "利用を停止しました" if status == "suspended" else "利用を再開しました"
    return RedirectResponse(f"/admin/tenants/{slug}?ok={msg}", status_code=303)


@app.post("/admin/tenants/{slug}/guilds")
async def admin_tenant_guilds(
    request: Request, slug: str, session: Optional[str] = Cookie(None)
):
    sess = require_session(session)
    require_root(sess)
    form = await request.form()
    ids = [str(x).strip() for x in form.getlist("guild_id") if str(x).strip()]
    extra = str(form.get("guild_id_manual") or "").strip()
    if extra:
        ids.append(extra)
    bad = [g for g in ids if not _GUILD_ID_RE.match(g)]
    if bad:
        return RedirectResponse(
            f"/admin/tenants/{slug}?err=サーバーIDの形式が不正です（{bad[0]}）", status_code=303
        )
    ok, moved = tenants.set_guilds(slug, ids)
    if not ok:
        return RedirectResponse(f"/admin/tenants/{slug}?err=会社が見つかりません", status_code=303)
    msg = "担当サーバーを更新しました"
    if moved:
        names = "、".join(sorted({name for _, name in moved}))
        msg += f"（{len(moved)}件を {names} から移しました）"
    return RedirectResponse(
        f"/admin/tenants/{slug}?{urllib.parse.urlencode({'ok': msg})}", status_code=303
    )


@app.post("/admin/tenants/{slug}/users/invite")
async def admin_tenant_invite(
    request: Request, slug: str, session: Optional[str] = Cookie(None)
):
    """担当者を追加し、初期パスワードを発行する。"""
    sess = require_session(session)
    require_root(sess)
    form = await request.form()
    email = str(form.get("email") or "")
    role = str(form.get("role") or "staff")
    try:
        _, password = user_store.invite(email, slug, role, added_by=sess.get("username", ""))
    except ValueError as e:
        return RedirectResponse(f"/admin/tenants/{slug}?err={e}", status_code=303)

    # パスワードは URL に載せない（アクセスログとブラウザ履歴に残るため）。
    # 一度だけ取り出せる置き場に入れ、画面表示後に消す。
    token = _stash_secret(password)
    sent = _mail_password(email, password)
    q = urllib.parse.urlencode({
        "ok": f"{email} を追加しました" + ("（メール送信済み）" if sent else ""),
        "pw": "" if sent else token,
    })
    return RedirectResponse(f"/admin/tenants/{slug}?{q}", status_code=303)


@app.post("/admin/tenants/{slug}/users/reset")
async def admin_tenant_reset(
    request: Request, slug: str, session: Optional[str] = Cookie(None)
):
    sess = require_session(session)
    require_root(sess)
    form = await request.form()
    email = str(form.get("email") or "")
    password = user_store.reset_password(email)
    if not password:
        return RedirectResponse(f"/admin/tenants/{slug}?err=ユーザーが見つかりません", status_code=303)
    sent = _mail_password(email, password)
    token = _stash_secret(password)
    q = urllib.parse.urlencode({
        "ok": f"{email} のパスワードを再発行しました" + ("（メール送信済み）" if sent else ""),
        "pw": "" if sent else token,
    })
    return RedirectResponse(f"/admin/tenants/{slug}?{q}", status_code=303)


@app.post("/admin/tenants/{slug}/users/toggle")
async def admin_tenant_toggle(
    request: Request, slug: str, session: Optional[str] = Cookie(None)
):
    sess = require_session(session)
    require_root(sess)
    form = await request.form()
    user_store.set_allowed(str(form.get("email") or ""), str(form.get("allowed") or "0") == "1")
    return RedirectResponse(f"/admin/tenants/{slug}?ok=アクセス権を更新しました", status_code=303)


@app.post("/admin/tenants/{slug}/users/role")
async def admin_tenant_role(
    request: Request, slug: str, session: Optional[str] = Cookie(None)
):
    sess = require_session(session)
    require_root(sess)
    form = await request.form()
    user_store.set_role(str(form.get("email") or ""), str(form.get("role") or "staff"))
    return RedirectResponse(f"/admin/tenants/{slug}?ok=権限を変更しました", status_code=303)


def _mail_password(email: str, password: str) -> bool:
    """初期パスワードをメールで送る。送れたかどうかを返す。

    送れなかった場合は画面に一度だけ出して手渡ししてもらう。
    """
    if not mailer.smtp_configured():
        return False
    base = os.environ.get("DASHBOARD_BASE_URL", "").rstrip("/")
    login_url = f"{base}/login" if base else "/login"
    try:
        subject, body = mailer.render_approval_email(email, password, login_url)
        ok, msg = mailer.send(email, subject, body)
        if not ok:
            log.warning("初期パスワードのメール送信に失敗しました（%s）: %s", email, msg)
        return bool(ok)
    except Exception:  # noqa: BLE001 — メール不通でも招待自体は成立している
        log.warning("初期パスワードのメール送信で例外が出ました（%s）", email, exc_info=True)
        return False


# -------------------------- パスワードの初回変更 --------------------------

# 初期パスワードを1回だけ画面に出すための一時置き場。URL に平文を載せない
# ためのもので、プロセス内に持つ（再起動で消えて構わない性質のもの）。
_SECRET_STASH: dict[str, str] = {}


def _stash_secret(value: str) -> str:
    token = secrets.token_urlsafe(12)
    _SECRET_STASH[token] = value
    if len(_SECRET_STASH) > 50:      # 取り出されなかった分を捨てる
        for k in list(_SECRET_STASH)[:-50]:
            _SECRET_STASH.pop(k, None)
    return token


def pop_secret(token: str) -> str:
    return _SECRET_STASH.pop(token or "", "")


templates.env.globals["pop_secret"] = pop_secret


@app.get("/account/password", response_class=HTMLResponse)
async def account_password_form(request: Request, session: Optional[str] = Cookie(None)):
    sess = require_session(session)
    return templates.TemplateResponse(
        "account_password.html",
        {"request": request, "session": sess,
         "forced": bool(sess.get("must_change_password"))},
    )


@app.post("/account/password")
async def account_password_save(
    request: Request, response: Response, session: Optional[str] = Cookie(None)
):
    sess = require_session(session)
    form = await request.form()
    new = str(form.get("password") or "")
    if new != str(form.get("password2") or ""):
        return RedirectResponse("/account/password?err=確認用と一致しません", status_code=303)
    try:
        ok = user_store.change_password(sess.get("user_id") or "", new)
    except ValueError as e:
        return RedirectResponse(f"/account/password?err={e}", status_code=303)
    if not ok:
        return RedirectResponse("/account/password?err=変更できませんでした", status_code=303)

    sess["must_change_password"] = False
    resp = RedirectResponse("/dashboard?pw=changed", status_code=303)
    set_session(resp, sess)
    return resp


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, session: Optional[str] = Cookie(None)):
    sess = get_session(session)
    if not sess:
        return RedirectResponse("/login")
    # セッションのギルド一覧は最大7日間そのまま残る。担当から外れた
    # サーバーが一覧に出ないよう、表示のたびに権限で絞り直す。
    admin_guilds = [
        g for g in sess.get("guilds", [])
        if sess.get("is_root")
        or sess.get("auth_method") != "email"
        or user_store.can_access_guild(sess.get("user_id") or "", str(g["id"]))
    ]
    for g in admin_guilds:
        gid = str(g["id"])
        g["bot_running"] = bot_manager.is_running(gid)
        g["configured"] = (config_store.deployment_dir(gid) / ".env").exists()

    await _refresh_member_counts(admin_guilds)
    for g in admin_guilds:
        g["stats"] = member_stats.summary(str(g["id"]), days=30)

    total_members = sum(
        (g["stats"] or {}).get("latest") or 0 for g in admin_guilds
    )
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "session": sess,
            "guilds": admin_guilds,
            "total_members": total_members,
        },
    )


async def _refresh_member_counts(guilds: list[dict]) -> None:
    """当日ぶん未記録のサーバーだけ Discord に人数を問い合わせて記録する。

    ダッシュボード表示をブロックしないよう、失敗・タイムアウトは黙って無視し、
    既に保存済みの推移データだけで描画する。
    """
    import asyncio

    async def one(g: dict) -> None:
        gid = str(g["id"])
        if not g.get("configured") or not member_stats.should_refresh(gid):
            return
        env = config_store.read_env(gid)
        token = (env.get("DISCORD_TOKEN_SUPPORT") or "").strip()
        if not token:
            return
        try:
            counts = await DiscordREST(token).get_guild_counts(gid)
        except Exception:  # noqa: BLE001 — 人数取得の失敗で画面を落とさない
            return
        if counts:
            member_stats.record(gid, counts.get("total"), counts.get("online"))

    targets = [g for g in guilds if g.get("configured")]
    if not targets:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*(one(g) for g in targets), return_exceptions=True),
            timeout=9.0,
        )
    except asyncio.TimeoutError:
        pass


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


# -------------------------- ロボット（機能）の定義 --------------------------
#
# 「サーバーを選ぶ → ロボットを選ぶ → そのロボットだけを設定する」という導線の
# 単一の情報源。ハブ画面・各設定ページ・状態バッジがすべてここを参照する。
#
#   key      : URL とテンプレート名に使う識別子
#   name/desc: 画面に出す名前と一行説明
#   icon     : _icons.html のマクロ名
#   url      : リンク先（{gid} を guild_id に置換）
#   active_if: このキーのいずれかが非空なら「派遣中」
#   needs    : 動作に必須の追加キー。active なのにこれが空なら「設定不足」
#   summary  : ハブのカードに出す要約 [(ラベル, envキー, 種別)]
#              種別は "channel" / "category" / "role" / "text"
ROBOTS = [
    dict(
        key="translation", name="翻訳ロボ", desc="英語と日本語を自動で通訳", icon="globe",
        url="/guild/{gid}/robot/translation",
        active_if=["TRANSLATE_CHANNEL_IDS", "TICKET_CATEGORY_IDS"],
        needs=[], summary=[("担当", "TRANSLATE_CHANNEL_IDS", "channel")],
        shared=["TRANSLATE_PROVIDER", "TRANSLATE_DEFAULT_MODE"],
        local=["TRANSLATE_CHANNEL_IDS", "TICKET_CATEGORY_IDS"],
    ),
    dict(
        key="ticket", name="受付ロボ", desc="お客様専用の問い合わせ部屋を用意", icon="ticket",
        url="/guild/{gid}/robot/ticket",
        active_if=["TICKET_CATEGORY_ID"],
        needs=["TICKET_STAFF_ROLE_IDS"],
        summary=[("カテゴリ", "TICKET_CATEGORY_ID", "category"),
                 ("スタッフ", "TICKET_STAFF_ROLE_IDS", "role")],
        shared=["FORCE_UI_LANG_TICKET"],
        local=["TICKET_CATEGORY_ID", "TICKET_STAFF_ROLE_IDS", "CARD_GAME_CHANNEL_ID"],
    ),
    dict(
        key="welcome", name="お迎えロボ", desc="新しく入った人を歓迎して案内", icon="sparkles",
        url="/guild/{gid}/robot/welcome",
        active_if=["WELCOME_CHANNEL_ID"], needs=[],
        summary=[("投稿先", "WELCOME_CHANNEL_ID", "channel")],
        shared=["WELCOME_TITLE", "WELCOME_DESCRIPTION", "WELCOME_COLOR",
                "WELCOME_BANNER_URL", "WELCOME_THUMBNAIL_URL",
                "WELCOME_DM_ENABLED", "WELCOME_DM_MESSAGE", "WELCOME_DM_INVITE_URL"],
        local=["WELCOME_CHANNEL_ID", "WELCOME_AUTOROLE_IDS",
               "WELCOME_RULES_CHANNEL_ID", "WELCOME_INTRO_CHANNEL_ID"],
    ),
    dict(
        key="suggester", name="気配りロボ", desc="会話を読んで最適な窓口へ案内", icon="sparkles",
        url="/guild/{gid}/robot/suggester",
        active_if=["COMMUNITY_CHANNEL_ID"], needs=["ANTHROPIC_API_KEY"],
        summary=[("監視", "COMMUNITY_CHANNEL_ID", "channel")],
        shared=["PRODUCT_ADVICE_TEXT", "PRODUCT_ADVICE_TEXT_EN",
                "SHIPPING_ADVICE_TEXT", "SHIPPING_ADVICE_TEXT_EN",
                "SUGGEST_MIN_CONFIDENCE"],
        local=["COMMUNITY_CHANNEL_ID", "PRODUCT_INQUIRY_CHANNEL_ID",
               "SHIPPING_GUIDE_CHANNEL_ID"],
    ),
    dict(
        key="shipping", name="送料ロボ", desc="宛先と重さから送料を即計算", icon="box",
        url="/guild/{gid}/robot/shipping",
        active_if=["SHIPPING_SHEET_ID"], needs=[],
        summary=[("許可", "ALLOW_CH_SHIPPING", "channel")],
        shared=["SHIPPING_SHEET_ID", "SHIPPING_SHEET_NAME", "PACKAGING_WEIGHT_G",
                "SHIPPING_DHL_FUEL_SURCHARGE_PCT",
                "SHIPPING_FEDEX_FUEL_SURCHARGE_PCT",
                "SHIPPING_EXTRA_SURCHARGE_PCT"],
        local=["ALLOW_CH_SHIPPING", "ALLOW_CAT_SHIPPING"],
    ),
    dict(
        key="auction", name="競りロボ", desc="ボタン入札のオークションを進行", icon="gavel",
        url="/guild/{gid}/robot/auction",
        active_if=["ALLOW_CH_AUCTION", "ALLOW_CAT_AUCTION", "AUCTION_MANAGER_ROLE_IDS"],
        needs=[], summary=[("許可", "ALLOW_CH_AUCTION", "channel")],
        shared=["AUCTION_DEFAULT_RESERVE_PRICE", "AUCTION_DEFAULT_MIN_INCREMENT",
                "AUCTION_DEFAULT_ANTI_SNIPE_WINDOW",
                "AUCTION_DEFAULT_ANTI_SNIPE_EXTEND"],
        local=["ALLOW_CH_AUCTION", "ALLOW_CAT_AUCTION",
               "AUCTION_MANAGER_ROLE_IDS", "AUCTION_TICKET_CATEGORY_ID"],
    ),
    dict(
        key="invite", name="案内ロボ", desc="どの招待から来たかを記録", icon="door",
        url="/guild/{gid}/robot/invite",
        active_if=["INVITE_LOG_CHANNEL_ID", "ALLOW_CH_INVITE"], needs=[],
        summary=[("ログ", "INVITE_LOG_CHANNEL_ID", "channel")],
        shared=[],
        local=["INVITE_LOG_CHANNEL_ID", "ALLOW_CH_INVITE",
               "INVITE_CREATOR_ROLE_IDS", "MODERATOR_CHANNEL_ID"],
    ),
    dict(
        key="forwarding", name="運び屋ロボ", desc="画像を別のチャンネルへ届ける", icon="forward",
        url="/guild/{gid}/robot/forwarding",
        active_if=[], needs=[], summary=[],   # 件数で判定するため env は見ない
        shared=[], local=[],
    ),
    dict(
        key="line", name="LINEロボ", desc="LINEグループの画像をDiscordへ運ぶ", icon="send",
        url="/guild/{gid}/robot/line",
        active_if=["LINE_FORWARD_CHANNEL_ID"],
        needs=["LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN"],
        summary=[("転送先", "LINE_FORWARD_CHANNEL_ID", "channel")],
        shared=["LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN",
                "LINE_ALLOWED_SOURCE_IDS", "LINE_FORWARD_TEXT"],
        local=["LINE_FORWARD_CHANNEL_ID"],
    ),
    dict(
        key="schedule", name="お知らせロボ", desc="決まった時刻にメッセージを投稿", icon="mail",
        url="/guild/{gid}/schedule",
        active_if=[], needs=[], summary=[],
        shared=[], local=[],
    ),
    dict(
        key="giveaway", name="抽選ロボ", desc="参加ボタン付きの抽選を開催", icon="gift",
        url="/guild/{gid}/giveaway",
        active_if=[], needs=[], summary=[],
        shared=[], local=[],
    ),
    dict(
        key="onboarding", name="道しるべロボ", desc="入室時のアンケートとロール付与", icon="layers",
        url="/guild/{gid}/onboarding",
        active_if=[], needs=[], summary=[],
        shared=[], local=[],
    ),
]
ROBOTS_BY_KEY = {r["key"]: r for r in ROBOTS}


def _name_index(channels_groups: list) -> tuple[dict, dict]:
    """チャンネルIDとカテゴリIDから表示名を引くための索引。"""
    ch, cat = {}, {}
    for c, chs in channels_groups:
        if c:
            cat[str(c["id"])] = c.get("name", "")
        for x in chs:
            ch[str(x["id"])] = x.get("name", "")
    return ch, cat


def _summarize(robot: dict, env: dict, ch_names: dict, cat_names: dict, roles: list) -> str:
    """カードに出す「担当: #general ほか2ch」のような一行。"""
    role_names = {str(r["id"]): r.get("name", "") for r in roles}
    parts = []
    for label, key, kind in robot.get("summary", []):
        raw = [v for v in (env.get(key) or "").split(",") if v.strip()]
        if not raw:
            continue
        table = {"channel": ch_names, "category": cat_names, "role": role_names}.get(kind, {})
        first = table.get(raw[0].strip())
        if not first:
            # トークン未設定などで名前を引けないときは、件数だけ伝える
            parts.append(f"{label}: {len(raw)}件")
            continue
        prefix = "#" if kind == "channel" else ""
        more = f" ほか{len(raw) - 1}件" if len(raw) > 1 else ""
        parts.append(f"{label}: {prefix}{first}{more}")
    return " ・ ".join(parts)


def robot_state(
    robot: dict, env: dict, *, forward_rule_count: int = 0, schedule_count: int = 0
) -> tuple[str, str]:
    """(状態キー, 表示ラベル) を返す。

    状態は active（派遣中）/ incomplete（設定不足）/ unconfigured（未設定）。
    転送とスケジュールは env に現れないため、件数で判定する。
    """
    key = robot["key"]
    if key == "forwarding":
        return ("active", "派遣中") if forward_rule_count else ("unconfigured", "未設定")
    if key == "schedule":
        return ("active", "派遣中") if schedule_count else ("unconfigured", "未設定")
    if key in ("giveaway", "onboarding"):
        return ("neutral", "")          # 常時使える。状態という概念がない
    if not any((env.get(k) or "").strip() for k in robot["active_if"]):
        return ("unconfigured", "未設定")
    missing = [k for k in robot["needs"] if not (env.get(k) or "").strip()]
    return ("incomplete", "設定不足") if missing else ("active", "派遣中")


async def _guild_page_ctx(request: Request, sess: dict, guild_id: str) -> dict:
    """ロボット設定ページ共通のテンプレート変数。

    Discord からチャンネルとロールだけを取る軽量版。転送先サーバー一覧や
    BOT プロフィールのような重い取得は、必要なページだけで個別に行う。
    """
    config_store.ensure_deployment(guild_id)
    env = config_store.read_env(guild_id)
    token = _bot_token_for(guild_id)

    grouped: list = []
    roles: list = []
    discord_ok = False
    if token:
        rest = DiscordREST(token)
        if await rest.get_guild(guild_id):
            discord_ok = True
            grouped = channels_grouped(await rest.list_channels(guild_id))
            roles = assignable_roles(await rest.list_roles(guild_id))

    return {
        "request": request,
        "session": sess,
        "guild_id": guild_id,
        "guild_name": _guild_name_from_session(sess, guild_id),
        "env": env,
        "channels_grouped": grouped,
        "roles": roles,
        "discord_ok": discord_ok,
        "bot_running": bot_manager.is_running(guild_id),
    }


@app.get("/guild/{guild_id}", response_class=HTMLResponse)
async def guild_home(
    request: Request,
    guild_id: str,
    session: Optional[str] = Cookie(None),
):
    """サーバーのホーム。派遣するロボットをここから選ぶ。"""
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)

    ctx = await _guild_page_ctx(request, sess, guild_id)
    env = ctx["env"]
    ch_names, cat_names = _name_index(ctx["channels_grouped"])

    # env に現れない機能は件数で状態を出す
    try:
        fwd_count = len(forward_store.load_rules())
    except Exception:  # noqa: BLE001 — 件数が取れなくてもハブは表示する
        log.warning("guild_home: 転送ルールを読めませんでした", exc_info=True)
        fwd_count = 0
    schedules = schedule_store.load(guild_id)
    sched_count = sum(1 for s in schedules if s.get("enabled", True))

    cards = []
    for r in ROBOTS:
        state, label = robot_state(
            r, env, forward_rule_count=fwd_count, schedule_count=sched_count
        )
        summary = _summarize(r, env, ch_names, cat_names, ctx["roles"])
        if r["key"] == "forwarding" and fwd_count:
            summary = f"転送ルール {fwd_count} 件"
        elif r["key"] == "schedule" and sched_count:
            summary = f"有効なお知らせ {sched_count} 件"
        cards.append({
            **r,
            "href": r["url"].format(gid=guild_id),
            "state": state,
            "state_label": label,
            "summary": summary,
        })

    ctx.update({"cards": cards, "subpage": "home"})
    return templates.TemplateResponse("guild_home.html", ctx)


@app.get("/guild/{guild_id}/settings", response_class=HTMLResponse)
async def guild_settings(
    request: Request,
    guild_id: str,
    session: Optional[str] = Cookie(None),
):
    """APIキー・BOTの見た目・サーバー構成テンプレート。ロボット共通の土台。"""
    sess = require_session(session)
    require_guild_admin(sess, guild_id)

    ctx = await _guild_page_ctx(request, sess, guild_id)
    token = _bot_token_for(guild_id)

    bot_profile: dict = {}
    if token:
        me = await DiscordREST(token).get_me()
        if me:
            bot_profile = {
                "username": me.get("username") or "",
                "id": me.get("id") or "",
                "avatar_url": (
                    f"https://cdn.discordapp.com/avatars/{me['id']}/{me['avatar']}.png?size=128"
                    if me.get("avatar") else ""
                ),
            }

    ctx.update({
        "bot_profile": bot_profile,
        "avatar_presets": AVATAR_PRESETS,
        "subpage": "settings",
    })
    return templates.TemplateResponse("guild_settings.html", ctx)


@app.get("/guild/{guild_id}/robot/{robot_key}", response_class=HTMLResponse)
async def robot_page(
    request: Request,
    guild_id: str,
    robot_key: str,
    session: Optional[str] = Cookie(None),
):
    """ロボット1体ぶんの設定画面。"""
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)

    robot = ROBOTS_BY_KEY.get(robot_key)
    # 独自ページを持つ機能（お知らせ・抽選・道しるべ）はそちらへ送る
    if robot is None or not robot["url"].endswith(f"/robot/{robot_key}"):
        target = robot["url"].format(gid=guild_id) if robot else f"/guild/{guild_id}"
        return RedirectResponse(target, status_code=303)

    ctx = await _guild_page_ctx(request, sess, guild_id)
    ctx["robot"] = robot
    ctx["subpage"] = "robot"

    # 「他のサーバーにも適用」用。自分が管理者の、このサーバー以外を出す。
    ctx["other_guilds"] = [
        {"id": str(g["id"]), "name": g.get("name") or str(g["id"])}
        for g in sess.get("guilds", [])
        if str(g["id"]) != str(guild_id)
    ]
    ctx["shared_values"] = [
        (k, ctx["env"].get(k, "")) for k in robot.get("shared", []) if ctx["env"].get(k, "")
    ]

    if robot_key == "shipping":
        fuel_cache: dict = {}
        fuel_path = config_store.deployment_dir(guild_id) / "data" / "fuel_surcharge.json"
        if fuel_path.exists():
            try:
                fuel_cache = json.loads(fuel_path.read_text("utf-8"))
            except (OSError, ValueError):
                log.warning("燃油サーチャージのキャッシュを読めませんでした")
        ctx["fuel_cache"] = fuel_cache

    if robot_key == "forwarding":
        ctx.update(await _forwarding_ctx(guild_id))

    if robot_key == "shipping":
        cfg = shipping_master.load_config(guild_id)
        ctx["products"] = products_store.load(guild_id)
        ctx["products_own"] = products_store.has_own(guild_id)
        ctx["products_max"] = products_store.MAX_PRODUCTS
        ctx["ship_cfg"] = {
            "own": shipping_master.has_own_config(guild_id),
            "blocks": len(cfg.get("blocks", {}).get("_aliases", {})),
            "countries": len(cfg.get("countries", [])),
        }

    if robot_key == "line":
        # LINE 側に貼ってもらう webhook URL。公開URLが分からない環境では
        # リクエストのホストから組み立てる。
        base = (os.environ.get("DASHBOARD_BASE_URL") or "").rstrip("/")
        if not base:
            base = str(request.base_url).rstrip("/")
        ctx["webhook_url"] = f"{base}/line/webhook/{guild_id}"

    return templates.TemplateResponse(f"robot_{robot_key}.html", ctx)


@app.post("/guild/{guild_id}/robot/{robot_key}/copy")
async def robot_copy(
    request: Request,
    guild_id: str,
    robot_key: str,
    session: Optional[str] = Cookie(None),
):
    """このロボットの設定を、別のサーバーにも適用する。

    コピーするのは shared に挙げたキーだけ。チャンネルIDやロールIDは
    サーバーごとに違う値なので、持っていっても存在しない ID を指すことに
    なる。コピー先では改めて選び直してもらう。
    """
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)

    robot = ROBOTS_BY_KEY.get(robot_key)
    if not robot or not robot.get("shared"):
        raise HTTPException(status_code=400, detail="このロボットに共通設定はありません")

    form = await request.form()
    targets = [str(x) for x in form.getlist("target") if str(x) != str(guild_id)]
    if not targets:
        return RedirectResponse(
            f"/guild/{guild_id}/robot/{robot_key}?copy_err=コピー先のサーバーを選んでください",
            status_code=303,
        )

    src = config_store.read_env(guild_id)
    payload = {k: src.get(k, "") for k in robot["shared"] if src.get(k, "")}
    if not payload:
        return RedirectResponse(
            f"/guild/{guild_id}/robot/{robot_key}?copy_err=コピーする内容がまだありません",
            status_code=303,
        )

    done = []
    for gid in targets:
        # コピー先も自分が管理者であることを確認する（他人のサーバーに書かない）
        try:
            require_admin_for_guild(sess, gid)
        except HTTPException:
            log.warning("コピー先 %s の権限がないため飛ばしました", gid)
            continue
        config_store.ensure_deployment(gid)
        config_store.write_env(gid, payload)
        if bot_manager.is_running(gid):
            bot_manager.restart(gid)
        done.append(gid)
        log.info("%s の %s 設定を %s にコピーしました", guild_id, robot_key, gid)

    return RedirectResponse(
        f"/guild/{guild_id}/robot/{robot_key}?copied={len(done)}&keys={len(payload)}",
        status_code=303,
    )


async def _forwarding_ctx(guild_id: str) -> dict:
    """転送ページ専用の重い取得（転送Botが参加する全サーバーのチャンネル一覧）。

    以前は setup ページの表示ごとに走っていて、転送を使わない人にも
    その待ち時間を払わせていた。転送ページ限定にする。
    """
    fwd_servers: list = []
    forward_rules: list = []
    labels = {
        "original": "そのまま（原文＋画像）",
        "image_only": "画像のみ",
        "decorated": "加工（原文＋付加文）",
        "custom": "任意テキストに置換",
    }
    try:
        token = forward_store.forward_bot_token()
        if token:
            fwd_servers = await forward_store.list_servers_with_channels(token)
        ch_index = forward_store.index_channels(fwd_servers)
        role_index = forward_store.index_roles(fwd_servers)
        for r in forward_store.load_rules():
            src, dst = str(r.get("source")), str(r.get("dest"))
            s, d = ch_index.get(src), ch_index.get(dst)
            mode = r.get("mode", "original")
            role_ids = [str(x) for x in (r.get("role_ids") or [])]
            forward_rules.append({
                "source": src,
                "dest": dst,
                "source_label": f"{s['server']} ＞ #{s['channel']}" if s else f"(ID: {src})",
                "dest_label": f"{d['server']} ＞ #{d['channel']}" if d else f"(ID: {dst})",
                "source_server_id": s["server_id"] if s else "",
                "mode": mode,
                "mode_label": labels.get(mode, mode),
                "role_ids": role_ids,
                "role_labels": [role_index.get(rid, rid) for rid in role_ids],
                "template": r.get("template") or "",
            })
    except Exception:  # noqa: BLE001 — 転送一覧が取れなくてもページは開く
        log.warning("転送先サーバー/ルールの取得に失敗しました", exc_info=True)
    return {
        "fwd_servers": fwd_servers,
        "forward_rules": forward_rules,
        "fwd_mode_labels": labels,
    }


@app.get("/guild/{guild_id}/setup", response_class=HTMLResponse)
async def guild_setup_redirect(
    guild_id: str,
    session: Optional[str] = Cookie(None),
):
    """旧・全部入り設定ページ。ブックマーク救済のためハブへ転送する。

    どのタブを見ていたかは #tab-xxx というフラグメントで、サーバーには
    届かない。転送先のハブ側に置いた JS が hash を見て各ロボットへ送る。
    """
    require_session(session)
    return RedirectResponse(f"/guild/{guild_id}", status_code=301)


@app.get("/guild/{guild_id}/setup/legacy", response_class=HTMLResponse)
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
    bot_profile: dict = {}
    if bot_token:
        rest = DiscordREST(bot_token)
        guild_info = await rest.get_guild(guild_id)
        if guild_info:
            discord_ok = True
            channels = await rest.list_channels(guild_id)
            grouped = channels_grouped(channels)
            all_roles = await rest.list_roles(guild_id)
            roles = assignable_roles(all_roles)
        me = await rest.get_me()
        if me:
            bot_profile = {
                "username": me.get("username") or "",
                "id": me.get("id") or "",
                "avatar_url": (
                    f"https://cdn.discordapp.com/avatars/{me['id']}/{me['avatar']}.png?size=128"
                    if me.get("avatar") else ""
                ),
            }

    # Message forwarding: list every server the forwarding bot can reach so the
    # source AND destination can be picked from other servers' channels (not
    # just this guild). Mirrors the global /forwarding page, but embedded here.
    fwd_servers: list = []
    forward_rules = []
    fwd_mode_labels = {
        "original": "そのまま（原文＋画像）",
        "image_only": "画像のみ",
        "decorated": "加工（原文＋付加文）",
        "custom": "任意テキストに置換",
    }
    try:
        fwd_token = forward_store.forward_bot_token()
        if fwd_token:
            fwd_servers = await forward_store.list_servers_with_channels(fwd_token)
        ch_index = forward_store.index_channels(fwd_servers)
        role_index = forward_store.index_roles(fwd_servers)
        for r in forward_store.load_rules():
            src = str(r.get("source"))
            dst = str(r.get("dest"))
            s = ch_index.get(src)
            d = ch_index.get(dst)
            mode = r.get("mode", "original")
            role_ids = [str(x) for x in (r.get("role_ids") or [])]
            forward_rules.append({
                "source": src,
                "dest": dst,
                "source_label": f"{s['server']} ＞ #{s['channel']}" if s else f"(ID: {src})",
                "dest_label": f"{d['server']} ＞ #{d['channel']}" if d else f"(ID: {dst})",
                "source_server_id": s["server_id"] if s else "",
                "mode": mode,
                "mode_label": fwd_mode_labels.get(mode, mode),
                "role_ids": role_ids,
                "role_labels": [role_index.get(rid, rid) for rid in role_ids],
                "template": r.get("template") or "",
            })
    except Exception:
        log.warning("setup: failed to load forwarding servers/rules", exc_info=True)

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
            "bot_profile": bot_profile,
            "avatar_presets": AVATAR_PRESETS,
            "channels_grouped": grouped,
            "roles": roles,
            "discord_ok": discord_ok,
            "bot_running": bot_manager.is_running(guild_id),
            "fuel_cache": fuel_cache,
            "forward_rules": forward_rules,
            "fwd_servers": fwd_servers,
        },
    )


async def _assert_channels_in_scope(sess: dict, guild_id: str, *channel_ids: int) -> None:
    """転送ルールの送信元・転送先が、その会社の担当サーバーのものか確かめる。

    転送Botは全社共通で、参加している全サーバーのチャンネルを列挙できる。
    チャンネルIDを検証しないと、他社サーバーの非公開チャンネルを送信元に
    指定して会話や画像を自社へ吸い出せてしまう。ここが最後の砦になる。
    """
    if sess.get("is_root"):
        return

    # このセッションが触れるサーバーの集合
    if sess.get("auth_method") == "email":
        slug = user_store.tenant_of(sess.get("user_id") or "")
        allowed_guilds = set(tenants.guilds_for_tenant(slug)) if slug else {str(guild_id)}
    else:
        allowed_guilds = {str(g["id"]) for g in sess.get("guilds", [])}

    token = forward_store.forward_bot_token()
    if not token:
        raise HTTPException(status_code=400, detail="転送Botのトークンが未設定です")
    servers = await forward_store.list_servers_with_channels(token)
    ok: set[str] = set()
    for s in servers:
        if str(s.get("id")) in allowed_guilds:
            ok.update(str(c["id"]) for c in s.get("channels", []))

    for cid in channel_ids:
        if str(cid) not in ok:
            log.warning(
                "担当外のチャンネルを転送に指定しました（%s → ch=%s）",
                sess.get("user_id"), cid,
            )
            raise HTTPException(
                status_code=403,
                detail="担当していないサーバーのチャンネルは指定できません",
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
    await _assert_channels_in_scope(sess, guild_id, source, dest)
    mode = (form.get("mode") or "original").strip()
    role_ids = [s.strip() for s in (form.get("role_ids") or "").split(",") if s.strip()]
    template = (form.get("template") or "").strip()
    added = forward_store.add_rule(source, dest, mode=mode, role_ids=role_ids, template=template)
    return JSONResponse({"ok": True, "added": bool(added)})


@app.post("/guild/{guild_id}/forwarding/update")
async def guild_forwarding_update(
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
    await _assert_channels_in_scope(sess, guild_id, source, dest)
    mode = (form.get("mode") or "original").strip()
    role_ids = [s.strip() for s in (form.get("role_ids") or "").split(",") if s.strip()]
    template = (form.get("template") or "").strip()
    updated = forward_store.update_rule(source, dest, mode=mode, role_ids=role_ids, template=template)
    return JSONResponse({"ok": True, "updated": bool(updated)})


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
    await _assert_channels_in_scope(sess, guild_id, source, dest)
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
        if k.startswith("_"):
            continue          # _present / _next はフォームの制御用。.env には書かない
        if k in multi_keys:
            vals = [v for v in form.getlist(k) if v]
            updates[k] = ",".join(vals)
        else:
            v = form.get(k)
            if v is None:
                continue
            updates[k] = str(v)

    # 選択がゼロの <select multiple> はキー自体が送信されないため、上のループでは
    # 拾えない。write_env はマージ型なので、そのままだと「全部外して保存」しても
    # 古い値が残り続ける。フォーム側が _present で「この画面はこのキーを扱う」と
    # 申告し、送信が無ければ空文字で明示的に消す。
    for k in form.getlist("_present"):
        if k in multi_keys and k not in updates:
            updates[k] = ""

    # Auto-extract Google Sheet ID if user pasted a URL
    if "SHIPPING_SHEET_ID" in updates:
        sid = config_store.extract_sheet_id(updates["SHIPPING_SHEET_ID"])
        if sid:
            updates["SHIPPING_SHEET_ID"] = sid

    # 値が実際に変わったかを見て、変化が無ければ BOT を再起動しない。
    before = config_store.read_env(guild_id)
    changed = any((before.get(k) or "") != (v or "") for k, v in updates.items())

    config_store.write_env(guild_id, updates)

    # Verify Discord auth right after save so the user gets immediate feedback.
    # We call /guilds/{id} with the bot token; success means the token is valid AND
    # the bot is already in this guild.
    # トークンを含む保存（＝基盤設定ページ）のときだけ検証する。各ロボットの
    # 保存で毎回 Discord に問い合わせると、その分だけ保存が遅くなる。
    auth_status = "unknown"
    bot_token = updates.get("DISCORD_TOKEN_SUPPORT") or _bot_token_for(guild_id)
    if bot_token and "DISCORD_TOKEN_SUPPORT" in updates:
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

    # 設定が実際に変わったときだけ再起動する。以前は保存のたびに落として
    # 上げ直していたため、チャンネルを1つ変えるだけで BOT が切断されていた。
    if changed:
        bot_manager.restart(guild_id)

    # 部分フォームは自分のページへ戻す。guild 配下に限定してオープンリダイレクトを防ぐ。
    nxt = str(form.get("_next") or "")
    if not nxt.startswith(f"/guild/{guild_id}/"):
        nxt = f"/guild/{guild_id}"
    sep = "&" if "?" in nxt else "?"
    return RedirectResponse(f"{nxt}{sep}saved=1&auth={auth_status}", status_code=303)


@app.post("/guild/{guild_id}/credential")
async def upload_credential(
    guild_id: str,
    session: Optional[str] = Cookie(None),
    service_account: UploadFile = File(...),
):
    sess = require_session(session)
    require_guild_admin(sess, guild_id)
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
    require_guild_admin(sess, guild_id)
    try:
        st = bot_manager.start(guild_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(st)


@app.post("/guild/{guild_id}/stop")
async def guild_stop(guild_id: str, session: Optional[str] = Cookie(None)):
    sess = require_session(session)
    require_guild_admin(sess, guild_id)
    return JSONResponse(bot_manager.stop(guild_id))


@app.post("/guild/{guild_id}/restart")
async def guild_restart(guild_id: str, session: Optional[str] = Cookie(None)):
    sess = require_session(session)
    require_guild_admin(sess, guild_id)
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
    require_guild_admin(sess, guild_id)
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
        "host_id": _discord_user_id(sess),
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
        "host_id": _discord_user_id(sess),
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


# -------------------------- BOT の見た目（アイコン・表示名） --------------------------

# Discord のアバターは 10MB まで受けるが、実用上は 1MB 以内で十分。
MAX_AVATAR_BYTES = 2 * 1024 * 1024
AVATAR_MIME = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
}


# 用意済みの「結びロボ」アイコン。ユーザーが画像を持っていなくても選べる。
AVATAR_PRESETS = [
    {"key": "robo-navy", "label": "結びロボ（ネイビー）"},
    {"key": "robo-blue", "label": "結びロボ（ブルー）"},
    {"key": "robo-cream", "label": "結びロボ（キャラメル）"},
    {"key": "robo-mint", "label": "結びロボ（ミント）"},
    {"key": "knot-navy", "label": "結びマーク（濃）"},
    {"key": "knot-white", "label": "結びマーク（白）"},
]
_PRESET_KEYS = {p["key"] for p in AVATAR_PRESETS}


def _preset_bytes(key: str) -> Optional[bytes]:
    """プリセット画像を読む。キーは許可リスト照合済みのものだけ受ける
    （パス結合前に検証しないとディレクトリトラバーサルになる）。"""
    if key not in _PRESET_KEYS:
        return None
    path = HERE / "static" / "presets" / f"{key}.png"
    try:
        return path.read_bytes()
    except OSError:
        return None


def _sniff_image_mime(data: bytes) -> Optional[str]:
    """拡張子ではなく中身で判定する（偽装ファイルを Discord に投げないため）。"""
    for magic, mime in AVATAR_MIME.items():
        if data.startswith(magic):
            return mime
    return None


@app.post("/guild/{guild_id}/appearance")
async def update_bot_appearance(
    guild_id: str,
    session: Optional[str] = Cookie(None),
    bot_username: str = Form(""),
    preset: str = Form(""),
    avatar: Optional[UploadFile] = File(None),
):
    """BOT のアイコンと表示名を Discord に反映する。"""
    sess = require_session(session)
    require_guild_admin(sess, guild_id)
    token = _bot_token_for(guild_id)
    if not token:
        return RedirectResponse(
            f"/guild/{guild_id}/setup?appearance_err=先にBOTトークンを保存してください#tab-appearance",
            status_code=303,
        )

    data_uri = None
    raw = b""
    if avatar is not None and avatar.filename:
        raw = await avatar.read()
    elif preset:
        raw = _preset_bytes(preset) or b""
        if not raw:
            return RedirectResponse(
                f"/guild/{guild_id}/setup?appearance_err=プリセットが見つかりません#tab-appearance",
                status_code=303,
            )
    if raw:
        if len(raw) > MAX_AVATAR_BYTES:
            return RedirectResponse(
                f"/guild/{guild_id}/setup?appearance_err=画像が大きすぎます（2MBまで）#tab-appearance",
                status_code=303,
            )
        mime = _sniff_image_mime(raw)
        if not mime:
            return RedirectResponse(
                f"/guild/{guild_id}/setup?appearance_err=PNG・JPEG・GIF の画像を選んでください#tab-appearance",
                status_code=303,
            )
        data_uri = f"data:{mime};base64," + base64.b64encode(raw).decode()

    name = (bot_username or "").strip()
    if not name and not data_uri:
        return RedirectResponse(
            f"/guild/{guild_id}/setup?appearance_err=変更内容がありません#tab-appearance",
            status_code=303,
        )

    ok, msg = await DiscordREST(token).patch_me(username=name or None, avatar_data_uri=data_uri)
    key = "appearance_ok" if ok else "appearance_err"
    return RedirectResponse(
        f"/guild/{guild_id}/setup?{key}={msg}#tab-appearance", status_code=303
    )


# -------------------------- 送料設定のマスター複製・取り込み --------------------------


@app.get("/guild/{guild_id}/robot/shipping/master.xlsx")
async def shipping_master_download(
    guild_id: str,
    session: Optional[str] = Cookie(None),
    source: str = "current",
):
    """送料設定を Excel で書き出す。

    source=master ならリポジトリ同梱のマスター、既定はこのサーバーの現状。
    他社に導入するときは master を落として渡す。
    """
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)

    data = shipping_master.build_workbook(None if source == "master" else guild_id)
    name = "Musubot送料設定_マスター.xlsx" if source == "master" else "Musubot送料設定.xlsx"
    quoted = urllib.parse.quote(name)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
    )


@app.post("/guild/{guild_id}/robot/shipping/import")
async def shipping_import(
    request: Request,
    guild_id: str,
    session: Optional[str] = Cookie(None),
):
    """スプレッドシートからの貼り付けを取り込む。"""
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    form = await request.form()
    text = str(form.get("pasted") or "")

    back = f"/guild/{guild_id}/robot/shipping"
    try:
        kind, count, warnings = shipping_master.apply_paste(guild_id, text)
    except ValueError as e:
        return RedirectResponse(f"{back}?import_err={e}", status_code=303)
    except Exception:  # noqa: BLE001 — 取り込み失敗で画面を落とさない
        log.exception("送料設定の取り込みに失敗しました（%s）", guild_id)
        return RedirectResponse(f"{back}?import_err=取り込みに失敗しました", status_code=303)

    msg = f"{kind}を{count}件取り込みました"
    if warnings:
        msg += f"（注意 {len(warnings)}件）"
    q = urllib.parse.urlencode({"import_ok": msg, "warn": " / ".join(warnings[:5])})
    return RedirectResponse(f"{back}?{q}", status_code=303)


@app.post("/guild/{guild_id}/robot/shipping/reset")
async def shipping_reset(
    guild_id: str,
    session: Optional[str] = Cookie(None),
):
    """取り込んだ独自設定を捨てて、同梱マスターに戻す。"""
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    done = shipping_master.reset_config(guild_id)
    msg = "マスターに戻しました" if done else "独自設定はありません"
    return RedirectResponse(
        f"/guild/{guild_id}/robot/shipping?import_ok={msg}", status_code=303
    )


@app.post("/guild/{guild_id}/welcome/dm-preview")
async def welcome_dm_preview(
    request: Request, guild_id: str, session: Optional[str] = Cookie(None)
):
    """DM本文のプレビュー。BOTと同じ置換ロジックを通して返す。

    画面側で別に組み立てると、実際に届く文面とずれる。ここで同じ関数を
    使うことで、プレビューと本番の差が出ないようにする。
    """
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    form = await request.form()

    from services import dm_template
    body = dm_template.render(
        str(form.get("message") or ""),
        user_mention="@新しいメンバー",
        user_name="new_member",
        user_display="新しいメンバー",
        guild_name=_guild_name_from_session(sess, guild_id),
        member_count=1234,
        invite_url=str(form.get("invite_url") or ""),
    )
    return JSONResponse({"preview": body[:2000], "length": len(body)})


# -------------------------- 送料カートの商品登録 --------------------------


def _products_redirect(guild_id: str, **q) -> RedirectResponse:
    url = f"/guild/{guild_id}/robot/shipping"
    if q:
        url += "?" + urllib.parse.urlencode(q) + "#products"
    return RedirectResponse(url, status_code=303)


@app.post("/guild/{guild_id}/products/add")
async def products_add(
    request: Request, guild_id: str, session: Optional[str] = Cookie(None)
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    form = await request.form()
    try:
        item = products_store.validate(
            str(form.get("name_ja") or ""), str(form.get("name_en") or ""),
            str(form.get("weight_g") or ""), str(form.get("unit_ja") or ""),
            str(form.get("unit_en") or ""), str(form.get("emoji") or ""),
        )
        products_store.add(guild_id, item)
    except ValueError as e:
        return _products_redirect(guild_id, prod_err=str(e))
    return _products_redirect(guild_id, prod_ok="商品を追加しました")


@app.post("/guild/{guild_id}/products/update")
async def products_update(
    request: Request, guild_id: str, session: Optional[str] = Cookie(None)
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    form = await request.form()
    try:
        item = products_store.validate(
            str(form.get("name_ja") or ""), str(form.get("name_en") or ""),
            str(form.get("weight_g") or ""), str(form.get("unit_ja") or ""),
            str(form.get("unit_en") or ""), str(form.get("emoji") or ""),
        )
    except ValueError as e:
        return _products_redirect(guild_id, prod_err=str(e))
    ok = products_store.update(guild_id, str(form.get("id") or ""), item)
    return _products_redirect(
        guild_id, **({"prod_ok": "商品を更新しました"} if ok else {"prod_err": "見つかりませんでした"})
    )


@app.post("/guild/{guild_id}/products/remove")
async def products_remove(
    request: Request, guild_id: str, session: Optional[str] = Cookie(None)
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    form = await request.form()
    products_store.remove(guild_id, str(form.get("id") or ""))
    return _products_redirect(guild_id, prod_ok="商品を削除しました")


@app.post("/guild/{guild_id}/products/move")
async def products_move(
    request: Request, guild_id: str, session: Optional[str] = Cookie(None)
):
    """カートの選択メニューに出る順を入れ替える。"""
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    form = await request.form()
    delta = -1 if str(form.get("dir") or "") == "up" else 1
    products_store.move(guild_id, str(form.get("id") or ""), delta)
    return _products_redirect(guild_id)


@app.post("/guild/{guild_id}/products/reset")
async def products_reset(
    guild_id: str, session: Optional[str] = Cookie(None)
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    done = products_store.reset(guild_id)
    return _products_redirect(
        guild_id, prod_ok="既定の商品リストに戻しました" if done else "独自の登録はありません"
    )


# -------------------------- LINE → Discord 転送 --------------------------


@app.post("/line/webhook/{guild_id}")
async def line_webhook(
    guild_id: str,
    request: Request,
    background: BackgroundTasks,
):
    """LINE Messaging API の webhook 受け口。

    ここは Discord のセッションを持たない外部からの呼び出しなので、
    **署名検証だけが認証**になる。検証に通らないものは 403 で捨てる。

    LINE は応答が遅いと再送してくるため、実際の転送は背景タスクに逃がして
    すぐ 200 を返す。
    """
    env = config_store.read_env(guild_id)
    secret = (env.get("LINE_CHANNEL_SECRET") or "").strip()
    if not secret:
        raise HTTPException(status_code=404, detail="LINE forwarding not configured")

    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")
    if not line_forward.verify_signature(secret, body, signature):
        log.warning("LINE webhook の署名が一致しません (guild=%s)", guild_id)
        raise HTTPException(status_code=403, detail="bad signature")

    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON")

    for event in payload.get("events", []):
        background.add_task(_handle_line_event, guild_id, event)
    return {"ok": True}


async def _handle_line_event(guild_id: str, event: dict) -> None:
    """1件の LINE イベントを Discord へ転送する。

    失敗しても webhook 自体は既に 200 を返しているので、ここでは
    ログを残すだけにして LINE の再送を誘発しない。
    """
    env = config_store.read_env(guild_id)
    access_token = (env.get("LINE_CHANNEL_ACCESS_TOKEN") or "").strip()
    dest = (env.get("LINE_FORWARD_CHANNEL_ID") or "").strip()
    if not access_token or not dest.isdigit():
        return
    if not line_forward.is_allowed(event, env.get("LINE_ALLOWED_SOURCE_IDS", "")):
        return
    if event.get("type") != "message":
        return

    message = event.get("message") or {}
    mtype = message.get("type")
    forward_text = (env.get("LINE_FORWARD_TEXT") or "") == "1"
    if mtype not in ("image",) and not (mtype == "text" and forward_text):
        return

    bot_token = _bot_token_for(guild_id)
    if not bot_token:
        log.warning("LINE 転送: guild %s の BOT トークンがありません", guild_id)
        return

    name = await line_forward.sender_name(access_token, event)
    prefix = f"**{name}** さんが LINE に投稿しました" if name else "LINE に投稿されました"
    rest = DiscordREST(bot_token)

    try:
        if mtype == "text":
            await rest.create_message(dest, {"content": f"{prefix}\n{message.get('text', '')}"})
            return
        data = await line_forward.fetch_content(access_token, str(message.get("id")))
        if not data:
            return
        await rest.create_message(
            dest,
            {"content": prefix},
            image_bytes=data,
            image_filename=f"line-{message.get('id')}.jpg",
        )
        log.info("LINE から Discord へ転送しました (guild=%s)", guild_id)
    except Exception:  # noqa: BLE001 — 転送失敗でプロセスを落とさない
        log.exception("LINE 転送に失敗しました (guild=%s)", guild_id)


# -------------------------- 定期メッセージ --------------------------


@app.get("/guild/{guild_id}/schedule", response_class=HTMLResponse)
async def schedule_page(
    request: Request,
    guild_id: str,
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    bot_token = _bot_token_for(guild_id)

    channels_groups: list = []
    if bot_token:
        chs = await DiscordREST(bot_token).list_channels(guild_id)
        channels_groups = channels_grouped(chs)

    items = schedule_store.load(guild_id)
    ch_names = {}
    for _cat, chs in channels_groups:
        for c in chs:
            ch_names[str(c["id"])] = c["name"]
    for it in items:
        it["describe"] = schedule_store.describe(it)
        it["channel_name"] = ch_names.get(str(it.get("channel_id")), it.get("channel_id"))

    return templates.TemplateResponse(
        "schedule.html",
        {
            "request": request,
            "session": sess,
            "guild_id": guild_id,
            "guild_name": _guild_name_from_session(sess, guild_id),
            "channels_grouped": channels_groups,
            "discord_ok": bool(bot_token and channels_groups),
            "items": items,
            "weekday_labels": schedule_store.WEEKDAY_LABELS,
            "min_interval": schedule_store.MIN_INTERVAL_MINUTES,
        },
    )


def _schedule_form_spec(form) -> tuple[Optional[dict], Optional[str]]:
    return schedule_store.validate(
        channel_id=str(form.get("channel_id") or ""),
        message=str(form.get("message") or ""),
        mode=str(form.get("mode") or ""),
        time_str=str(form.get("time") or ""),
        weekday=str(form.get("weekday") or "0"),
        interval_minutes=str(form.get("interval_minutes") or "60"),
    )


@app.post("/guild/{guild_id}/schedule/add")
async def schedule_add(
    request: Request,
    guild_id: str,
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    form = await request.form()
    spec, err = _schedule_form_spec(form)
    if err:
        return RedirectResponse(f"/guild/{guild_id}/schedule?err={err}", status_code=303)
    schedule_store.add(guild_id, str(form.get("name") or ""), spec)
    return RedirectResponse(f"/guild/{guild_id}/schedule?ok=added", status_code=303)


@app.post("/guild/{guild_id}/schedule/update")
async def schedule_update(
    request: Request,
    guild_id: str,
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    form = await request.form()
    spec, err = _schedule_form_spec(form)
    if err:
        return RedirectResponse(f"/guild/{guild_id}/schedule?err={err}", status_code=303)
    ok = schedule_store.update(guild_id, str(form.get("id") or ""), str(form.get("name") or ""), spec)
    q = "ok=updated" if ok else "err=見つかりませんでした"
    return RedirectResponse(f"/guild/{guild_id}/schedule?{q}", status_code=303)


@app.post("/guild/{guild_id}/schedule/toggle")
async def schedule_toggle(
    request: Request,
    guild_id: str,
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    form = await request.form()
    schedule_store.toggle(
        guild_id, str(form.get("id") or ""), str(form.get("enabled") or "0") == "1"
    )
    return RedirectResponse(f"/guild/{guild_id}/schedule?ok=toggled", status_code=303)


@app.post("/guild/{guild_id}/schedule/remove")
async def schedule_remove(
    request: Request,
    guild_id: str,
    session: Optional[str] = Cookie(None),
):
    sess = require_session(session)
    require_admin_for_guild(sess, guild_id)
    form = await request.form()
    schedule_store.remove(guild_id, str(form.get("id") or ""))
    return RedirectResponse(f"/guild/{guild_id}/schedule?ok=removed", status_code=303)


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
