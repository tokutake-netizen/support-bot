"""サーバー人数の推移を記録する軽量ストア。

Discord は「過去のメンバー数」を提供しないため、ダッシュボードを開いたタイミングで
現在値をスナップショットし、1日1点として蓄積する。

保存先: <deployments_root>/../member_stats.json （Railway では永続ボリューム上）
形式:   {"<guild_id>": {"YYYY-MM-DD": {"total": int, "online": int}}}
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import config_store

# 同じ日に何度もダッシュボードを開いても書き込みは1回で足りるが、
# 当日分は最新値で上書きしていく（人数は増減するため）。
_JST = timezone(timedelta(hours=9))
MAX_DAYS = 180


def _path() -> Path:
    override = os.environ.get("MEMBER_STATS_PATH")
    if override:
        return Path(override)
    return config_store.deployments_root().parent / "member_stats.json"


def _load() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    p = _path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        tmp.replace(p)
    except OSError:
        pass


def _today() -> str:
    return datetime.now(_JST).strftime("%Y-%m-%d")


def record(guild_id: str, total: Optional[int], online: Optional[int] = None) -> None:
    """当日のスナップショットを記録（同日は上書き）。"""
    if total is None:
        return
    data = _load()
    g = data.setdefault(str(guild_id), {})
    entry = {"total": int(total)}
    if online is not None:
        entry["online"] = int(online)
    g[_today()] = entry

    # 古すぎる点を捨てる
    if len(g) > MAX_DAYS:
        for k in sorted(g.keys())[: len(g) - MAX_DAYS]:
            g.pop(k, None)
    _save(data)


def series(guild_id: str, days: int = 30) -> list[dict]:
    """直近 days 日ぶんの [{date, total, online}] を古い順で返す。"""
    g = _load().get(str(guild_id), {})
    if not g:
        return []
    cutoff = (datetime.now(_JST) - timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    for d in sorted(g.keys()):
        if d < cutoff:
            continue
        e = g[d]
        out.append({"date": d, "total": e.get("total"), "online": e.get("online")})
    return out


def summary(guild_id: str, days: int = 30) -> Optional[dict]:
    """最新値と期間内の増減。データが1点も無ければ None。"""
    s = series(guild_id, days)
    if not s:
        return None
    latest = s[-1]["total"]
    first = s[0]["total"]
    return {
        "latest": latest,
        "delta": (latest - first) if (latest is not None and first is not None) else None,
        "points": len(s),
        "series": s,
    }


def should_refresh(guild_id: str) -> bool:
    """当日ぶんが未記録なら True（＝Discord に問い合わせる価値がある）。"""
    g = _load().get(str(guild_id), {})
    return _today() not in g
