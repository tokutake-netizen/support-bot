"""Email + password user store for the dashboard.

Lives next to the deployments volume so it survives Railway redeploys:
  /data/dashboard_users.json (or DASHBOARD_USERS_FILE override).

Passwords are stored as pbkdf2_sha256 with a per-user salt — no plaintext,
no external deps. Root admin is bootstrapped from env vars:
  - DASHBOARD_ROOT_EMAIL
  - DASHBOARD_ROOT_PASSWORD
and is NOT persisted to the JSON file. Changing the env rotates the root.

Allowlist is a single boolean per user. Allowed = full dashboard access.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

PBKDF2_ITER = 200_000
HASH_ALGO = "sha256"


def _users_path() -> Path:
    raw = os.environ.get("DASHBOARD_USERS_FILE")
    if raw:
        return Path(raw)
    # Default: next to deployments root if /data is available, else local.
    deployments_root = os.environ.get("DEPLOYMENTS_ROOT")
    if deployments_root:
        return Path(deployments_root).parent / "dashboard_users.json"
    return Path(__file__).resolve().parent.parent / "dashboard_users.json"


def _load() -> dict:
    p = _users_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text("utf-8"))
    except json.JSONDecodeError:
        return {}


def _save(data: dict) -> None:
    p = _users_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")


def _hash_password(password: str, salt_hex: Optional[str] = None) -> tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(HASH_ALGO, password.encode("utf-8"), salt, PBKDF2_ITER)
    return salt.hex(), dk.hex()


def _verify_password(password: str, salt_hex: str, expected_hex: str) -> bool:
    _, dk_hex = _hash_password(password, salt_hex)
    return secrets.compare_digest(dk_hex, expected_hex)


# ---------- root admin (env-bootstrapped) ----------

def root_email() -> Optional[str]:
    e = (os.environ.get("DASHBOARD_ROOT_EMAIL") or "").strip().lower()
    return e or None


def is_root(email: str) -> bool:
    r = root_email()
    return bool(r and email.lower() == r)


def authenticate(email: str, password: str) -> Optional[dict]:
    """Return user dict on success, None on failure.

    Order:
      1. Root admin (env DASHBOARD_ROOT_EMAIL + DASHBOARD_ROOT_PASSWORD)
      2. Stored users in dashboard_users.json (only if allowed=True)
    """
    email = (email or "").strip().lower()
    if not email or not password:
        return None

    if is_root(email):
        if password == os.environ.get("DASHBOARD_ROOT_PASSWORD"):
            return {
                "email": email,
                "is_root": True,
                "allowed": True,
            }
        return None

    users = _load()
    record = users.get(email)
    if not record:
        return None
    if not record.get("allowed"):
        return None
    if not _verify_password(password, record["salt"], record["pwhash"]):
        return None
    return {
        "email": email,
        "is_root": False,
        "allowed": True,
        "added_by": record.get("added_by"),
        "created_at": record.get("created_at"),
        "guilds": allowed_guilds(email),
        "tenant": record.get("tenant"),
        "must_change_password": bool(record.get("must_change_password")),
    }


# ---------- 担当サーバー（テナント越境を防ぐ要） ----------
#
# メール認証のユーザーは、以前は「全サーバーに素通し」だった。他社に
# アカウントを配ると、URL のサーバーIDを差し替えるだけで他社の APIキーが
# 見えてしまうため、ユーザーごとに触れるサーバーを持たせる。
#
# 既存ユーザーには guilds が無い。移行の取りこぼしで既存運用を止めたくない
# ので、その場合は「全サーバー」として扱う（＝従来どおり）。他社を迎える前に
# migrate_existing_users() で明示的に付与すること。

def allowed_guilds(email: str) -> Optional[list[str]]:
    """このユーザーが触れるサーバーID。None は「全部」の意味。"""
    rec = _load().get((email or "").strip().lower())
    if not rec:
        return []
    g = rec.get("guilds")
    if g is None:
        return None
    return [str(x) for x in g]


def set_allowed_guilds(email: str, guild_ids: Optional[list[str]]) -> bool:
    """担当サーバーを設定する。None を渡すと全サーバー許可に戻る。"""
    email = (email or "").strip().lower()
    users = _load()
    if email not in users:
        return False
    if guild_ids is None:
        users[email].pop("guilds", None)
    else:
        users[email]["guilds"] = [str(x) for x in guild_ids]
    _save(users)
    return True


def can_access_guild(email: str, guild_id: str) -> bool:
    """このユーザーがそのサーバーを触ってよいか。

    会社（テナント）に属していればそちらが正。会社が利用停止なら全部拒否。
    会社に属していない旧来のユーザーは、個別の担当リストで判定する。
    """
    email = (email or "").strip().lower()
    slug = tenant_of(email)
    if slug:
        from . import tenants
        if not tenants.is_active(slug):
            return False
        return str(guild_id) in tenants.guilds_for_tenant(slug)

    allowed = allowed_guilds(email)
    if allowed is None:      # 未移行のユーザー（従来どおり全部）
        return True
    return str(guild_id) in allowed


def migrate_existing_users(all_guild_ids: list[str]) -> int:
    """guilds を持たない既存ユーザーに、現時点の全サーバーを付与する。

    他社を迎える前に一度流す。これをしないと既存ユーザーは「全部見える」
    ままなので、新しい会社のサーバーまで見えてしまう。
    """
    users = _load()
    n = 0
    for email, rec in users.items():
        if "guilds" not in rec:
            rec["guilds"] = [str(g) for g in all_guild_ids]
            n += 1
    if n:
        _save(users)
    return n


# ---------- 会社（テナント）との紐づけ ----------

def set_tenant(email: str, slug: Optional[str]) -> bool:
    email = (email or "").strip().lower()
    users = _load()
    if email not in users:
        return False
    if slug:
        users[email]["tenant"] = slug
    else:
        users[email].pop("tenant", None)
    _save(users)
    return True


def tenant_of(email: str) -> Optional[str]:
    rec = _load().get((email or "").strip().lower())
    return rec.get("tenant") if rec else None


def users_of_tenant(slug: str) -> list[dict]:
    out = []
    for email, rec in _load().items():
        if rec.get("tenant") != slug:
            continue
        out.append({
            "email": email,
            "allowed": bool(rec.get("allowed")),
            "status": rec.get("status", "approved"),
            "role": rec.get("role", "staff"),
            "must_change_password": bool(rec.get("must_change_password")),
            "created_at": rec.get("created_at"),
        })
    out.sort(key=lambda u: u["email"])
    return out


def set_role(email: str, role: str) -> bool:
    if role not in ("tenant_admin", "staff"):
        return False
    email = (email or "").strip().lower()
    users = _load()
    if email not in users:
        return False
    users[email]["role"] = role
    _save(users)
    return True


def role_of(email: str) -> str:
    rec = _load().get((email or "").strip().lower())
    return (rec or {}).get("role", "staff")


# ---------- 初期パスワードと初回変更 ----------

def invite(email: str, tenant: str, role: str = "staff", added_by: str = "") -> tuple[dict, str]:
    """担当者を追加し、自動生成の初期パスワードを返す。

    管理者が任意の文字列を決められるようにはしない。管理者が恒常パスワードを
    知っている状態だと、本人になりすました操作をログで区別できなくなるため。
    初回ログイン時に本人が変更する。
    """
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("メールアドレスの形式が正しくありません")
    users = _load()
    password = _gen_password()
    salt, pwhash = _hash_password(password)
    users[email] = {
        **users.get(email, {}),
        "pwhash": pwhash,
        "salt": salt,
        "allowed": True,
        "status": "approved",
        "tenant": tenant,
        "role": role if role in ("tenant_admin", "staff") else "staff",
        "must_change_password": True,
        "added_by": added_by,
        "created_at": int(time.time()),
    }
    _save(users)
    return {"email": email, "tenant": tenant, "role": role}, password


def reset_password(email: str) -> Optional[str]:
    """初期パスワードを再発行する。次回ログイン時に本人が変更する。"""
    email = (email or "").strip().lower()
    users = _load()
    if email not in users:
        return None
    password = _gen_password()
    users[email]["salt"], users[email]["pwhash"] = _hash_password(password)
    users[email]["must_change_password"] = True
    _save(users)
    return password


def change_password(email: str, new_password: str) -> bool:
    """本人がパスワードを変更する。強制変更フラグを落とす。"""
    email = (email or "").strip().lower()
    if len(new_password or "") < 10:
        raise ValueError("パスワードは10文字以上にしてください")
    users = _load()
    if email not in users:
        return False
    users[email]["salt"], users[email]["pwhash"] = _hash_password(new_password)
    users[email]["must_change_password"] = False
    _save(users)
    return True


# ---------- user management (admin operations) ----------

def list_users() -> list[dict]:
    users = _load()
    out = []
    for email, rec in users.items():
        out.append({
            "email": email,
            "allowed": bool(rec.get("allowed")),
            "status": rec.get("status", "approved"),  # legacy users default to approved
            "added_by": rec.get("added_by") or rec.get("approved_by"),
            "created_at": rec.get("created_at") or rec.get("approved_at"),
            "requested_at": rec.get("requested_at"),
            "is_root": False,
        })
    # Sort: pending first, then approved by created_at
    out.sort(key=lambda u: (0 if u["status"] == "pending" else 1, -(u.get("created_at") or 0)))
    # Surface root explicitly so the admin page makes the bootstrap visible.
    r = root_email()
    if r:
        out.insert(0, {
            "email": r,
            "allowed": True,
            "status": "approved",
            "added_by": "(env)",
            "created_at": None,
            "requested_at": None,
            "is_root": True,
        })
    return out


def add_user(email: str, password: str, added_by: str = "") -> dict:
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("invalid email")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    if is_root(email):
        raise ValueError("this email is the root admin (env-managed)")
    salt, pwhash = _hash_password(password)
    users = _load()
    users[email] = {
        "pwhash": pwhash,
        "salt": salt,
        "allowed": True,
        "status": "approved",
        "added_by": added_by,
        "created_at": int(time.time()),
        "approved_at": int(time.time()),
    }
    _save(users)
    return {"email": email, "allowed": True, "added_by": added_by}


# ---------- registration / approval workflow ----------

def request_access(email: str) -> dict:
    """Anyone can call this to request access. Marks the user "pending"
    (no password yet). Idempotent — re-requesting just refreshes the
    requested_at timestamp."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("invalid email")
    if is_root(email):
        raise ValueError("this email is the root admin (env-managed)")
    users = _load()
    existing = users.get(email)
    if existing and existing.get("status") == "approved":
        return {"email": email, "status": "already_approved"}
    users[email] = {
        **(existing or {}),
        "status": "pending",
        "allowed": False,
        "requested_at": int(time.time()),
    }
    # Pending users have no password yet; drop any stale hash to be safe.
    users[email].pop("pwhash", None)
    users[email].pop("salt", None)
    _save(users)
    return {"email": email, "status": "pending"}


def list_pending() -> list[dict]:
    users = _load()
    return [
        {"email": e, **rec}
        for e, rec in users.items()
        if rec.get("status") == "pending"
    ]


def _gen_password() -> str:
    """16-char URL-safe password (suitable for emailing)."""
    return secrets.token_urlsafe(12)


def approve_user(email: str, approved_by: str = "") -> tuple[dict, str]:
    """Approve a pending user. Returns (user_record, plaintext_password).
    The caller is responsible for emailing the plaintext password — it is
    not persisted anywhere except as a hash.
    """
    email = (email or "").strip().lower()
    if is_root(email):
        raise ValueError("this email is the root admin (env-managed)")
    users = _load()
    rec = users.get(email)
    if not rec:
        raise ValueError("user not found")
    password = _gen_password()
    salt, pwhash = _hash_password(password)
    rec.update({
        "pwhash": pwhash,
        "salt": salt,
        "allowed": True,
        "status": "approved",
        "approved_by": approved_by,
        "approved_at": int(time.time()),
    })
    users[email] = rec
    _save(users)
    return ({"email": email, **rec}, password)


def reject_user(email: str) -> bool:
    """Delete a pending request entirely."""
    email = (email or "").strip().lower()
    users = _load()
    if email not in users or users[email].get("status") != "pending":
        return False
    del users[email]
    _save(users)
    return True


def regenerate_password(email: str) -> Optional[str]:
    """Generate a fresh password for an approved user and return it.
    Used by the /forgot endpoint. Returns None if the user doesn't
    exist or isn't approved (so unknown emails don't reveal anything).
    """
    email = (email or "").strip().lower()
    if is_root(email):
        return None
    users = _load()
    rec = users.get(email)
    if not rec or rec.get("status") != "approved":
        return None
    password = _gen_password()
    salt, pwhash = _hash_password(password)
    rec.update({"pwhash": pwhash, "salt": salt, "reset_at": int(time.time())})
    users[email] = rec
    _save(users)
    return password


def set_allowed(email: str, allowed: bool) -> bool:
    email = (email or "").strip().lower()
    if is_root(email):
        return False  # root status is env-managed
    users = _load()
    if email not in users:
        return False
    users[email]["allowed"] = bool(allowed)
    _save(users)
    return True


def remove_user(email: str) -> bool:
    email = (email or "").strip().lower()
    if is_root(email):
        return False
    users = _load()
    if email not in users:
        return False
    del users[email]
    _save(users)
    return True


# ---------- Discord-login root admins (managed in-app) ----------
# Discord-OAuth logins are matched against this list to decide is_root, so root
# can be granted from the dashboard instead of editing Railway env each time.
# Bootstrap IDs in DASHBOARD_ROOT_DISCORD_ID (CSV) are always root and are shown
# as "(env)" — they cannot be removed from the UI.

def _discord_roots_path() -> Path:
    raw = os.environ.get("DASHBOARD_DISCORD_ROOTS_FILE")
    if raw:
        return Path(raw)
    return _users_path().parent / "dashboard_discord_roots.json"


def _load_discord_roots() -> dict:
    p = _discord_roots_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text("utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_discord_roots(data: dict) -> None:
    p = _discord_roots_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")


def env_discord_root_ids() -> set[str]:
    raw = os.environ.get("DASHBOARD_ROOT_DISCORD_ID", "")
    return {s.strip() for s in raw.split(",") if s.strip()}


def _normalize_discord_id(user_id: str) -> str:
    uid = (user_id or "").strip()
    if not uid.isdigit() or not (17 <= len(uid) <= 20):
        raise ValueError("Discord ユーザーIDは17〜20桁の数字で入力してください")
    return uid


def is_discord_root(user_id) -> bool:
    uid = str(user_id)
    return uid in env_discord_root_ids() or uid in _load_discord_roots()


def list_discord_roots() -> list[dict]:
    """Combined view: env bootstrap IDs first (non-removable), then stored ones."""
    out: list[dict] = []
    stored = _load_discord_roots()
    for uid in sorted(env_discord_root_ids()):
        rec = stored.get(uid, {})
        out.append({
            "id": uid,
            "label": rec.get("label") or "",
            "added_by": "(env)",
            "created_at": rec.get("created_at"),
            "source": "env",
        })
    env_ids = env_discord_root_ids()
    for uid, rec in stored.items():
        if uid in env_ids:
            continue
        out.append({
            "id": uid,
            "label": rec.get("label") or "",
            "added_by": rec.get("added_by") or "",
            "created_at": rec.get("created_at"),
            "source": "stored",
        })
    return out


def add_discord_root(user_id: str, label: str = "", added_by: str = "") -> dict:
    uid = _normalize_discord_id(user_id)
    roots = _load_discord_roots()
    roots[uid] = {
        "label": (label or "").strip(),
        "added_by": added_by,
        "created_at": int(time.time()),
    }
    _save_discord_roots(roots)
    return {"id": uid, **roots[uid]}


def remove_discord_root(user_id: str) -> bool:
    uid = str(user_id).strip()
    if uid in env_discord_root_ids():
        return False  # env-managed, not removable from UI
    roots = _load_discord_roots()
    if uid not in roots:
        return False
    del roots[uid]
    _save_discord_roots(roots)
    return True
