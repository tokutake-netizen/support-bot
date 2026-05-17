"""Per-guild config storage — reads/writes deployments/<guild-id>/.env files.

We deliberately keep the existing `deployments/<name>/.env` layout so the
existing main.py + cogs work unchanged — the dashboard is just a typed
front-end to that same file.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Optional

# Resolve deployments dir. On Railway we mount a volume at /data/deployments;
# locally fall back to ./deployments.
def deployments_root() -> Path:
    root = os.environ.get("DEPLOYMENTS_ROOT")
    if root:
        return Path(root)
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root / "deployments"


def deployment_dir(guild_id: str) -> Path:
    return deployments_root() / str(guild_id)


def template_env_example() -> Path:
    return Path(__file__).resolve().parent.parent / "deployments" / "template" / ".env.example"


def ensure_deployment(guild_id: str) -> Path:
    """Create the deployment dir + seed with .env from template if missing."""
    d = deployment_dir(guild_id)
    (d / "credentials").mkdir(parents=True, exist_ok=True)
    (d / "data").mkdir(parents=True, exist_ok=True)
    env_path = d / ".env"
    if not env_path.exists():
        tpl = template_env_example()
        if tpl.exists():
            shutil.copy(tpl, env_path)
        else:
            env_path.write_text("# created by dashboard\n", "utf-8")
    return d


def read_env(guild_id: str) -> dict[str, str]:
    env_path = deployment_dir(guild_id) / ".env"
    if not env_path.exists():
        return {}
    out: dict[str, str] = {}
    for line in env_path.read_text("utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = v.strip()
    return out


_KEY_RE = re.compile(r"^(?P<key>[A-Z][A-Z0-9_]*)=", re.M)


def write_env(guild_id: str, updates: dict[str, str]) -> None:
    """Merge updates into the existing .env. Preserves comments & ordering.

    Keys not present in the file are appended at the end. Values containing
    newlines are not supported (warn upstream).
    """
    d = ensure_deployment(guild_id)
    env_path = d / ".env"
    content = env_path.read_text("utf-8") if env_path.exists() else ""
    existing_keys = set(m.group("key") for m in _KEY_RE.finditer(content))

    # Replace in place
    lines_out = []
    for line in content.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            key = s.split("=", 1)[0].strip()
            if key in updates:
                lines_out.append(f"{key}={updates[key]}")
                continue
        lines_out.append(line)

    # Append new keys
    for k, v in updates.items():
        if k not in existing_keys:
            lines_out.append(f"{k}={v}")

    env_path.write_text("\n".join(lines_out) + "\n", "utf-8")


def list_deployments() -> list[str]:
    root = deployments_root()
    if not root.exists():
        return []
    out = []
    for p in root.iterdir():
        if p.is_dir() and p.name != "template" and (p / ".env").exists():
            out.append(p.name)
    return sorted(out)


def write_credential_file(guild_id: str, filename: str, content: bytes) -> Path:
    d = ensure_deployment(guild_id)
    target = d / "credentials" / filename
    target.write_bytes(content)
    return target


def extract_sheet_id(url_or_id: str) -> Optional[str]:
    """Pull the Sheet ID out of a Google Sheets URL, or return the input if it already looks like an ID."""
    s = url_or_id.strip()
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]{30,}", s):
        return s
    return None
