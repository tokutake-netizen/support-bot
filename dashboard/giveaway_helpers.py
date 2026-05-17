"""Duration parsing + giveaways.json read/write for the dashboard.

We duplicate the parse/format logic from cogs/giveaway.py rather than
importing the cog (the cog brings in discord.py and the whole bot stack,
which we don't want loaded inside the dashboard process). The two
implementations are intentionally tiny and easy to keep in sync.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .config_store import deployment_dir

DURATION_RE = re.compile(r"^\s*(?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?\s*$", re.I)


def parse_duration(text: str) -> Optional[int]:
    text = (text or "").strip().lower()
    if not text:
        return None
    m = DURATION_RE.match(text)
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    total = d * 86400 + h * 3600 + mi * 60 + s
    return total if total > 0 else None


def fmt_duration(secs: int) -> str:
    if secs <= 0:
        return "0s"
    parts = []
    for unit, label in ((86400, "d"), (3600, "h"), (60, "m"), (1, "s")):
        if secs >= unit:
            parts.append(f"{secs // unit}{label}")
            secs %= unit
    return " ".join(parts)


def _giveaways_path(guild_id: str) -> Path:
    return deployment_dir(guild_id) / "data" / "giveaways.json"


def load_giveaways(guild_id: str) -> dict[str, dict]:
    p = _giveaways_path(guild_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text("utf-8"))
    except json.JSONDecodeError:
        return {}


def save_giveaways(guild_id: str, data: dict[str, dict]) -> None:
    p = _giveaways_path(guild_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")


def add_giveaway(guild_id: str, message_id: str, gw: dict) -> None:
    data = load_giveaways(guild_id)
    data[str(message_id)] = gw
    save_giveaways(guild_id, data)


def build_giveaway_embed(gw: dict, ended: bool = False) -> dict:
    """Return the embed payload that mirrors cogs/giveaway.py _build_embed.

    The running bot rewrites this embed on end / reroll, so any divergence
    is short-lived. The shapes still need to line up so the running bot
    can re-render correctly.
    """
    ends_at = datetime.fromisoformat(gw["ends_at"])
    ts = int(ends_at.timestamp())
    title = f"🎉 GIVEAWAY: {gw['prize']}"
    if ended:
        title = f"🏁 [Ended] {title}"
    color = 0x57F287 if not ended else 0x4E5058
    desc_lines = [
        f"**🎁 Prize**: {gw['prize']}",
        f"**👥 Winners**: {gw['winner_count']}",
        f"**⏱ Ends**: <t:{ts}:R> (<t:{ts}:F>)",
        f"**🎯 Host**: <@{gw['host_id']}>",
        f"**👤 Entries**: {len(gw.get('entries', []))}",
    ]
    if gw.get("required_role_id"):
        desc_lines.append(f"**🔒 Required role**: <@&{gw['required_role_id']}>")
    if ended:
        winners = gw.get("winners", [])
        if winners:
            desc_lines.append("")
            desc_lines.append(
                "**🏆 Winners**: " + ", ".join(f"<@{w}>" for w in winners)
            )
        else:
            desc_lines.append("\n_No entries._")
    else:
        desc_lines.append("\nClick the button below to enter!")
    embed = {
        "title": title,
        "description": "\n".join(desc_lines),
        "color": color,
    }
    if gw.get("image_url"):
        embed["image"] = {"url": gw["image_url"]}
    if gw.get("note"):
        embed["footer"] = {"text": gw["note"]}
    return embed


def enter_button_component(disabled: bool = False) -> dict:
    return {
        "type": 1,
        "components": [
            {
                "type": 2,
                "style": 3,  # success/green
                "label": "🎉 Enter" if not disabled else "🏁 Ended",
                "custom_id": "giveaway:enter" if not disabled else "giveaway:ended",
                "disabled": disabled,
            }
        ],
    }


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def future_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
