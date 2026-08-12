"""Google Sheets client for shipping rates with header-based dynamic lookup + 5min cache."""
from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

import gspread
from google.oauth2.service_account import Credentials

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
CACHE_TTL_SECONDS = 300  # 5 minutes


RULES_SHEET_NAME = "発送ルール"

# 「運送会社ルール」欄に書ける値。表記ゆれを吸収する。
CARRIER_RULES = {
    "DHL固定": "DHL",
    "DHL": "DHL",
    "Fedex固定": "Fedex",
    "FEDEX固定": "Fedex",
    "Fedex": "Fedex",
    "安い方": "cheaper",
    "安いほう": "cheaper",
    "自動": "cheaper",
}


def parse_rules(rows: list[list[str]]) -> dict[str, dict[str, str]]:
    """「発送ルール」タブを読む。

    人が触るのはこのタブだけで済むようにするためのもの。1行1ブロックで
      ブロックコード / 送料表タブの見出し / 運送会社ルール / メモ / 対象国
    が並ぶ。見出し行は日本語なので、列位置ではなく見出し名で探す。

    戻り値: {ブロックコード: {"display": 表示名, "carrier": "DHL"|"Fedex"|"cheaper"}}
    """
    if not rows:
        return {}
    header_idx = None
    for i, row in enumerate(rows[:10]):
        if any("ブロックコード" in (c or "") for c in row):
            header_idx = i
            break
    if header_idx is None:
        return {}

    header = [(c or "").strip() for c in rows[header_idx]]

    def col(*keywords: str) -> Optional[int]:
        for ci, name in enumerate(header):
            if any(k in name for k in keywords):
                return ci
        return None

    c_code = col("ブロックコード")
    c_disp = col("見出し")
    c_rule = col("運送会社")
    if c_code is None:
        return {}

    out: dict[str, dict[str, str]] = {}
    for row in rows[header_idx + 1:]:
        if c_code >= len(row):
            continue
        code = (row[c_code] or "").strip()
        if not code or code.startswith("#"):
            continue
        disp = (row[c_disp] or "").strip() if c_disp is not None and c_disp < len(row) else ""
        rule_raw = (row[c_rule] or "").strip() if c_rule is not None and c_rule < len(row) else ""
        carrier = CARRIER_RULES.get(rule_raw, "")
        if rule_raw and not carrier:
            log.warning("発送ルールの『%s』を解釈できません（%s）。安い方として扱います", rule_raw, code)
            carrier = "cheaper"
        out[code] = {"display": disp, "carrier": carrier or "cheaper"}
    return out


def normalize_header(s: str) -> str:
    """ブロック見出しの照合用に正規化する。

    シートの見出しは「ヨーロッパ（オーストリア、ベルギー、…）」のように長く、
    改行やスペースの入り方が編集のたびに変わる。完全一致で照合していたため、
    空白を1つ足しただけで送料が引けなくなっていた。空白を全て落とし、
    全角半角と括弧・読点の揺れを吸収してから比べる。
    """
    import unicodedata
    s = unicodedata.normalize("NFKC", s or "")
    for a, b in (("（", "("), ("）", ")"), ("、", ","), ("／", "/"), ("　", "")):
        s = s.replace(a, b)
    return "".join(s.split()).lower()


def _is_carrier_choice_header(cell: Optional[str]) -> bool:
    """「どちらの運送会社で送るか」を示す列見出しか。

    ブロックによって書き方が違う:
      ・安い方
      ・使う配送方法（US/DDU_DHL）  ← 安さではなく意図的に DHL を選ぶ場合
    """
    s = (cell or "").strip()
    if not s:
        return False
    return any(k in s for k in ("安い", "使う配送", "配送方法", "採用", "使用便"))


def _parse_price(row: list[str], col: Optional[int]) -> Optional[int]:
    """「¥2,683」のような表記を整数の円に直す。読めなければ None。"""
    if col is None or col >= len(row):
        return None
    s = (row[col] or "").replace("¥", "").replace(",", "").replace("￥", "").strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


@dataclass
class RateResult:
    carrier: str       # "DHL" or "Fedex"
    price_jpy: int
    bracket_kg: float
    block_header: str


class SheetsClient:
    def __init__(self) -> None:
        self.sheet_id = os.getenv("SHIPPING_SHEET_ID", "")
        self.sheet_name = os.getenv("SHIPPING_SHEET_NAME", "比較表US基準")
        cred_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "./credentials/service_account.json")
        self.cred_path = cred_path

        self._client: Optional[gspread.Client] = None
        self._cache: dict[str, Any] = {}
        self._cache_ts: float = 0.0

    def _connect(self) -> gspread.Client:
        if self._client is None:
            # 1. Try base64-encoded JSON in env (Railway / cloud-friendly)
            b64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "").strip()
            if b64:
                import base64
                import json as _json
                try:
                    info = _json.loads(base64.b64decode(b64))
                    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
                    self._client = gspread.authorize(creds)
                    return self._client
                except Exception:
                    log.exception("failed to load service account from b64 env, falling back to file")
            # 2. Fallback: file path (local dev)
            creds = Credentials.from_service_account_file(self.cred_path, scopes=SCOPES)
            self._client = gspread.authorize(creds)
        return self._client

    def _load(self, force: bool = False) -> dict[str, Any]:
        """Load and parse the sheet. Returns dict with weights, blocks, max_kg, fetched_at."""
        if not force and self._cache and (time.time() - self._cache_ts) < CACHE_TTL_SECONDS:
            return self._cache

        client = self._connect()
        ws = client.open_by_key(self.sheet_id).worksheet(self.sheet_name)
        rows = ws.get_all_values()  # list[list[str]]

        # Heuristic parser: find weight column (column with sequential 0.5 step values)
        # Find header row containing block names (Row 2 in expected layout)
        # Find sub-header row with 'DHL'/'Fedex'/'安い方' (Row 3)

        sub_header_row_idx: Optional[int] = None
        for ri, row in enumerate(rows[:10]):
            if any("DHL" in (c or "") for c in row) and any("Fedex" in (c or "") for c in row):
                sub_header_row_idx = ri
                break
        if sub_header_row_idx is None:
            raise RuntimeError("Could not find DHL/Fedex sub-header row")

        block_header_row_idx = max(sub_header_row_idx - 1, 0)
        block_header_row = rows[block_header_row_idx]
        sub_header_row = rows[sub_header_row_idx]

        # Identify column indices: walk through sub_header looking for triples
        blocks: dict[str, dict[str, int]] = {}
        last_block_label: str = ""
        for ci, cell in enumerate(sub_header_row):
            label = (block_header_row[ci] if ci < len(block_header_row) else "").strip()
            if label:
                last_block_label = label
            cell_norm = (cell or "").strip().lower()
            if not last_block_label:
                continue
            if cell_norm == "dhl":
                blocks.setdefault(last_block_label, {})["dhl"] = ci
            elif cell_norm == "fedex":
                blocks.setdefault(last_block_label, {})["fedex"] = ci
            elif _is_carrier_choice_header(cell):
                # 「どちらの運送会社で送るか」を書いてある列。
                # 多くのブロックは「安い方」だが、アメリカのように安さではなく
                # 意図的に DHL を選んでいるブロックは「使う配送方法」と書かれる。
                # どちらも同じ意味の列として扱う。
                blocks.setdefault(last_block_label, {}).setdefault("carrier_col", ci)

        # Find weight column: look in column index 0 or 1 for first row after header that parses as float
        weight_col = 1  # B column = index 1
        weights: dict[float, int] = {}  # kg -> row_index
        for ri in range(sub_header_row_idx + 1, len(rows)):
            row = rows[ri]
            if weight_col >= len(row):
                continue
            v = (row[weight_col] or "").replace(",", "").strip()
            try:
                kg = float(v)
            except ValueError:
                continue
            if kg <= 0:
                continue
            weights[kg] = ri

        max_kg = max((kg for kg in weights), default=0.0)

        self._cache = {
            "rows": rows,
            "weights": weights,
            "blocks": blocks,
            "max_kg": max_kg,
            "fetched_at": time.time(),
        }
        self._cache_ts = time.time()
        log.info(
            "Sheet loaded: %d blocks, %d weight rows, max=%skg",
            len(blocks), len(weights), max_kg,
        )
        return self._cache

    @staticmethod
    def round_up_to_half(kg: float) -> float:
        return math.ceil(kg * 2) / 2

    def lookup(self, block_header: str, weight_kg: float) -> Optional[RateResult]:
        data = self._load()
        blocks: dict[str, dict[str, int]] = data["blocks"]
        weights: dict[float, int] = data["weights"]
        rows: list[list[str]] = data["rows"]

        bracket = self.round_up_to_half(weight_kg)
        if bracket > data["max_kg"]:
            return None

        block = blocks.get(block_header)
        if not block:
            # 完全一致しなければ、空白や全角半角を無視して照合する
            want = normalize_header(block_header)
            for bh, info in blocks.items():
                if normalize_header(bh) == want:
                    block, block_header = info, bh
                    break
        if not block:
            # それでも駄目なら部分一致（見出しを短く書き換えた場合の救済）
            want = normalize_header(block_header)
            for bh, info in blocks.items():
                nb = normalize_header(bh)
                if want and (want in nb or nb in want):
                    block, block_header = info, bh
                    break
        if not block:
            log.warning(
                "送料表にブロック『%s』が見つかりません。シートの見出しと "
                "countries.json の対応を確認してください（読めている見出し: %s）",
                block_header, list(blocks)[:3],
            )
            return None

        row_idx = weights.get(bracket)
        if row_idx is None:
            # advance to next available bracket
            higher = sorted(k for k in weights if k >= bracket)
            if not higher:
                return None
            row_idx = weights[higher[0]]
            bracket = higher[0]

        row = rows[row_idx]
        dhl_price = _parse_price(row, block.get("dhl"))
        fedex_price = _parse_price(row, block.get("fedex"))

        carrier = self._decide_carrier(row, block, block_header, dhl_price, fedex_price)
        if carrier is None:
            return None
        price = dhl_price if carrier == "DHL" else fedex_price
        if price is None:
            return None
        return RateResult(carrier=carrier, price_jpy=price, bracket_kg=bracket, block_header=block_header)

    @staticmethod
    def _decide_carrier(
        row: list[str],
        block: dict[str, int],
        block_header: str,
        dhl_price: Optional[int],
        fedex_price: Optional[int],
    ) -> Optional[str]:
        """このブロックをどちらの運送会社で送るかを決める。

        シートの「使う配送方法／安い方」列に書かれた指定を最優先する。
        アメリカのように、安さではなく意図的に DHL を選んでいるブロックが
        あるため、ここを勝手に安い方で上書きしてはいけない。

        指定が読めなかったときは、以前は無条件に Fedex にしていたが、それだと
        シートの指定と逆の運送会社を選びうる。安い方に倒す。
        """
        raw = ""
        ci = block.get("carrier_col")
        if ci is not None and ci < len(row):
            raw = (row[ci] or "").strip()
        upper = raw.upper()
        if upper.startswith("D"):
            return "DHL"
        if upper.startswith("F"):
            return "Fedex"

        if raw:
            log.warning(
                "運送会社の指定を解釈できませんでした（%s: %r）。安い方を採ります",
                block_header, raw,
            )
        else:
            log.warning(
                "運送会社を指定する列が見つかりません（%s）。安い方を採ります", block_header
            )
        if dhl_price is None and fedex_price is None:
            return None
        if fedex_price is None:
            return "DHL"
        if dhl_price is None:
            return "Fedex"
        return "DHL" if dhl_price <= fedex_price else "Fedex"

    def reload(self) -> dict[str, Any]:
        return self._load(force=True)

    def get_max_kg(self) -> float:
        return float(self._load().get("max_kg", 0))

    def get_block_count(self) -> int:
        return len(self._load().get("blocks", {}))
