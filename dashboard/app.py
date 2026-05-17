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

from . import auth, bot_manager, config_store
from .discord_api import DiscordREST, assignable_roles, channels_grouped

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
    for g in sess.get("guilds", []):
        if str(g["id"]) == str(guild_id) and g.get("admin"):
            return g
    raise HTTPException(status_code=403, detail="not an admin of that guild")


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
    url = auth.build_authorize_url(state)
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
    guilds = [
        {"id": g["id"], "name": g["name"], "icon": g.get("icon"), "admin": auth.is_admin(g)}
        for g in raw_guilds
    ]
    sess = {
        "user_id": user["id"],
        "username": user.get("global_name") or user.get("username"),
        "avatar": user.get("avatar"),
        "guilds": guilds,
    }
    resp = RedirectResponse("/dashboard")
    set_session(resp, sess)
    resp.delete_cookie("oauth_state")
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login")
    resp.delete_cookie("session")
    return resp


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, session: Optional[str] = Cookie(None)):
    sess = get_session(session)
    if not sess:
        return RedirectResponse("/login")
    admin_guilds = [g for g in sess.get("guilds", []) if g.get("admin")]
    # Annotate with bot running status
    for g in admin_guilds:
        g["bot_running"] = bot_manager.is_running(str(g["id"]))
        g["configured"] = (config_store.deployment_dir(str(g["id"])) / ".env").exists()
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "session": sess, "guilds": admin_guilds},
    )


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
        },
    )


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
        "ALLOW_CH_SHIPPING",
        "ALLOW_CAT_SHIPPING",
        "ALLOW_CH_GIVEAWAY",
        "ALLOW_CH_INVITE",
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
    bot_manager.restart(guild_id)
    return RedirectResponse(f"/guild/{guild_id}/setup?saved=1", status_code=303)


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
