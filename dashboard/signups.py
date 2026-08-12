"""導入申し込みの受付。

お客様（他社）が申し込みフォームを書き、スーパー管理者が承認すると
会社（テナント）と担当者アカウントが同時に作られる。

保存先: dashboard_users.json と同じ場所の signups.json
1件ぶん:
  {
    "id": "sg_1",
    "company": "株式会社サンプル",
    "contact": "山田太郎",
    "email": "yamada@example.co.jp",
    "phone": "",
    "guild_id": "123456789012345678",   # 任意
    "note": "カード販売。翻訳とチケットを使いたい",
    "status": "pending",                # pending | approved | rejected
    "created_at": 1786...,
    "handled_at": null,
    "handled_by": ""
  }
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

from . import config_store

FILENAME = "signups.json"
MAX_PENDING = 200           # 荒らし対策の上限
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
GUILD_RE = re.compile(r"^\d{17,20}$")


def _path() -> Path:
    return config_store.deployments_root().parent / FILENAME


def _load() -> list[dict]:
    p = _path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _save(items: list[dict]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", "utf-8")
    tmp.replace(p)


def _next_id(items: list[dict]) -> str:
    n = 0
    for it in items:
        m = re.match(r"^sg_(\d+)$", str(it.get("id", "")))
        if m:
            n = max(n, int(m.group(1)))
    return f"sg_{n + 1}"


def submit(
    company: str, contact: str, email: str,
    phone: str = "", guild_id: str = "", note: str = "",
) -> dict:
    """申し込みを受け付ける。入力の検証はここで完結させる。"""
    company = (company or "").strip()
    contact = (contact or "").strip()
    email = (email or "").strip().lower()
    guild_id = (guild_id or "").strip()

    if not company:
        raise ValueError("会社名を入力してください")
    if not contact:
        raise ValueError("ご担当者名を入力してください")
    if not EMAIL_RE.match(email):
        raise ValueError("メールアドレスの形式が正しくありません")
    if guild_id and not GUILD_RE.match(guild_id):
        raise ValueError("DiscordサーバーIDは17〜20桁の数字です")

    items = _load()
    if sum(1 for i in items if i.get("status") == "pending") >= MAX_PENDING:
        raise ValueError("ただいま受付が混み合っています。時間をおいてお試しください")
    for i in items:
        if i.get("status") == "pending" and i.get("email") == email:
            raise ValueError("このメールアドレスの申し込みは受付済みです。ご連絡をお待ちください")

    item = {
        "id": _next_id(items),
        "company": company[:80],
        "contact": contact[:40],
        "email": email,
        "phone": (phone or "").strip()[:30],
        "guild_id": guild_id,
        "note": (note or "").strip()[:400],
        "status": "pending",
        "created_at": int(time.time()),
        "handled_at": None,
        "handled_by": "",
    }
    items.append(item)
    _save(items)
    return item


def list_all(status: Optional[str] = None) -> list[dict]:
    items = _load()
    if status:
        items = [i for i in items if i.get("status") == status]
    items.sort(key=lambda i: i.get("created_at") or 0, reverse=True)
    return items


def count_pending() -> int:
    return sum(1 for i in _load() if i.get("status") == "pending")


def get(sid: str) -> Optional[dict]:
    for i in _load():
        if str(i.get("id")) == str(sid):
            return i
    return None


def mark(sid: str, status: str, by: str = "") -> bool:
    if status not in ("approved", "rejected", "pending"):
        return False
    items = _load()
    for i in items:
        if str(i.get("id")) == str(sid):
            i["status"] = status
            i["handled_at"] = int(time.time())
            i["handled_by"] = by
            _save(items)
            return True
    return False
