"""会社（テナント）の台帳。

Musubot を複数社に提供するための最小の仕組み。
deployments/<guild_id>/ の構造には一切触らず、dashboard_users.json と
同じ場所に tenants.json を1枚置くだけにしてある。

  /data/
    deployments/<guild_id>/...   ← 従来のまま
    dashboard_users.json         ← ユーザーに tenant フィールドが増えるだけ
    tenants.json                 ← ここ

1社ぶん:
  {
    "beyond": {
      "name": "BEYOND",
      "status": "active",        # active | suspended
      "guilds": ["123..."],      # この会社が触れる Discord サーバー
      "note": "",
      "created_at": 1786...
    }
  }
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Optional

from . import config_store

FILENAME = "tenants.json"
DEFAULT_SLUG = "beyond"


def _path() -> Path:
    return config_store.deployments_root().parent / FILENAME


def _load() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    tmp.replace(p)


def ensure_bootstrap() -> dict:
    """台帳が無ければ、いまある全サーバーを既定の1社にまとめて作る。

    既存（BEYOND単独運用）から移行するとき、何もしなくても今までどおり
    動く状態を作るためのもの。
    """
    data = _load()
    if data:
        return data
    data = {
        DEFAULT_SLUG: {
            "name": "BEYOND",
            "status": "active",
            "guilds": [str(g) for g in config_store.list_deployments()],
            "note": "移行時に自動作成",
            "created_at": int(time.time()),
        }
    }
    _save(data)
    return data


def slugify(name: str, existing: Optional[dict] = None) -> str:
    """会社名から識別子を作る。

    日本語だけの社名からは英数字が取れないので、その場合は company-1、
    company-2 … と連番にする。時刻由来の数字にすると意味が読めないため。
    """
    s = unicodedata.normalize("NFKC", (name or "").strip().lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if s:
        return s[:40]
    data = existing if existing is not None else _load()
    n = 1
    while f"company-{n}" in data:
        n += 1
    return f"company-{n}"


def list_tenants() -> list[dict]:
    data = ensure_bootstrap()
    out = []
    for slug, t in data.items():
        out.append({
            "slug": slug,
            "name": t.get("name") or slug,
            "status": t.get("status", "active"),
            "guilds": [str(g) for g in t.get("guilds", [])],
            "note": t.get("note", ""),
            "created_at": t.get("created_at"),
        })
    out.sort(key=lambda x: (x["status"] != "active", x["name"]))
    return out


def get(slug: str) -> Optional[dict]:
    t = ensure_bootstrap().get(slug)
    if not t:
        return None
    return {"slug": slug, **t, "guilds": [str(g) for g in t.get("guilds", [])]}


def add(name: str, note: str = "") -> dict:
    data = ensure_bootstrap()
    slug = slugify(name, data)
    base, i = slug, 2
    while slug in data:
        slug = f"{base}-{i}"
        i += 1
    data[slug] = {
        "name": (name or slug).strip(),
        "status": "active",
        "guilds": [],
        "note": note.strip(),
        "created_at": int(time.time()),
    }
    _save(data)
    return {"slug": slug, **data[slug]}


def set_status(slug: str, status: str) -> bool:
    if status not in ("active", "suspended"):
        return False
    data = ensure_bootstrap()
    if slug not in data:
        return False
    data[slug]["status"] = status
    _save(data)
    return True


def update(slug: str, name: Optional[str] = None, note: Optional[str] = None) -> bool:
    data = ensure_bootstrap()
    if slug not in data:
        return False
    if name is not None and name.strip():
        data[slug]["name"] = name.strip()
    if note is not None:
        data[slug]["note"] = note.strip()
    _save(data)
    return True


def remove(slug: str) -> bool:
    data = ensure_bootstrap()
    if slug not in data:
        return False
    del data[slug]
    _save(data)
    return True


def set_guilds(slug: str, guild_ids: list[str]) -> tuple[bool, list[tuple[str, str]]]:
    """担当サーバーを設定する。

    1つのサーバーを2社が同時に担当することはできないので、他社が持っている
    ものを指定したときは**その会社から取り上げて移す**。以前は黙って無視して
    いたため、「保存しました」と出るのに何も起きない状態になっていた。

    戻り値: (成功したか, [(サーバーID, 元の会社名), ...])
    """
    data = ensure_bootstrap()
    if slug not in data:
        return False, []

    wanted = [str(g) for g in guild_ids]
    moved: list[tuple[str, str]] = []
    for other, t in data.items():
        if other == slug:
            continue
        keep = []
        for g in [str(x) for x in t.get("guilds", [])]:
            if g in wanted:
                moved.append((g, t.get("name") or other))
            else:
                keep.append(g)
        t["guilds"] = keep

    data[slug]["guilds"] = wanted
    _save(data)
    return True, moved


def tenant_for_guild(guild_id: str) -> Optional[str]:
    for slug, t in ensure_bootstrap().items():
        if str(guild_id) in [str(g) for g in t.get("guilds", [])]:
            return slug
    return None


def guilds_for_tenant(slug: str) -> list[str]:
    t = ensure_bootstrap().get(slug) or {}
    return [str(g) for g in t.get("guilds", [])]


def is_active(slug: str) -> bool:
    t = ensure_bootstrap().get(slug)
    return bool(t) and t.get("status", "active") == "active"


def unassigned_guilds() -> list[str]:
    """どの会社にも割り当てられていないサーバー。割当漏れの検知用。"""
    assigned = set()
    for t in ensure_bootstrap().values():
        assigned.update(str(g) for g in t.get("guilds", []))
    return [str(g) for g in config_store.list_deployments() if str(g) not in assigned]
