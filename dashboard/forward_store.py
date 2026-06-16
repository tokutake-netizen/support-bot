"""画像転送ルールの読み書き（転送Botと共有する forward_rules.json）。

転送Bot（リポジトリ直下の Discordbot/cogs/forward_config.py）と同じ JSON を
読み書きする。Bot 側は mtime を監視して自動リロードするため、ここで保存すれば
Bot を再起動せずに反映される。

ファイルの場所:
  環境変数 FORWARD_RULES_PATH があればそれを使う。
  無ければ Discordbot/data/forward_rules.json（このファイルから見て ../../data）。

転送先チャンネルの選択を「サーバー名 ＞ #チャンネル名」で分かりやすく見せるため、
Bot トークンで参加サーバーとチャンネル一覧を取得するヘルパも置く。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from .discord_api import DiscordREST, channels_grouped


# 本番では転送Bot（別Railwayプロジェクト）と Postgres を共有する。
# DATABASE_URL があれば Postgres、無ければローカル JSON にフォールバック。
DATABASE_URL = os.environ.get("DATABASE_URL")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS forward_rules (
    source BIGINT NOT NULL,
    dest   BIGINT NOT NULL,
    PRIMARY KEY (source, dest)
)
"""
# 転送Bot側 rules_store と同じ後方互換マイグレーション（共有DB）。
_MIGRATE_SQL = (
    "ALTER TABLE forward_rules ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'original'",
    "ALTER TABLE forward_rules ADD COLUMN IF NOT EXISTS role_ids TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE forward_rules ADD COLUMN IF NOT EXISTS template TEXT NOT NULL DEFAULT ''",
)
VALID_MODES = ("original", "image_only", "decorated", "custom")
_table_ready = False


def using_db() -> bool:
    return bool(DATABASE_URL)


def _pg_connect():
    import psycopg
    return psycopg.connect(DATABASE_URL, connect_timeout=10)


def _ensure_table(cur) -> None:
    global _table_ready
    if not _table_ready:
        cur.execute(_CREATE_SQL)
        for stmt in _MIGRATE_SQL:
            cur.execute(stmt)
        _table_ready = True


def _csv(role_ids) -> str:
    return ",".join(str(int(x)) for x in (role_ids or []))


def _parse_roles(raw) -> list:
    if not raw:
        return []
    if isinstance(raw, list):
        return [int(x) for x in raw]
    return [int(x) for x in str(raw).split(",") if str(x).strip()]


def _norm_mode(mode) -> str:
    return mode if mode in VALID_MODES else "original"


def rules_path() -> Path:
    env = os.environ.get("FORWARD_RULES_PATH")
    if env:
        return Path(env)
    # dashboard/ -> support_bot/ -> Discordbot/ の直下の data/
    discordbot_root = Path(__file__).resolve().parent.parent.parent
    return discordbot_root / "data" / "forward_rules.json"


def load_rules() -> list[dict]:
    if using_db():
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                _ensure_table(cur)
                cur.execute(
                    "SELECT source, dest, mode, role_ids, template FROM forward_rules "
                    "ORDER BY source, dest"
                )
                rows = cur.fetchall()
            conn.commit()
        return [
            {"source": r[0], "dest": r[1], "mode": _norm_mode(r[2]),
             "role_ids": _parse_roles(r[3]), "template": r[4] or ""}
            for r in rows
        ]
    p = rules_path()
    if not p.exists():
        return []
    try:
        rules = json.loads(p.read_text("utf-8")).get("rules", [])
    except Exception:
        return []
    for r in rules:
        r["mode"] = _norm_mode(r.get("mode"))
        r["role_ids"] = _parse_roles(r.get("role_ids"))
        r["template"] = r.get("template") or ""
    return rules


def save_rules(rules: list[dict]) -> None:
    p = rules_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"rules": rules}, ensure_ascii=False, indent=2), "utf-8")


def add_rule(source: int, dest: int, mode: str = "original", role_ids=None, template: str = "") -> bool:
    """重複していなければ追加。追加したら True。"""
    mode = _norm_mode(mode)
    roles_csv = _csv(role_ids)
    template = template or ""
    if using_db():
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                _ensure_table(cur)
                cur.execute(
                    "INSERT INTO forward_rules (source, dest, mode, role_ids, template) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (source, dest, mode, roles_csv, template),
                )
                added = cur.rowcount > 0
            conn.commit()
        return added
    rules = load_rules()
    if any(r.get("source") == source and r.get("dest") == dest for r in rules):
        return False
    rules.append({"source": source, "dest": dest, "mode": mode,
                  "role_ids": _parse_roles(role_ids), "template": template})
    save_rules(rules)
    return True


def update_rule(source: int, dest: int, mode: str = "original", role_ids=None, template: str = "") -> bool:
    """既存ルール (source,dest) の mode / role_ids / template を更新。更新したら True。"""
    mode = _norm_mode(mode)
    roles_csv = _csv(role_ids)
    template = template or ""
    if using_db():
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                _ensure_table(cur)
                cur.execute(
                    "UPDATE forward_rules SET mode=%s, role_ids=%s, template=%s "
                    "WHERE source=%s AND dest=%s",
                    (mode, roles_csv, template, source, dest),
                )
                updated = cur.rowcount > 0
            conn.commit()
        return updated
    rules = load_rules()
    found = False
    for r in rules:
        if r.get("source") == source and r.get("dest") == dest:
            r["mode"] = mode
            r["role_ids"] = _parse_roles(role_ids)
            r["template"] = template
            found = True
    if found:
        save_rules(rules)
    return found


def remove_rule(source: int, dest: int) -> bool:
    if using_db():
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                _ensure_table(cur)
                cur.execute(
                    "DELETE FROM forward_rules WHERE source=%s AND dest=%s",
                    (source, dest),
                )
                removed = cur.rowcount > 0
            conn.commit()
        return removed
    rules = load_rules()
    new = [r for r in rules if not (r.get("source") == source and r.get("dest") == dest)]
    if len(new) == len(rules):
        return False
    save_rules(new)
    return True


def forward_bot_token() -> Optional[str]:
    """転送Bot（両サーバーに参加している Bot）のトークンを取得する。

    優先順: 環境変数 FORWARD_BOT_TOKEN > Discordbot/.env の DISCORD_TOKEN。
    """
    tok = os.environ.get("FORWARD_BOT_TOKEN")
    if tok:
        return tok
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text("utf-8").splitlines():
            s = line.strip()
            if s.startswith("DISCORD_TOKEN=") and "=" in s:
                return s.split("=", 1)[1].strip()
    return None


async def list_servers_with_channels(token: str) -> list[dict]:
    """転送Botが参加している各サーバーと、その送信可能チャンネルを返す。

    返り値: [{"id","name","channels":[{"id","name","category"}],"roles":[{"id","name"}]}]
    チャンネルはカテゴリ順に並べ、ピッカーで見やすいラベルを付ける。
    roles は送信者フィルタ用（@everyone は除外）。
    """
    rest = DiscordREST(token)
    guilds = await rest.list_my_guilds()
    out: list[dict] = []
    for g in guilds:
        gid = str(g["id"])
        channels = await rest.list_channels(gid)
        flat: list[dict] = []
        for category, chans in channels_grouped(channels):
            cat_name = category["name"] if category else None
            for c in chans:
                flat.append(
                    {
                        "id": str(c["id"]),
                        "name": c["name"],
                        "category": cat_name,
                    }
                )
        roles: list[dict] = []
        try:
            for r in await rest.list_roles(gid):
                rid = str(r.get("id"))
                if rid == gid:  # @everyone は除外
                    continue
                roles.append({"id": rid, "name": r.get("name", rid)})
        except Exception:
            pass
        out.append({"id": gid, "name": g.get("name", gid), "channels": flat, "roles": roles})
    return out


def index_channels(servers: list[dict]) -> dict[str, dict]:
    """channel_id -> {"channel","server","server_id"} の逆引き表（ルール表示用）。"""
    idx: dict[str, dict] = {}
    for s in servers:
        for c in s["channels"]:
            idx[c["id"]] = {"channel": c["name"], "server": s["name"], "server_id": s["id"]}
    return idx


def index_roles(servers: list[dict]) -> dict[str, str]:
    """role_id -> role_name の逆引き表（ルール一覧のフィルタ表示用）。"""
    idx: dict[str, str] = {}
    for s in servers:
        for r in s.get("roles", []):
            idx[r["id"]] = r["name"]
    return idx
