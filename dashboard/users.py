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
    }


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
