"""Lightweight i18n loader. Loads i18n/{lang}.json into memory and provides t()."""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

I18N_DIR = Path(__file__).parent.parent / "i18n"

DEFAULT_LANG = "en"
SUPPORTED = {"ja", "en"}


def normalize_locale(locale: Optional[str]) -> str:
    if not locale:
        return DEFAULT_LANG
    locale = locale.lower()
    if locale.startswith("ja"):
        return "ja"
    return "en"


@lru_cache(maxsize=4)
def _load(lang: str) -> dict[str, Any]:
    path = I18N_DIR / f"{lang}.json"
    if not path.exists():
        log.warning("i18n file missing: %s", path)
        return {}
    return json.loads(path.read_text("utf-8"))


def t(key: str, lang: str = DEFAULT_LANG, **fmt: Any) -> str:
    """Get localized string by dot-separated key."""
    lang = normalize_locale(lang)
    data = _load(lang)
    node: Any = data
    for part in key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            # fallback to default lang
            if lang != DEFAULT_LANG:
                return t(key, DEFAULT_LANG, **fmt)
            return key  # last resort: return key
    if isinstance(node, str) and fmt:
        try:
            return node.format(**fmt)
        except (KeyError, IndexError):
            return node
    return str(node)
