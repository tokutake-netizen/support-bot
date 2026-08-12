"""設定データのバックアップ。

全テナントの設定・APIキー・ユーザー台帳・抽選や競りの状態は、Railway の
永続ボリューム1枚に載っている。誤操作かボリューム障害ひとつで全顧客の
データが消え、復旧手段が無い状態だった。

日次で /data を tar.gz に固め、世代を保持する。外部（S3等）への退避は
まだ入れていないので、ボリューム自体が失われる事態には対応できない。
それでも「誤って消した」「壊れた」からの復旧はできる。
"""
from __future__ import annotations

import logging
import os
import shutil
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import config_store

log = logging.getLogger(__name__)

KEEP_DAYS = 14
JST = timezone(timedelta(hours=9))

# バックアップするもの。親ディレクトリを丸ごと固めると、無関係なものまで
# 巻き込んで肥大化する（ローカル検証で67MBになった）。必要なものだけ挙げる。
INCLUDE = (
    "deployments",
    "tenants.json",
    "dashboard_users.json",
    "dashboard_discord_roots.json",
    "signups.json",
    "member_stats.json",
)
EXCLUDE_NAMES = {"backups", "__pycache__", ".git", "credentials"}
EXCLUDE_SUFFIX = (".log", ".tmp", ".json.tmp", ".env.tmp", ".pyc")


def backup_dir() -> Path:
    override = os.environ.get("BACKUP_DIR")
    if override:
        return Path(override)
    return config_store.deployments_root().parent / "backups"


def _target_root() -> Path:
    return config_store.deployments_root().parent


def _keep(path: Path) -> bool:
    if path.name in EXCLUDE_NAMES:
        return False
    return not path.name.endswith(EXCLUDE_SUFFIX)


def create() -> Optional[Path]:
    """いまの状態を1本の tar.gz にする。

    tarfile は同期処理。イベントループ上で直接呼ぶと、圧縮の間ダッシュボード
    全体が無応答になる（実際にヘルスチェックが返らなくなった）。非同期の
    文脈からは asyncio.to_thread 経由で呼ぶこと。
    """
    root = _target_root()
    if not root.exists():
        return None
    out_dir = backup_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(JST).strftime("%Y%m%d-%H%M%S")
    out = out_dir / f"musubot-{stamp}.tar.gz"
    # with_suffix は最後の .gz だけを置き換えるので名前が壊れる。文字列で作る。
    tmp = out_dir / (out.name + ".tmp")
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            for name in INCLUDE:
                item = root / name
                if not item.exists() or not _keep(item):
                    continue
                tar.add(item, arcname=name, filter=_filter)
        tmp.replace(out)
    except OSError:
        log.exception("バックアップの作成に失敗しました")
        tmp.unlink(missing_ok=True)
        return None
    log.info("バックアップを作成しました: %s (%.1f MB)", out.name, out.stat().st_size / 1e6)
    return out


def _filter(info: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
    name = Path(info.name).name
    if name in EXCLUDE_NAMES or name.endswith(EXCLUDE_SUFFIX):
        return None
    return info


def prune(keep_days: int = KEEP_DAYS) -> int:
    """古い世代を捨てる。ボリュームを埋めると全社を巻き込むため。"""
    out_dir = backup_dir()
    if not out_dir.exists():
        return 0
    cutoff = time.time() - keep_days * 86400
    n = 0
    for f in out_dir.glob("musubot-*.tar.gz"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except OSError:
            log.warning("古いバックアップを削除できませんでした: %s", f.name)
    return n


def list_backups() -> list[dict]:
    out_dir = backup_dir()
    if not out_dir.exists():
        return []
    items = []
    for f in sorted(out_dir.glob("musubot-*.tar.gz"), reverse=True):
        try:
            st = f.stat()
        except OSError:
            continue
        items.append({
            "name": f.name,
            "size_mb": round(st.st_size / 1e6, 2),
            "at": datetime.fromtimestamp(st.st_mtime, JST).strftime("%Y-%m-%d %H:%M"),
        })
    return items


def disk_free_mb() -> float:
    try:
        return shutil.disk_usage(_target_root()).free / 1e6
    except OSError:
        return -1.0
