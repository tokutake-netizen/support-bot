"""Anthropic Claude API thin wrapper with prompt caching and usage tracking."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from anthropic import AsyncAnthropic

log = logging.getLogger(__name__)

USAGE_PATH = Path("data/usage.json")
USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)

# Anthropic published pricing (USD per million tokens). Update if pricing changes.
PRICING = {
    "claude-haiku-4-5":  {"input": 1.0,  "cached": 0.10, "output": 5.0},
    "claude-sonnet-4-6": {"input": 3.0,  "cached": 0.30, "output": 15.0},
    "claude-opus-4-7":   {"input": 15.0, "cached": 1.50, "output": 75.0},
}


@dataclass
class ClaudeReply:
    text: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    cost_usd: float


class ClaudeClient:
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None) -> None:
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.model = model or os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self._client = AsyncAnthropic(api_key=self.api_key)

    async def chat(
        self,
        *,
        system: str | list[dict[str, Any]],
        user: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> ClaudeReply:
        """Send a single-turn chat. `system` may be a string OR a list with cache_control."""
        if isinstance(system, str):
            system_param: list[dict[str, Any]] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
        else:
            system_param = system

        response = await self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_param,
            messages=[{"role": "user", "content": user}],
        )

        text_chunks = [b.text for b in response.content if getattr(b, "type", None) == "text"]
        text = "".join(text_chunks).strip()

        usage = response.usage
        input_t = getattr(usage, "input_tokens", 0)
        cached_t = getattr(usage, "cache_read_input_tokens", 0) or 0
        output_t = getattr(usage, "output_tokens", 0)
        cost = self._estimate_cost(input_t, cached_t, output_t)

        self._record_usage(input_t, cached_t, output_t, cost)
        return ClaudeReply(text, input_t, cached_t, output_t, cost)

    def _estimate_cost(self, input_t: int, cached_t: int, output_t: int) -> float:
        rates = PRICING.get(self.model, PRICING["claude-haiku-4-5"])
        billed_input = max(0, input_t - cached_t)
        return (
            billed_input * rates["input"]
            + cached_t * rates["cached"]
            + output_t * rates["output"]
        ) / 1_000_000

    def _record_usage(self, input_t: int, cached_t: int, output_t: int, cost: float) -> None:
        try:
            month = datetime.now(timezone.utc).strftime("%Y-%m")
            data = {}
            if USAGE_PATH.exists():
                data = json.loads(USAGE_PATH.read_text("utf-8"))
            bucket = data.setdefault(month, {"input": 0, "cached": 0, "output": 0, "cost_usd": 0.0})
            bucket["input"] += input_t
            bucket["cached"] += cached_t
            bucket["output"] += output_t
            bucket["cost_usd"] = round(bucket["cost_usd"] + cost, 6)
            USAGE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
        except Exception:
            log.exception("usage record failed")

    @staticmethod
    def monthly_usd_used() -> float:
        if not USAGE_PATH.exists():
            return 0.0
        try:
            data = json.loads(USAGE_PATH.read_text("utf-8"))
            month = datetime.now(timezone.utc).strftime("%Y-%m")
            return float(data.get(month, {}).get("cost_usd", 0.0))
        except Exception:
            return 0.0
