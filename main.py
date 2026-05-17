import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

# ---------- multi-deployment: locate config dir ----------
parser = argparse.ArgumentParser(description="Discord support bot")
parser.add_argument(
    "--env-dir",
    default=os.environ.get("BOT_ENV_DIR", "."),
    help="Directory holding .env, data/, credentials/. Defaults to CWD.",
)
_args, _ = parser.parse_known_args()

_env_dir = Path(_args.env_dir).resolve()
if not _env_dir.exists():
    print(f"env-dir does not exist: {_env_dir}", file=sys.stderr)
    sys.exit(1)
os.chdir(_env_dir)  # so relative paths (data/, credentials/) resolve here
load_dotenv(dotenv_path=_env_dir / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("support_bot.log"),
    ],
)
log = logging.getLogger("support_bot")
log.info("env_dir=%s", _env_dir)

# Make sure cogs/ and services/ are importable from the original source dir
_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
intents.members = True  # for on_member_join (privileged)

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (id=%s)", bot.user, bot.user.id)
    log.info("Guilds: %s", [g.name for g in bot.guilds])
    try:
        guild_id = os.getenv("GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
        else:
            synced = await bot.tree.sync()
        log.info("Synced %d slash command(s)", len(synced))
    except Exception:
        log.exception("Slash command sync failed")


async def main() -> None:
    async with bot:
        for ext in (
            "cogs.translator", "cogs.suggester", "cogs.shipping",
            "cogs.ticket", "cogs.welcome", "cogs.giveaway",
            "cogs.invite_tracker", "cogs.digest", "cogs.backup",
            "cogs.health", "cogs.help",
        ):
            try:
                await bot.load_extension(ext)
                log.info("Loaded extension: %s", ext)
            except Exception:
                log.exception("Failed loading %s", ext)
        token = os.getenv("DISCORD_TOKEN_SUPPORT")
        if not token:
            log.error("DISCORD_TOKEN_SUPPORT not set in .env")
            return
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
