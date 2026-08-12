"""規約ページに差し込む運営者の情報。

会社名や連絡先をテンプレートに直接書くと、社名変更や移転のたびに
コードを直すことになるので環境変数から読む。未設定の項目は画面に
「（未設定）」と出して、公開前に埋め忘れたことが分かるようにする。

Railway の Variables に入れる想定:
  LEGAL_COMPANY_NAME   運営会社名
  LEGAL_REPRESENTATIVE 代表者名
  LEGAL_ADDRESS        所在地
  LEGAL_CONTACT_EMAIL  問い合わせ窓口のメールアドレス
  LEGAL_CONTACT_URL    問い合わせフォームのURL（任意）
  LEGAL_EFFECTIVE_DATE 規約の施行日（例 2026年8月12日）
"""
from __future__ import annotations

import os

UNSET = "（未設定）"

FIELDS = {
    "company": "LEGAL_COMPANY_NAME",
    "representative": "LEGAL_REPRESENTATIVE",
    "address": "LEGAL_ADDRESS",
    "email": "LEGAL_CONTACT_EMAIL",
    "contact_url": "LEGAL_CONTACT_URL",
    "effective_date": "LEGAL_EFFECTIVE_DATE",
}


def info() -> dict:
    out = {}
    for key, env in FIELDS.items():
        out[key] = (os.environ.get(env) or "").strip() or UNSET
    out["service"] = "Musubot（ムスボット）"
    out["incomplete"] = [
        env for key, env in FIELDS.items()
        if key != "contact_url" and out[key] == UNSET
    ]
    return out
