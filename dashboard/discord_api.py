"""Discord REST helpers using a bot token (not user OAuth).

Used to enumerate channels/categories/roles for the setup UI dropdowns.
A bot token gives a stable, high-rate-limit way to inspect any guild the
bot has joined — much better than relying on the user's OAuth token, which
has guild-level scopes but no channel listing.
"""
from __future__ import annotations

import json
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

    async def get_guild_counts(self, guild_id: int | str) -> Optional[dict]:
        """メンバー数つきでギルドを取得。人数推移の記録に使う。

        with_counts=true で approximate_member_count / approximate_presence_count
        が付く（概算だが Discord が返す唯一の集計値）。
        """
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                f"{DISCORD_API}/guilds/{guild_id}",
                params={"with_counts": "true"},
                headers=self._headers(),
            )
            if r.status_code != 200:
                return None
            g = r.json()
            return {
                "total": g.get("approximate_member_count"),
                "online": g.get("approximate_presence_count"),
            }

    async def list_my_guilds(self) -> list[dict]:
        """このBotトークンが参加している全ギルドを返す（転送ピッカー用）。"""
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{DISCORD_API}/users/@me/guilds", headers=self._headers())
            return r.json() if r.status_code == 200 else []

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

    async def create_message(
        self,
        channel_id: int | str,
        payload: dict,
        image_bytes: Optional[bytes] = None,
        image_filename: Optional[str] = None,
    ) -> dict:
        """POST a message to a channel. Optionally attach an image file.

        When ``image_bytes`` is provided, the message is sent as multipart
        with ``payload_json`` and the file attached as ``files[0]``. The
        embed in payload should reference the attachment via
        ``embed.image.url = "attachment://<filename>"`` if it wants to
        display the upload inline.
        """
        url = f"{DISCORD_API}/channels/{channel_id}/messages"
        async with httpx.AsyncClient(timeout=15.0) as client:
            if image_bytes:
                files = {
                    "files[0]": (image_filename or "image.png", image_bytes, "application/octet-stream")
                }
                data = {"payload_json": json.dumps(payload)}
                r = await client.post(
                    url, headers=self._headers(), files=files, data=data
                )
            else:
                hdr = {**self._headers(), "Content-Type": "application/json"}
                r = await client.post(url, headers=hdr, json=payload)
            r.raise_for_status()
            return r.json()

    async def get_me(self) -> Optional[dict]:
        """BOT 自身のプロフィール（表示名・アバター）を取得。"""
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{DISCORD_API}/users/@me", headers=self._headers())
            return r.json() if r.status_code == 200 else None

    async def patch_me(
        self, username: Optional[str] = None, avatar_data_uri: Optional[str] = None
    ) -> tuple[bool, str]:
        """BOT の表示名・アイコンを変更する。(成功したか, メッセージ) を返す。

        avatar_data_uri は "data:image/png;base64,..." 形式。Discord は
        ユーザー名変更を厳しくレート制限する（連続変更で 429）ため、
        呼び出し側でエラーをそのまま見せる。
        """
        payload: dict = {}
        if username:
            payload["username"] = username
        if avatar_data_uri:
            payload["avatar"] = avatar_data_uri
        if not payload:
            return False, "変更内容がありません"

        hdr = {**self._headers(), "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.patch(f"{DISCORD_API}/users/@me", headers=hdr, json=payload)
        if r.status_code == 200:
            return True, "更新しました"
        if r.status_code == 429:
            return False, "Discord のレート制限中です。しばらく待ってからもう一度お試しください"
        try:
            detail = r.json()
            msg = detail.get("message") or str(detail)
        except ValueError:
            msg = r.text[:200]
        return False, f"Discord がエラーを返しました（{r.status_code}）: {msg}"

    async def patch_message(
        self, channel_id: int | str, message_id: int | str, payload: dict
    ) -> dict:
        url = f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}"
        hdr = {**self._headers(), "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.patch(url, headers=hdr, json=payload)
            r.raise_for_status()
            return r.json()

    # ---------- guild settings ----------

    async def patch_guild(self, guild_id: int | str, payload: dict) -> dict:
        url = f"{DISCORD_API}/guilds/{guild_id}"
        hdr = {**self._headers(), "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.patch(url, headers=hdr, json=payload)
            r.raise_for_status()
            return r.json()

    async def get_onboarding(self, guild_id: int | str) -> Optional[dict]:
        url = f"{DISCORD_API}/guilds/{guild_id}/onboarding"
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, headers=self._headers())
            return r.json() if r.status_code == 200 else None

    async def put_onboarding(self, guild_id: int | str, payload: dict) -> dict:
        url = f"{DISCORD_API}/guilds/{guild_id}/onboarding"
        hdr = {**self._headers(), "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.put(url, headers=hdr, json=payload)
            r.raise_for_status()
            return r.json()

    # ---------- scheduled events ----------

    async def list_scheduled_events(self, guild_id: int | str) -> list[dict]:
        url = f"{DISCORD_API}/guilds/{guild_id}/scheduled-events"
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, headers=self._headers(), params={"with_user_count": "true"})
            return r.json() if r.status_code == 200 else []

    async def create_scheduled_event(self, guild_id: int | str, payload: dict) -> dict:
        url = f"{DISCORD_API}/guilds/{guild_id}/scheduled-events"
        hdr = {**self._headers(), "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, headers=hdr, json=payload)
            r.raise_for_status()
            return r.json()

    async def patch_scheduled_event(
        self, guild_id: int | str, event_id: int | str, payload: dict
    ) -> dict:
        url = f"{DISCORD_API}/guilds/{guild_id}/scheduled-events/{event_id}"
        hdr = {**self._headers(), "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.patch(url, headers=hdr, json=payload)
            r.raise_for_status()
            return r.json()

    async def delete_scheduled_event(self, guild_id: int | str, event_id: int | str) -> bool:
        url = f"{DISCORD_API}/guilds/{guild_id}/scheduled-events/{event_id}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.delete(url, headers=self._headers())
            return r.status_code in (204, 200)


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
