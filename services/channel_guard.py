"""Channel allowlist guard for slash commands.

Env convention (single source of truth, no need to edit code to retune):
- ALLOW_CH_<KEY>=id1,id2  →  command only allowed in those channel IDs
- ALLOW_CAT_<KEY>=id1,id2 →  command also allowed under those category IDs
  (handy for /shipping inside any ticket-category channel)

Unset keys = no restriction (backwards-compatible). When the channel is
disallowed, an ephemeral message naming the permitted channels is sent.
"""
from __future__ import annotations

import logging
import os
from typing import Iterable

import discord

log = logging.getLogger(__name__)


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


async def _safe_send(interaction: discord.Interaction, msg: str) -> None:
    """Best-effort ephemeral reply that survives ack races.

    Discord sometimes redelivers interaction events; the first delivery may
    have already acknowledged the interaction by the time our second
    response.send_message attempt fires. Fall back through every available
    channel (followup, raw HTTP) and swallow the last-resort failure.
    """
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
            return
    except (discord.InteractionResponded, discord.HTTPException) as e:
        log.debug("channel_guard: response.send_message failed (%s); falling back to followup", e)
    try:
        await interaction.followup.send(msg, ephemeral=True)
    except discord.HTTPException as e:
        log.warning("channel_guard: could not deliver block notice to user (%s)", e)


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

    log.info(
        "channel_guard blocked /%s in channel_id=%s category_id=%s "
        "(allowed ch_ids=%s cat_ids=%s)",
        key.lower(),
        getattr(channel, "id", None),
        getattr(channel, "category_id", None),
        ch_ids,
        cat_ids,
    )
    msg = f"⚠️ `/{key.lower()}` is only available in: {_format_mentions(ch_ids, cat_ids)}"
    await _safe_send(interaction, msg)
    return False
