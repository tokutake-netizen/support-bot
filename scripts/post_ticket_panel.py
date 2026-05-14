"""One-off script: post the ticket panel embed+button to PRODUCT_INQUIRY_CHANNEL_ID."""
import asyncio
import os
import sys
from pathlib import Path

# allow imports from parent
sys.path.insert(0, str(Path(__file__).parent.parent))

import discord
from dotenv import load_dotenv

from cogs.ticket import TicketOpenView

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")


async def main() -> None:
    token = os.getenv("DISCORD_TOKEN_SUPPORT")
    channel_id = int(os.getenv("PRODUCT_INQUIRY_CHANNEL_ID", "0"))
    if not token or not channel_id:
        print("missing DISCORD_TOKEN_SUPPORT or PRODUCT_INQUIRY_CHANNEL_ID")
        return

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            ch = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
            embed = discord.Embed(
                title="🎫 サポートチケット / Support Ticket",
                description=(
                    "下のボタンを押すと、あなた専用の個別チャット（プライベートスレッド）が"
                    "作成されます。商品の質問・配送相談などお気軽にどうぞ。\n\n"
                    "Press the button below to open a **private thread** with our staff. "
                    "Use it for product questions, shipping inquiries, or anything else.\n\n"
                    "—\n"
                    "・💰 商品の在庫・価格 / Product stock & price\n"
                    "・📦 配送・送料 / Shipping inquiries\n"
                    "・❓ その他の質問 / Other questions"
                ),
                color=0x5865F2,
            )
            embed.set_footer(text="運営が個別に対応します / Staff will reply individually")
            msg = await ch.send(embed=embed, view=TicketOpenView())
            print(f"posted panel: message_id={msg.id} channel={ch.name}")
        except Exception as e:
            print(f"failed: {e}")
        finally:
            await client.close()

    await client.start(token)


if __name__ == "__main__":
    asyncio.run(main())
