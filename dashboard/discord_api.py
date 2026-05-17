"""Discord REST helpers using a bot token (not user OAuth).

Used to enumerate channels/categories/roles for the setup UI dropdowns.
A bot token gives a stable, high-rate-limit way to inspect any guild the
bot has joined — much better than relying on the user's OAuth token, which
has guild-level scopes but no channel listing.
"""
from __future__ import annotations

from typing import Optional

import httpx

DISCORD_API = "https://discord.com/api/v10"


class DiscordREST:
    def __init__(self, bot_token: str) -> None:
        self.token = bot_token

    def _headers(self) -> dict:
        return {"Authorization": f"Bot {self.token}", "User-Agent": "support_bot_dashboard"}

    async def get_guild(self, guild_id: int | str) -> Optional[dict]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{DISCORD_API}/guilds/{guild_id}", headers=self._headers())
            return r.json() if r.status_code == 200 else None

    async def list_channels(self, guild_id: int | str) -> list[dict]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{DISCORD_API}/guilds/{guild_id}/channels", headers=self._headers()
            )
            return r.json() if r.status_code == 200 else []

    async def list_roles(self, guild_id: int | str) -> list[dict]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{DISCORD_API}/guilds/{guild_id}/roles", headers=self._headers()
            )
            return r.json() if r.status_code == 200 else []


# Discord channel types we care about
CH_TEXT = 0
CH_VOICE = 2
CH_CATEGORY = 4
CH_NEWS = 5
CH_FORUM = 15


def split_channels(channels: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (categories, text-like channels) sorted by position."""
    cats = sorted(
        [c for c in channels if c.get("type") == CH_CATEGORY],
        key=lambda c: c.get("position", 0),
    )
    text_like = sorted(
        [c for c in channels if c.get("type") in (CH_TEXT, CH_NEWS, CH_FORUM)],
        key=lambda c: (c.get("parent_id") or "", c.get("position", 0)),
    )
    return cats, text_like


def channels_grouped(channels: list[dict]) -> list[tuple[Optional[dict], list[dict]]]:
    """Return [(category_or_None, [channels under it])] preserving Discord's order."""
    cats, texts = split_channels(channels)
    by_parent: dict[Optional[str], list[dict]] = {}
    for c in texts:
        by_parent.setdefault(c.get("parent_id"), []).append(c)

    out: list[tuple[Optional[dict], list[dict]]] = []
    # Channels without a category
    if None in by_parent:
        out.append((None, by_parent[None]))
    for cat in cats:
        out.append((cat, by_parent.get(cat["id"], [])))
    return out


def assignable_roles(roles: list[dict]) -> list[dict]:
    """Roles users can pick (not @everyone, not managed integrations) sorted by position desc."""
    out = [r for r in roles if not r.get("managed") and r.get("name") != "@everyone"]
    return sorted(out, key=lambda r: r.get("position", 0), reverse=True)
