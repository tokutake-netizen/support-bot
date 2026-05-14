"""Find moderator and admin roles in the configured guild and update .env."""
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
            roles = await guild.fetch_roles()
            print(f"\n=== Roles in '{guild.name}' ({guild.id}) ===")
            for r in sorted(roles, key=lambda x: -x.position):
                marks = []
                if r.permissions.administrator:
                    marks.append("ADMIN")
                if r.permissions.manage_messages or r.permissions.kick_members:
                    marks.append("MOD")
                if r.is_default():
                    marks.append("@everyone")
                tag = f" [{', '.join(marks)}]" if marks else ""
                print(f"  {r.id}  pos={r.position:3d}  : {r.name}{tag}")

            # Heuristic: pick admin and mod roles
            picked: list[discord.Role] = []
            for r in roles:
                low = r.name.lower()
                if any(k in low for k in ("admin", "管理", "owner", "オーナー")) and not r.is_default():
                    picked.append(r)
                    continue
                if any(k in low for k in ("moderator", "mod", "モデレ", "運営", "staff", "スタッフ")) and not r.is_default():
                    picked.append(r)
                    continue
                # Also include any role with administrator permission
                if r.permissions.administrator and not r.is_default() and not r.is_bot_managed():
                    picked.append(r)

            unique = list({r.id: r for r in picked}.values())
            if unique:
                ids = ",".join(str(r.id) for r in unique)
                print(f"\n>>> Selected: {[r.name for r in unique]}")
                print(f">>> IDs: {ids}")
                content = ENV_PATH.read_text("utf-8")
                if re.search(r"^TICKET_STAFF_ROLE_IDS=", content, flags=re.M):
                    content = re.sub(r"^TICKET_STAFF_ROLE_IDS=.*$", f"TICKET_STAFF_ROLE_IDS={ids}", content, flags=re.M)
                else:
                    content = re.sub(r"^TICKET_STAFF_ROLE_ID=.*$", f"TICKET_STAFF_ROLE_IDS={ids}", content, flags=re.M)
                ENV_PATH.write_text(content, "utf-8")
                print(f">>> Wrote TICKET_STAFF_ROLE_IDS={ids} to .env")
            else:
                print("\n!! No moderator/admin roles found by heuristic.")
        finally:
            await client.close()

    await client.start(token)


if __name__ == "__main__":
    asyncio.run(main())
