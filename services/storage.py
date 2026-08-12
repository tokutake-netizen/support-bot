"""Tiny JSON file store. Reads/writes data/ relative to CWD (the deployment dir)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _data_dir() -> Path:
    p = Path("data")
    p.mkdir(parents=True, exist_ok=True)
    return p


DATA_DIR = _data_dir()


def load(name: str, default: Any = None) -> Any:
    path = DATA_DIR / name
    if not path.exists():
        return default if default is not None else {}
    try:
        return json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError:
        return default if default is not None else {}


def save(name: str, data: Any) -> None:
    """一時ファイルに書いてから差し替える。

    直書きだと、書き込み中にプロセスが落ちた場合に壊れた JSON が残る。
    load() は壊れていると既定値（空）を返す作りなので、抽選のエントリーや
    競りの入札履歴が「黙って全部消える」ことになる。
    """
    path = DATA_DIR / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
    tmp.replace(path)
