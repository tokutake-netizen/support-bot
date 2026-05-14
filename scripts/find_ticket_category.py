"""Find the 'Created Tickets' category in the configured guild and update .env."""
import asyncio
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import discord
from dotenv import load_dotenv

ENV_PATH = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)


async def main() -> None:
    token = os.getenv("DISCORD_TOKEN_SUPPORT")
    guild_id = int(os.getenv("GUILD_ID", "0"))
    if not token or not guild_id:
        print("missing DISCORD_TOKEN_SUPPORT or GUILD_ID")
        return

    intents = discord.Intents.default()
    intents.guilds = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(guild_id) or await client.fetch_guild(guild_id)
            cats = [c for c in guild.channels if isinstance(c, discord.CategoryChannel)]
            print(f"\n=== Categories in '{guild.name}' ({guild.id}) ===")
            for c in cats:
                print(f"  {c.id}  : {c.name}")
            # Try to find a 'Created Tickets' category
            target = None
            for c in cats:
                low = c.name.lower()
                if "created" in low and "ticket" in low:
                    target = c
                    break
            if target is None:
                for c in cats:
                    if "ticket" in c.name.lower():
                        target = c
                        break
            if target:
                print(f"\n>>> Selected: {target.name} (id={target.id})")
                # Update .env
                content = ENV_PATH.read_text("utf-8")
                content = re.sub(
                    r"^TICKET_CATEGORY_ID=.*$",
                    f"TICKET_CATEGORY_ID={target.id}",
                    content, flags=re.M,
                )
                ENV_PATH.write_text(content, "utf-8")
                print(f">>> Wrote TICKET_CATEGORY_ID={target.id} to .env")
            else:
                print("\n!! No 'Created Tickets' / 'ticket' category found. Please create one in Discord first.")
        finally:
            await client.close()

    await client.start(token)


if __name__ == "__main__":
    asyncio.run(main())
