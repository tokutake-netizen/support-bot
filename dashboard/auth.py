"""Discord OAuth2 helpers.

Flow:
  GET /login         -> redirect to Discord with state in cookie
  GET /oauth/callback -> exchange code for token -> fetch user + guilds -> set session
  GET /logout        -> clear session

Required env (set in Railway / .env):
  DISCORD_CLIENT_ID
  DISCORD_CLIENT_SECRET
  DASHBOARD_BASE_URL   e.g. https://your-app.up.railway.app   (no trailing slash)
  DASHBOARD_SECRET     for session signing (any 32+ char random string)
"""
from __future__ import annotations

import os
import secrets
from typing import Optional

import httpx

DISCORD_API = "https://discord.com/api/v10"
OAUTH_AUTHORIZE = "https://discord.com/oauth2/authorize"
OAUTH_TOKEN = "https://discord.com/api/oauth2/token"
SCOPES = "identify guilds"


def env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


def redirect_uri() -> str:
    return f"{env('DASHBOARD_BASE_URL').rstrip('/')}/oauth/callback"


def build_authorize_url(state: str) -> str:
    from urllib.parse import urlencode

    params = {
        "client_id": env("DISCORD_CLIENT_ID"),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "prompt": "consent",
    }
    return f"{OAUTH_AUTHORIZE}?{urlencode(params)}"


def gen_state() -> str:
    return secrets.token_urlsafe(24)


async def exchange_code(code: str) -> dict:
    """Exchange an authorization code for an access token."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            OAUTH_TOKEN,
            data={
                "client_id": env("DISCORD_CLIENT_ID"),
                "client_secret": env("DISCORD_CLIENT_SECRET"),
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri(),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r.raise_for_status()
        return r.json()


async def fetch_user(access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        r.raise_for_status()
        return r.json()


async def fetch_user_guilds(access_token: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{DISCORD_API}/users/@me/guilds",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        r.raise_for_status()
        return r.json()


PERMISSION_ADMINISTRATOR = 1 << 3


def is_admin(guild: dict) -> bool:
    """Check if the OAuth-returned guild dict marks the user as administrator."""
    if guild.get("owner"):
        return True
    try:
        perms = int(guild.get("permissions", 0))
    except (TypeError, ValueError):
        return False
    return bool(perms & PERMISSION_ADMINISTRATOR)
