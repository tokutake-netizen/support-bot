"""LINE グループに投稿された画像を Discord へ転送する。

LINE Messaging API の webhook を受け、画像メッセージなら本体を取得して
Discord のチャンネルへ添付投稿する。

前提:
  - LINE 公式アカウント（Messaging API チャネル）を作り、転送元のグループに
    その BOT を招待しておく。
  - LINE Developers の Webhook URL に
    https://<ダッシュボード>/line/webhook/<guild_id> を設定する。

安全性:
  webhook の URL は推測できてしまうので、**署名検証が唯一の防御**になる。
  LINE は本文の HMAC-SHA256（鍵＝チャネルシークレット）を X-Line-Signature
  に入れて送ってくるので、これが一致しないリクエストは捨てる。

設定は各デプロイの .env に入る:
  LINE_CHANNEL_SECRET        署名検証に使う
  LINE_CHANNEL_ACCESS_TOKEN  画像取得に使う
  LINE_FORWARD_CHANNEL_ID    転送先の Discord チャンネル
  LINE_FORWARD_TEXT          "1" ならテキストも転送する
  LINE_ALLOWED_SOURCE_IDS    転送を許可するグループ/ルームID（空なら全部）
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Optional

import httpx

log = logging.getLogger(__name__)

LINE_CONTENT_API = "https://api-data.line.me/v2/bot/message/{message_id}/content"
LINE_PROFILE_API = "https://api.line.me/v2/bot/{scope}/member/{user_id}"

# Discord の添付上限に対する安全側の上限。LINE の画像は通常これに収まる。
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def verify_signature(channel_secret: str, body: bytes, signature: str) -> bool:
    """X-Line-Signature を検証する。

    比較は hmac.compare_digest で行う（タイミング攻撃を避けるため）。
    """
    if not channel_secret or not signature:
        return False
    mac = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def source_id(event: dict) -> str:
    """イベントの発生元（グループ/ルーム/個人）のID。"""
    src = event.get("source") or {}
    return str(src.get("groupId") or src.get("roomId") or src.get("userId") or "")


def is_allowed(event: dict, allowed_csv: str) -> bool:
    """転送を許可する発生元かどうか。allowed_csv が空なら全部許可。"""
    allowed = [x.strip() for x in (allowed_csv or "").split(",") if x.strip()]
    if not allowed:
        return True
    return source_id(event) in allowed


async def fetch_content(access_token: str, message_id: str) -> Optional[bytes]:
    """LINE から画像の実データを取得する。

    LINE のコンテンツは一定期間で消えるため、webhook を受けたらすぐ呼ぶ。
    """
    url = LINE_CONTENT_API.format(message_id=message_id)
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        log.warning("LINE 画像の取得に失敗しました (message_id=%s): %s", message_id, e)
        return None
    if r.status_code != 200:
        log.warning("LINE 画像の取得が %s を返しました (message_id=%s)", r.status_code, message_id)
        return None
    if len(r.content) > MAX_IMAGE_BYTES:
        log.warning("LINE 画像が大きすぎます (%d bytes)", len(r.content))
        return None
    return r.content


async def sender_name(access_token: str, event: dict) -> str:
    """投稿者の表示名。取得できなければ空文字（転送は続行する）。"""
    src = event.get("source") or {}
    user_id = src.get("userId")
    if not user_id:
        return ""
    if src.get("groupId"):
        scope = f"group/{src['groupId']}"
    elif src.get("roomId"):
        scope = f"room/{src['roomId']}"
    else:
        scope = "profile"
    url = (
        f"https://api.line.me/v2/bot/{scope}/member/{user_id}"
        if scope != "profile"
        else f"https://api.line.me/v2/bot/profile/{user_id}"
    )
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
        if r.status_code == 200:
            return str(r.json().get("displayName") or "")
    except httpx.HTTPError:
        pass
    return ""


def extension_for(content_type: str) -> str:
    return {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/gif": "gif",
    }.get((content_type or "").lower(), "jpg")
