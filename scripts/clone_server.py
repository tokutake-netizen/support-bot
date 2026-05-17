"""
clone_server.py — Discord サーバーのカテゴリ/チャンネル/ロール構造を別サーバーへ複製する。

使い方:
    python scripts/clone_server.py                 # 実行（複製）
    python scripts/clone_server.py --dry-run       # 何が作られるか表示のみ
    python scripts/clone_server.py --wipe-target   # 先に target を空にしてから複製（危険）

必要な .env (このスクリプトと同じ場所か親ディレクトリで読み込み)
    DISCORD_TOKEN_CLONE   ... 両方のサーバーに参加している bot のトークン
    SOURCE_GUILD_ID       ... コピー元サーバーID
    TARGET_GUILD_ID       ... コピー先サーバーID
    CLONE_ROLES=1         ... ロールも複製するか（既定 1）
    CLONE_ICON=1          ... サーバーアイコン／バナーも複製するか（既定 0）

クローン対象:
    - ロール（色／hoist／mentionable／permissions、@everyone はパーミッションのみ上書き）
    - カテゴリ
    - テキスト・ボイス・フォーラムチャンネル（topic, slowmode, nsfw, bitrate, user_limit）
    - チャンネル単位の権限オーバーライド（role / @everyone）
    - 任意: サーバーアイコン・バナー

クローンしないもの:
    - メッセージ / メンバー / メンバーのロール割り当て / Webhook / 連携 / スレッド本文
    - managed ロール（bot 自身のロール等、Discord 側で自動管理されるもの）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

import discord
from dotenv import load_dotenv


def _load_env() -> None:
    here = Path(__file__).resolve()
    # scripts/.env → support_bot/.env → repo root を順に探す
    for candidate in [here.parent / ".env", here.parent.parent / ".env", here.parent.parent.parent / ".env"]:
        if candidate.exists():
            load_dotenv(candidate, override=False)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _fmt_perms(overwrite: discord.PermissionOverwrite) -> str:
    allow, deny = overwrite.pair()
    return f"allow={allow.value} deny={deny.value}"


async def _clone_roles(
    source: discord.Guild,
    target: discord.Guild,
    role_map: dict[int, discord.Role],
    dry_run: bool,
) -> None:
    """source のロールを target に作成。@everyone は permissions のみ更新。"""
    # discord はロール一覧を position 降順で返すので、低い→高い順に作る
    source_roles = sorted(source.roles, key=lambda r: r.position)

    for src_role in source_roles:
        if src_role.is_default():
            # @everyone
            target_everyone = target.default_role
            role_map[src_role.id] = target_everyone
            if dry_run:
                print(f"  [dry] @everyone perms <- {src_role.permissions.value}")
            else:
                try:
                    await target_everyone.edit(permissions=src_role.permissions)
                    print(f"  ✓ @everyone perms updated")
                except discord.Forbidden:
                    print(f"  ! @everyone perms update forbidden (bot ロールを最上位に置いてください)")
            continue

        if src_role.managed:
            # bot 自身のロール等は複製不可
            print(f"  - skip managed role: {src_role.name}")
            continue

        if dry_run:
            print(f"  [dry] create role: {src_role.name} (color={src_role.color}, hoist={src_role.hoist})")
            continue

        try:
            new_role = await target.create_role(
                name=src_role.name,
                permissions=src_role.permissions,
                colour=src_role.colour,
                hoist=src_role.hoist,
                mentionable=src_role.mentionable,
                reason="clone_server.py",
            )
            role_map[src_role.id] = new_role
            print(f"  ✓ role: {src_role.name}")
        except discord.Forbidden:
            print(f"  ! forbidden creating role: {src_role.name}")
        except discord.HTTPException as e:
            print(f"  ! HTTP error creating role {src_role.name}: {e}")


def _translate_overwrites(
    source_overwrites: dict,
    role_map: dict[int, discord.Role],
    target: discord.Guild,
) -> dict:
    """source の overwrites を target 側の Role/Object に張り替え。Member はスキップ（メンバーは複製しないため）。"""
    result: dict = {}
    for entity, overwrite in source_overwrites.items():
        if isinstance(entity, discord.Role):
            mapped = role_map.get(entity.id)
            if mapped is None:
                # 複製できなかった managed role など → スキップ
                continue
            result[mapped] = overwrite
        elif isinstance(entity, discord.Member):
            # メンバー固有の overwrite は target に該当メンバーがいる保証がないのでスキップ
            continue
        else:
            continue
    return result


async def _clone_channels(
    source: discord.Guild,
    target: discord.Guild,
    role_map: dict[int, discord.Role],
    dry_run: bool,
) -> None:
    # まずカテゴリ
    cat_map: dict[int, discord.CategoryChannel] = {}
    for src_cat in sorted(source.categories, key=lambda c: c.position):
        overwrites = _translate_overwrites(src_cat.overwrites, role_map, target)
        if dry_run:
            print(f"  [dry] category: {src_cat.name} (overrides={len(overwrites)})")
            continue
        try:
            new_cat = await target.create_category(
                name=src_cat.name,
                overwrites=overwrites,
                reason="clone_server.py",
            )
            cat_map[src_cat.id] = new_cat
            print(f"  ✓ category: {src_cat.name}")
        except discord.HTTPException as e:
            print(f"  ! error creating category {src_cat.name}: {e}")

    # 次にそれ以外（カテゴリ無しを含む）。source の position 順を維持。
    others = [c for c in source.channels if not isinstance(c, discord.CategoryChannel)]
    others.sort(key=lambda c: (c.category.position if c.category else -1, c.position))

    for src_ch in others:
        parent = cat_map.get(src_ch.category.id) if src_ch.category else None
        overwrites = _translate_overwrites(src_ch.overwrites, role_map, target)

        kind = type(src_ch).__name__
        if dry_run:
            print(f"  [dry] {kind}: {src_ch.name} (under: {src_ch.category.name if src_ch.category else '-'})")
            continue

        try:
            if isinstance(src_ch, discord.TextChannel):
                await target.create_text_channel(
                    name=src_ch.name,
                    category=parent,
                    overwrites=overwrites,
                    topic=src_ch.topic,
                    slowmode_delay=src_ch.slowmode_delay,
                    nsfw=src_ch.nsfw,
                    reason="clone_server.py",
                )
            elif isinstance(src_ch, discord.VoiceChannel):
                await target.create_voice_channel(
                    name=src_ch.name,
                    category=parent,
                    overwrites=overwrites,
                    bitrate=src_ch.bitrate,
                    user_limit=src_ch.user_limit,
                    reason="clone_server.py",
                )
            elif isinstance(src_ch, discord.ForumChannel):
                await target.create_forum(
                    name=src_ch.name,
                    category=parent,
                    overwrites=overwrites,
                    topic=src_ch.topic,
                    reason="clone_server.py",
                )
            elif isinstance(src_ch, discord.StageChannel):
                await target.create_stage_channel(
                    name=src_ch.name,
                    category=parent,
                    overwrites=overwrites,
                    bitrate=src_ch.bitrate,
                    reason="clone_server.py",
                )
            else:
                print(f"  - skip unsupported type {kind}: {src_ch.name}")
                continue
            print(f"  ✓ {kind}: {src_ch.name}")
        except discord.Forbidden:
            print(f"  ! forbidden: {kind} {src_ch.name}")
        except discord.HTTPException as e:
            print(f"  ! HTTP error {kind} {src_ch.name}: {e}")


async def _wipe_target(target: discord.Guild, dry_run: bool) -> None:
    print(f"\n[wipe] deleting all channels and non-managed non-@everyone roles in '{target.name}'")
    for ch in list(target.channels):
        if dry_run:
            print(f"  [dry] delete channel: {ch.name}")
            continue
        try:
            await ch.delete(reason="clone_server.py wipe")
            print(f"  - deleted channel: {ch.name}")
        except discord.HTTPException as e:
            print(f"  ! delete channel {ch.name}: {e}")
    for role in list(target.roles):
        if role.is_default() or role.managed:
            continue
        if dry_run:
            print(f"  [dry] delete role: {role.name}")
            continue
        try:
            await role.delete(reason="clone_server.py wipe")
            print(f"  - deleted role: {role.name}")
        except discord.HTTPException as e:
            print(f"  ! delete role {role.name}: {e}")


async def _clone_assets(source: discord.Guild, target: discord.Guild, dry_run: bool) -> None:
    """サーバーアイコン・バナーをコピー。"""
    if source.icon:
        if dry_run:
            print("  [dry] copy icon")
        else:
            try:
                data = await source.icon.read()
                await target.edit(icon=data, reason="clone_server.py")
                print("  ✓ icon copied")
            except discord.HTTPException as e:
                print(f"  ! icon: {e}")
    if source.banner:
        if dry_run:
            print("  [dry] copy banner")
        else:
            try:
                data = await source.banner.read()
                await target.edit(banner=data, reason="clone_server.py")
                print("  ✓ banner copied")
            except discord.HTTPException as e:
                print(f"  ! banner: {e}")


async def run(dry_run: bool, wipe: bool) -> None:
    _load_env()
    token = os.getenv("DISCORD_TOKEN_CLONE") or os.getenv("DISCORD_TOKEN_SUPPORT")
    source_id = os.getenv("SOURCE_GUILD_ID")
    target_id = os.getenv("TARGET_GUILD_ID")

    if not token or not source_id or not target_id:
        print("ERROR: DISCORD_TOKEN_CLONE / SOURCE_GUILD_ID / TARGET_GUILD_ID を .env に設定してください")
        sys.exit(1)

    intents = discord.Intents.default()
    intents.guilds = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        try:
            source = client.get_guild(int(source_id)) or await client.fetch_guild(int(source_id))
            target = client.get_guild(int(target_id)) or await client.fetch_guild(int(target_id))
            if source is None or target is None:
                print("ERROR: bot が source/target サーバーに参加していません")
                await client.close()
                return

            # fetch_guild は channels/roles を持たないことがあるので get_guild を優先し、無ければ取り直す
            if not source.channels:
                source = await client.fetch_guild(int(source_id), with_counts=False)
            if not target.channels:
                target = await client.fetch_guild(int(target_id), with_counts=False)

            print(f"\n=== Clone Plan ===")
            print(f"source: {source.name} ({source.id})")
            print(f"target: {target.name} ({target.id})")
            print(f"dry_run={dry_run} wipe={wipe}")

            if wipe:
                await _wipe_target(target, dry_run)

            role_map: dict[int, discord.Role] = {}
            if _env_flag("CLONE_ROLES", True):
                print("\n[1/3] Cloning roles ...")
                await _clone_roles(source, target, role_map, dry_run)
            else:
                # ロールを複製しない場合、名前で照合
                for src_role in source.roles:
                    if src_role.is_default():
                        role_map[src_role.id] = target.default_role
                        continue
                    match = discord.utils.get(target.roles, name=src_role.name)
                    if match:
                        role_map[src_role.id] = match

            print("\n[2/3] Cloning categories and channels ...")
            await _clone_channels(source, target, role_map, dry_run)

            if _env_flag("CLONE_ICON", False):
                print("\n[3/3] Cloning icon/banner ...")
                await _clone_assets(source, target, dry_run)
            else:
                print("\n[3/3] icon/banner skipped (CLONE_ICON=0)")

            print("\n✓ done")
        finally:
            await client.close()

    await client.start(token)


def main() -> None:
    parser = argparse.ArgumentParser(description="Clone Discord server structure")
    parser.add_argument("--dry-run", action="store_true", help="変更を加えず、プランだけ表示")
    parser.add_argument("--wipe-target", action="store_true", help="複製前に target のチャンネル/ロールを全削除（危険）")
    args = parser.parse_args()

    if args.wipe_target and not args.dry_run:
        ans = input("⚠️  target サーバーの既存チャンネル/ロールを全削除します。続行しますか? [yes/NO]: ")
        if ans.strip().lower() != "yes":
            print("中止しました。")
            return

    asyncio.run(run(dry_run=args.dry_run, wipe=args.wipe_target))


if __name__ == "__main__":
    main()
