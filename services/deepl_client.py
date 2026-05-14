"""DeepL translator wrapper with monthly usage tracking."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import deepl

log = logging.getLogger(__name__)

DEEPL_USAGE_PATH = Path("data/deepl_usage.json")
DEEPL_USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)


@dataclass
class DeeplReply:
    text: str
    source_lang: str
    target_lang: str
    char_count: int


class DeeplClient:
    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or os.getenv("DEEPL_API_KEY", "").strip()
        if not self.api_key:
            raise RuntimeError("DEEPL_API_KEY is not set")
        self._t = deepl.Translator(self.api_key)

    def is_free_tier(self) -> bool:
        # Free keys end with ":fx"
        return self.api_key.endswith(":fx")

    async def translate(self, text: str, target_lang: str, source_lang: Optional[str] = None) -> DeeplReply:
        """target_lang: 'JA' or 'EN-US'. source_lang: 'EN', 'JA', or None for auto-detect."""
        # Normalize target to DeepL's expected codes
        tgt = target_lang.upper()
        if tgt == "EN":
            tgt = "EN-US"

        # deepl-python is sync, run in executor to avoid blocking
        import asyncio
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self._t.translate_text(text, target_lang=tgt, source_lang=source_lang)
        )

        if isinstance(result, list):
            translated = "".join(r.text for r in result)
            detected = result[0].detected_source_lang if result else (source_lang or "")
        else:
            translated = result.text
            detected = result.detected_source_lang

        chars = len(text)
        self._record(chars)
        return DeeplReply(
            text=translated,
            source_lang=detected,
            target_lang=tgt,
            char_count=chars,
        )

    def _record(self, chars: int) -> None:
        try:
            month = datetime.now(timezone.utc).strftime("%Y-%m")
            data = {}
            if DEEPL_USAGE_PATH.exists():
                data = json.loads(DEEPL_USAGE_PATH.read_text("utf-8"))
            bucket = data.setdefault(month, {"chars": 0, "requests": 0})
            bucket["chars"] += chars
            bucket["requests"] += 1
            DEEPL_USAGE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
        except Exception:
            log.exception("DeepL usage record failed")

    @staticmethod
    def monthly_chars_used() -> int:
        if not DEEPL_USAGE_PATH.exists():
            return 0
        try:
            data = json.loads(DEEPL_USAGE_PATH.read_text("utf-8"))
            month = datetime.now(timezone.utc).strftime("%Y-%m")
            return int(data.get(month, {}).get("chars", 0))
        except Exception:
            return 0

    def get_remote_usage(self) -> dict:
        """Fetch live usage from DeepL servers."""
        try:
            u = self._t.get_usage()
            return {
                "character_count": u.character.count if u.character else 0,
                "character_limit": u.character.limit if u.character else 0,
            }
        except Exception:
            log.exception("get_usage failed")
            return {"character_count": 0, "character_limit": 0}
