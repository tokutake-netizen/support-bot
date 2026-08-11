"""定期メッセージ（スケジュール投稿）の設定ストア。

ダッシュボードが書き、BOT 側の cogs/scheduler.py が読む。
保存先は各デプロイの data/scheduled_messages.json で、BOT は mtime を見て
自動リロードするため、保存すれば再起動なしで反映される。

1件のスケジュール:
    {
      "id": "sc_1",
      "name": "毎朝の入荷案内",
      "channel_id": "123...",
      "message": "本文",
      "mode": "daily" | "weekly" | "interval",
      "time": "09:00",          # daily / weekly（JST）
      "weekday": 0,             # weekly のみ 0=月 ... 6=日
      "interval_minutes": 60,   # interval のみ
      "enabled": true,
      "last_run": 0.0           # BOT が書き込む（UNIX秒）
    }
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from . import config_store

FILENAME = "scheduled_messages.json"
MODES = ("daily", "weekly", "interval")
WEEKDAY_LABELS = ["月", "火", "水", "木", "金", "土", "日"]
MIN_INTERVAL_MINUTES = 10
MAX_MESSAGE_LEN = 1900  # Discord は2000字上限。余白を持たせる。
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _path(guild_id: str) -> Path:
    return config_store.deployment_dir(str(guild_id)) / "data" / FILENAME


def load(guild_id: str) -> list[dict]:
    p = _path(guild_id)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def save(guild_id: str, items: list[dict]) -> None:
    p = _path(guild_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(p)


def _next_id(items: list[dict]) -> str:
    n = 0
    for it in items:
        m = re.match(r"^sc_(\d+)$", str(it.get("id", "")))
        if m:
            n = max(n, int(m.group(1)))
    return f"sc_{n + 1}"


def validate(
    *,
    channel_id: str,
    message: str,
    mode: str,
    time_str: str,
    weekday: str,
    interval_minutes: str,
) -> tuple[Optional[dict], Optional[str]]:
    """フォーム入力を検証し、(正規化済みdict, エラー文) を返す。"""
    channel_id = (channel_id or "").strip()
    if not channel_id.isdigit():
        return None, "投稿先チャンネルを選択してください"

    message = (message or "").strip()
    if not message:
        return None, "メッセージ本文を入力してください"
    if len(message) > MAX_MESSAGE_LEN:
        return None, f"メッセージが長すぎます（{MAX_MESSAGE_LEN}文字まで）"

    if mode not in MODES:
        return None, "実行タイミングの指定が不正です"

    out = {"channel_id": channel_id, "message": message, "mode": mode}

    if mode == "interval":
        try:
            iv = int(interval_minutes)
        except (TypeError, ValueError):
            return None, "実行間隔は数値で入力してください"
        if iv < MIN_INTERVAL_MINUTES:
            return None, f"実行間隔は{MIN_INTERVAL_MINUTES}分以上にしてください"
        out["interval_minutes"] = iv
    else:
        if not _TIME_RE.match((time_str or "").strip()):
            return None, "時刻は HH:MM 形式で入力してください"
        out["time"] = time_str.strip()
        if mode == "weekly":
            try:
                wd = int(weekday)
            except (TypeError, ValueError):
                return None, "曜日を選択してください"
            if not 0 <= wd <= 6:
                return None, "曜日の指定が不正です"
            out["weekday"] = wd

    return out, None


def add(guild_id: str, name: str, spec: dict) -> dict:
    items = load(guild_id)
    item = {
        "id": _next_id(items),
        "name": (name or "").strip() or "定期メッセージ",
        "enabled": True,
        "last_run": 0.0,
        **spec,
    }
    items.append(item)
    save(guild_id, items)
    return item


def update(guild_id: str, sched_id: str, name: str, spec: dict) -> bool:
    items = load(guild_id)
    for it in items:
        if str(it.get("id")) == str(sched_id):
            # last_run は BOT の管理下なので保持する
            last_run = it.get("last_run", 0.0)
            enabled = it.get("enabled", True)
            it.clear()
            it.update(
                {
                    "id": sched_id,
                    "name": (name or "").strip() or "定期メッセージ",
                    "enabled": enabled,
                    "last_run": last_run,
                    **spec,
                }
            )
            save(guild_id, items)
            return True
    return False


def remove(guild_id: str, sched_id: str) -> bool:
    items = load(guild_id)
    kept = [it for it in items if str(it.get("id")) != str(sched_id)]
    if len(kept) == len(items):
        return False
    save(guild_id, kept)
    return True


def toggle(guild_id: str, sched_id: str, enabled: bool) -> bool:
    items = load(guild_id)
    for it in items:
        if str(it.get("id")) == str(sched_id):
            it["enabled"] = bool(enabled)
            save(guild_id, items)
            return True
    return False


def describe(item: dict) -> str:
    """一覧に出す人間向けの実行タイミング説明。"""
    mode = item.get("mode")
    if mode == "interval":
        iv = int(item.get("interval_minutes") or 0)
        if iv % 60 == 0 and iv >= 60:
            return f"{iv // 60}時間ごと"
        return f"{iv}分ごと"
    if mode == "weekly":
        wd = item.get("weekday")
        label = WEEKDAY_LABELS[wd] if isinstance(wd, int) and 0 <= wd <= 6 else "?"
        return f"毎週{label}曜 {item.get('time', '')}"
    return f"毎日 {item.get('time', '')}"
