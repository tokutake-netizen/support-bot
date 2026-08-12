"""送料計算のカートに出す商品の登録。

これまで商品はリポジトリ同梱の data/products.json 固定で、変更するには
コードを編集するしかなかった。取り扱う商品は会社ごとに違うので、
サーバー単位で登録・編集できるようにする。

保存先: deployments/<guild_id>/data/products.json
ファイルが無ければ同梱の既定リストを使う（＝従来どおり）。
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Optional

from . import config_store

FILENAME = "products.json"
MAX_PRODUCTS = 25          # Discord のセレクトは25件が上限
MAX_WEIGHT_G = 100_000


def _default_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / FILENAME


def path_for(guild_id: str) -> Path:
    return config_store.deployment_dir(str(guild_id)) / "data" / FILENAME


def has_own(guild_id: str) -> bool:
    return path_for(guild_id).exists()


def load(guild_id: str) -> list[dict]:
    p = path_for(guild_id)
    if not p.exists():
        p = _default_path()
    try:
        data = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return []
    return list(data.get("products", []))


def save(guild_id: str, products: list[dict]) -> None:
    p = path_for(guild_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"version": "dashboard", "products": products},
                   ensure_ascii=False, indent=2) + "\n", "utf-8")
    tmp.replace(p)


def reset(guild_id: str) -> bool:
    p = path_for(guild_id)
    if not p.exists():
        return False
    p.unlink()
    return True


def make_id(name_en: str, name_ja: str, existing: list[dict]) -> str:
    """英語名から識別子を作る。取れなければ連番。"""
    s = unicodedata.normalize("NFKC", (name_en or "").strip().lower())
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")[:32]
    if not s:
        s = "item"
    used = {p.get("id") for p in existing}
    if s not in used:
        return s
    n = 2
    while f"{s}_{n}" in used:
        n += 1
    return f"{s}_{n}"


def validate(name_ja: str, name_en: str, weight_g: str, unit_ja: str = "",
             unit_en: str = "", emoji: str = "") -> dict:
    """フォーム入力を検証して1件ぶんの dict にする。"""
    name_ja = (name_ja or "").strip()
    name_en = (name_en or "").strip()
    if not name_ja:
        raise ValueError("商品名（日本語）を入力してください")
    if not name_en:
        raise ValueError("商品名（英語）を入力してください。海外のお客様にはこちらが表示されます")
    try:
        g = int(str(weight_g).replace(",", "").strip())
    except (TypeError, ValueError):
        raise ValueError("重量はグラム単位の数値で入力してください")
    if g <= 0 or g > MAX_WEIGHT_G:
        raise ValueError(f"重量は1〜{MAX_WEIGHT_G:,}gの範囲で入力してください")
    return {
        "name_ja": name_ja[:60],
        "name_en": name_en[:60],
        "emoji": (emoji or "").strip()[:8],
        "weight_g": g,
        "unit_ja": (unit_ja or "個").strip()[:8],
        "unit_en": (unit_en or "").strip()[:12],
    }


def add(guild_id: str, item: dict) -> dict:
    items = load(guild_id)
    if len(items) >= MAX_PRODUCTS:
        raise ValueError(
            f"商品は{MAX_PRODUCTS}件までです（Discordの選択メニューの上限）。"
            "使わない商品を削除してください"
        )
    item = {"id": make_id(item["name_en"], item["name_ja"], items), **item}
    items.append(item)
    save(guild_id, items)
    return item


def update(guild_id: str, pid: str, item: dict) -> bool:
    items = load(guild_id)
    for i, p in enumerate(items):
        if p.get("id") == pid:
            items[i] = {"id": pid, **item}
            save(guild_id, items)
            return True
    return False


def remove(guild_id: str, pid: str) -> bool:
    items = load(guild_id)
    kept = [p for p in items if p.get("id") != pid]
    if len(kept) == len(items):
        return False
    save(guild_id, kept)
    return True


def move(guild_id: str, pid: str, delta: int) -> bool:
    """並び順を変える。カートの選択メニューに出る順になる。"""
    items = load(guild_id)
    idx = next((i for i, p in enumerate(items) if p.get("id") == pid), None)
    if idx is None:
        return False
    j = max(0, min(len(items) - 1, idx + delta))
    if j == idx:
        return False
    items.insert(j, items.pop(idx))
    save(guild_id, items)
    return True
