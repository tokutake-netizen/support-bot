"""SMTP email sender.

Reads SMTP_HOST / SMTP_PORT / SMTP_USERNAME / SMTP_PASSWORD /
SMTP_FROM_EMAIL / SMTP_FROM_NAME from the environment. Designed for
low volume (approval emails, password reset) so blocking smtplib is
fine — we run it inside a threadpool from the FastAPI route.

If SMTP is not configured, ``send()`` returns False and the caller can
fall back to "show the password on screen" so the admin can hand it
over manually.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Optional

log = logging.getLogger(__name__)


def smtp_configured() -> bool:
    return all(
        os.environ.get(k)
        for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM_EMAIL")
    )


def send(
    to_email: str,
    subject: str,
    body: str,
    *,
    from_email: Optional[str] = None,
    from_name: Optional[str] = None,
) -> tuple[bool, str]:
    """Send a plain-text email. Returns (ok, message)."""
    if not smtp_configured():
        return False, "SMTP not configured (set SMTP_* env vars)"
    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    username = os.environ["SMTP_USERNAME"]
    password = os.environ["SMTP_PASSWORD"]
    sender_email = from_email or os.environ["SMTP_FROM_EMAIL"]
    sender_name = from_name or os.environ.get("SMTP_FROM_NAME") or "Support Bot Dashboard"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((sender_name, sender_email))
    msg["To"] = to_email

    try:
        # Gmail / most modern providers: STARTTLS on 587. SSL on 465.
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20) as srv:
                srv.login(username, password)
                srv.sendmail(sender_email, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=20) as srv:
                srv.ehlo()
                srv.starttls()
                srv.ehlo()
                srv.login(username, password)
                srv.sendmail(sender_email, [to_email], msg.as_string())
        return True, "sent"
    except Exception as e:
        log.exception("SMTP send failed")
        return False, str(e)


def render_approval_email(email: str, password: str, login_url: str) -> tuple[str, str]:
    subject = "Support Bot Dashboard — アカウント承認のお知らせ"
    body = (
        f"こんにちは。\n\n"
        f"Support Bot ダッシュボードへのアクセスが承認されました。\n\n"
        f"以下の情報でログインしてください:\n"
        f"  ログイン URL: {login_url}\n"
        f"  メールアドレス: {email}\n"
        f"  初期パスワード: {password}\n\n"
        f"ログイン後は必要に応じてパスワードリセット機能で変更できます。\n\n"
        f"このメールに心当たりがない場合は管理者にご連絡ください。\n"
    )
    return subject, body


def render_reset_email(email: str, password: str, login_url: str) -> tuple[str, str]:
    subject = "Support Bot Dashboard — パスワード再発行"
    body = (
        f"こんにちは。\n\n"
        f"パスワードの再発行が行われました。\n\n"
        f"  ログイン URL: {login_url}\n"
        f"  メールアドレス: {email}\n"
        f"  新しいパスワード: {password}\n\n"
        f"このメールに心当たりがない場合は、ただちに管理者にご連絡ください。\n"
    )
    return subject, body
