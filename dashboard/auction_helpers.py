"""Auction creation helpers used by dashboard/app.py.

Mirrors enough of cogs/auction.py to:
  - parse_duration (same format as the cog)
  - build the live auction embed dict shape
  - build the bid + history button components

The running bot's AuctionView persistent view (custom_ids "auction:bid"
and "auction:history") catches clicks on any message posted with these
components.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .config_store import deployment_dir

AUCTIONS_FILE = "auctions.json"
DURATION_RE = re.compile(r"^\s*(?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?\s*$", re.I)
BID_INCREMENT_PCT = 0.05


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


def _fmt_money(amount: int) -> str:
    return f"¥{amount:,}"


def _current_price(auction: dict) -> int:
    """Mirror cogs/auction.py _current_price (no bids yet → starting_bid)."""
    bids = auction.get("bids", [])
    if not bids:
        return int(auction["starting_bid"])
    high = max(int(b["amount"]) for b in bids)
    floor_inc = high + int(auction.get("min_increment", 100))
    pct_inc = math.ceil(high * (1.0 + BID_INCREMENT_PCT))
    return max(floor_inc, pct_inc)


def build_embed(auction: dict) -> dict:
    """Live auction embed (no bids yet) as a REST API embed dict."""
    ends_at = datetime.fromisoformat(auction["ends_at"])
    ts = int(ends_at.timestamp())
    fields = [
        {"name": "💰 Starting bid", "value": _fmt_money(int(auction["starting_bid"])), "inline": True},
        {"name": "👥 Bidders", "value": "0", "inline": True},
        {"name": "📈 Next bid >=", "value": _fmt_money(_current_price(auction)), "inline": True},
        {"name": "⏱ Ends", "value": f"<t:{ts}:R> (<t:{ts}:F>)", "inline": False},
    ]
    reserve = int(auction.get("reserve_price", 0) or 0)
    if reserve:
        fields.append({"name": "🔒 Reserve", "value": _fmt_money(reserve), "inline": True})
    fields.append({"name": "🎯 Host", "value": f"<@{auction['host_id']}>", "inline": True})

    embed: dict = {
        "title": f"🔨 AUCTION: {auction['title']}",
        "color": 0xF0B232,
        "fields": fields,
    }
    if auction.get("description"):
        embed["description"] = auction["description"]
    if auction.get("image_filename"):
        embed["image"] = {"url": f"attachment://{auction['image_filename']}"}
    elif auction.get("image_url"):
        embed["image"] = {"url": auction["image_url"]}
    return embed


def view_components() -> list:
    """Buttons matching the bot's AuctionView persistent view."""
    return [{
        "type": 1,
        "components": [
            {"type": 2, "style": 1, "label": "💰 Place Bid", "custom_id": "auction:bid"},
            {"type": 2, "style": 2, "label": "📜 Bid history", "custom_id": "auction:history"},
        ],
    }]


def _auctions_path(guild_id: str) -> Path:
    return deployment_dir(guild_id) / "data" / AUCTIONS_FILE


def load_auctions(guild_id: str) -> dict:
    p = _auctions_path(guild_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text("utf-8"))
    except json.JSONDecodeError:
        return {}


def save_auctions(guild_id: str, data: dict) -> None:
    p = _auctions_path(guild_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")


def add_auction(guild_id: str, message_id: str, auction: dict) -> None:
    data = load_auctions(guild_id)
    data[str(message_id)] = auction
    save_auctions(guild_id, data)


def future_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
