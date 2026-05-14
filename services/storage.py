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
    path = DATA_DIR / name
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
