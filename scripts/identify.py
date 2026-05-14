"""Identify what kind of object an ID refers to in the configured guild."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import discord
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

TARGET_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 0


async def main() -> None:
    token = os.getenv("DISCORD_TOKEN_SUPPORT")
    guild_id = int(os.getenv("GUILD_ID", "0"))
    if not token or not guild_id or not TARGET_ID:
        print("usage: python3 scripts/identify.py <ID>")
        return
    intents = discord.Intents.default()
    intents.guilds = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(guild_id) or await client.fetch_guild(guild_id)
            ch = guild.get_channel(TARGET_ID)
            if ch:
                kind = type(ch).__name__
                cat = ch.category.name if getattr(ch, "category", None) else "(none)"
                print(f"Channel: {kind} | name='{ch.name}' | category='{cat}'")
                return
            roles = await guild.fetch_roles()
            for r in roles:
                if r.id == TARGET_ID:
                    print(f"Role: '{r.name}' (perms admin={r.permissions.administrator})")
                    return
            members_chunk = await guild.fetch_member(TARGET_ID) if False else None
            try:
                m = await guild.fetch_member(TARGET_ID)
                print(f"Member: {m.name}")
                return
            except Exception:
                pass
            print("Not found in channels/roles/members")
        finally:
            await client.close()

    await client.start(token)

if __name__ == "__main__":
    asyncio.run(main())
