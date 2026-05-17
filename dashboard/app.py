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

import random
from datetime import datetime, timezone

from . import auth, bot_manager, config_store, giveaway_helpers as gh
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
