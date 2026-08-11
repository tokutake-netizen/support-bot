"""Feature: 定期メッセージ（スケジュール投稿）。

ダッシュボードが data/scheduled_messages.json に書いた設定を読み、
指定のタイミングで指定チャンネルへメッセージを投稿する。

設定ファイルは mtime を監視して自動リロードするので、ダッシュボードで
保存すれば BOT の再起動なしに反映される。

1件の形式は dashboard/schedule_store.py のドキュメント参照。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands, tasks

try:
    from zoneinfo import ZoneInfo
    JST = ZoneInfo("Asia/Tokyo")
except Exception:  # pragma: no cover - tzdata が無い環境向け
    JST = timezone(timedelta(hours=9))

log = logging.getLogger(__name__)

SCHEDULE_FILE = Path("data") / "scheduled_messages.json"
# 起動直後や長時間停止のあとに、過ぎた予定をまとめて撃たないための猶予。
# 予定時刻からこれ以上経過していたらその回は見送る。
CATCHUP_GRACE_SECONDS = 15 * 60


def _parse_hhmm(s: str) -> Optional[tuple[int, int]]:
    try:
        h, m = str(s).split(":")
        h, m = int(h), int(m)
    except (ValueError, AttributeError):
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None


def due_at(item: dict, now: datetime) -> Optional[datetime]:
    """この設定の「直近の実行予定時刻」を返す。interval は None。"""
    mode = item.get("mode")
    if mode == "interval":
        return None
    hm = _parse_hhmm(item.get("time", ""))
    if not hm:
        return None
    h, m = hm
    today = now.astimezone(JST).replace(hour=h, minute=m, second=0, microsecond=0)
    if mode == "weekly":
        wd = item.get("weekday")
        if not isinstance(wd, int) or not 0 <= wd <= 6:
            return None
        if today.weekday() != wd:
            return None
    return today


def should_run(item: dict, now: Optional[datetime] = None) -> bool:
    """いま投稿すべきかどうか。"""
    if not item.get("enabled", True):
        return False
    now = now or datetime.now(JST)
    now_ts = now.timestamp()
    last_run = float(item.get("last_run") or 0.0)

    if item.get("mode") == "interval":
        iv = int(item.get("interval_minutes") or 0)
        if iv <= 0:
            return False
        # 初回（last_run=0）は登録直後の即時連投を避けるため1周期待つ。
        if last_run <= 0:
            item["last_run"] = now_ts
            return False
        return now_ts - last_run >= iv * 60

    target = due_at(item, now)
    if target is None:
        return False
    delta = now_ts - target.timestamp()
    if delta < 0 or delta > CATCHUP_GRACE_SECONDS:
        return False
    # 同じ予定時刻で二重投稿しない
    return last_run < target.timestamp()


class Scheduler(commands.Cog):
    """定期メッセージを投稿するループ。"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._items: list[dict] = []
        self._mtime: float = -1.0
        self._reload()

    async def cog_load(self) -> None:
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()

    # ---------- 設定ファイル ----------

    def _reload(self) -> None:
        """mtime が変わっていれば読み直す。"""
        try:
            mtime = SCHEDULE_FILE.stat().st_mtime
        except OSError:
            if self._items:
                log.info("scheduled_messages.json が無くなりました。定期メッセージを停止します")
            self._items, self._mtime = [], -1.0
            return
        if mtime == self._mtime:
            return
        try:
            data = json.loads(SCHEDULE_FILE.read_text("utf-8"))
        except (OSError, ValueError):
            log.warning("scheduled_messages.json を読めませんでした")
            return
        if not isinstance(data, list):
            return
        # ファイルの last_run はダッシュボード保存で巻き戻ることがあるため、
        # メモリ上の進行状況を id 単位で引き継ぐ。
        prev = {str(i.get("id")): float(i.get("last_run") or 0.0) for i in self._items}
        for it in data:
            key = str(it.get("id"))
            it["last_run"] = max(float(it.get("last_run") or 0.0), prev.get(key, 0.0))
        self._items = data
        self._mtime = mtime
        log.info("定期メッセージを読み込みました: %d 件", len(self._items))

    def _persist(self) -> None:
        """last_run を書き戻す（再起動後の二重投稿を防ぐ）。"""
        try:
            SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = SCHEDULE_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._items, ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(SCHEDULE_FILE)
            self._mtime = SCHEDULE_FILE.stat().st_mtime
        except OSError:
            log.exception("scheduled_messages.json の書き込みに失敗しました")

    # ---------- 実行ループ ----------

    @tasks.loop(seconds=30)
    async def tick(self) -> None:
        self._reload()
        if not self._items:
            return
        now = datetime.now(JST)
        fired = False
        for item in self._items:
            if not should_run(item, now):
                continue
            if await self._post(item):
                item["last_run"] = now.timestamp()
                fired = True
        if fired:
            self._persist()

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def _post(self, item: dict) -> bool:
        raw = str(item.get("channel_id") or "")
        if not raw.isdigit():
            return False
        channel = self.bot.get_channel(int(raw))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(raw))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.warning("定期メッセージ: チャンネル %s を取得できません", raw)
                return False
        try:
            await channel.send(item.get("message", ""))
        except (discord.Forbidden, discord.HTTPException):
            log.exception("定期メッセージの投稿に失敗しました (id=%s)", item.get("id"))
            return False
        log.info("定期メッセージを投稿しました: %s -> #%s", item.get("name"), getattr(channel, "name", raw))
        return True


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Scheduler(bot))
