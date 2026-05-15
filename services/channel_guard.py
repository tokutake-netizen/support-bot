"""Channel allowlist guard for slash commands.

Env convention (single source of truth, no need to edit code to retune):
- ALLOW_CH_<KEY>=id1,id2  →  command only allowed in those channel IDs
- ALLOW_CAT_<KEY>=id1,id2 →  command also allowed under those category IDs
  (handy for /shipping inside any ticket-category channel)

Unset keys = no restriction (backwards-compatible). When the channel is
disallowed, an ephemeral message naming the permitted channels is sent.
"""
from __future__ import annotations

import os
from typing import Iterable

import discord


def _parse_ids(env_name: str) -> list[int]:
    out: list[int] = []
    for tok in (os.getenv(env_name) or "").split(","):
        t = tok.strip()
        if t.isdigit():
            out.append(int(t))
    return out


def _format_mentions(ch_ids: Iterable[int], cat_ids: Iterable[int]) -> str:
    parts = [f"<#{i}>" for i in ch_ids]
    parts += [f"<#{i}> (category)" for i in cat_ids]
    return ", ".join(parts) if parts else "(no channels configured)"


async def ensure_channel_allowed(interaction: discord.Interaction, key: str) -> bool:
    """Check ALLOW_CH_<KEY> / ALLOW_CAT_<KEY> against interaction.channel.

    Returns True if allowed (or no rule set). If blocked, sends an ephemeral
    notice listing the permitted channels and returns False — caller should
    short-circuit.
    """
    ch_ids = _parse_ids(f"ALLOW_CH_{key.upper()}")
    cat_ids = _parse_ids(f"ALLOW_CAT_{key.upper()}")
    if not ch_ids and not cat_ids:
        return True

    channel = interaction.channel
    if channel is None:
        return True  # don't block DMs etc.

    if isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.Thread)):
        if channel.id in ch_ids:
            return True
        cat_id = getattr(channel, "category_id", None)
        if cat_id is not None and cat_id in cat_ids:
            return True
    else:
        # Unknown channel type (e.g. DM) — don't block.
        return True

    msg = f"⚠️ `/{key.lower()}` is only available in: {_format_mentions(ch_ids, cat_ids)}"
    try:
        await interaction.response.send_message(msg, ephemeral=True)
    except discord.InteractionResponded:
        await interaction.followup.send(msg, ephemeral=True)
    return False
