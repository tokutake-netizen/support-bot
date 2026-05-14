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
            elif "安い" in (cell or ""):
                blocks.setdefault(last_block_label, {})["cheaper"] = ci

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
            # try fuzzy match by substring
            for bh, info in blocks.items():
                if block_header in bh or bh in block_header:
                    block = info
                    block_header = bh
                    break
        if not block:
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
        cheaper = (row[block["cheaper"]] if "cheaper" in block and block["cheaper"] < len(row) else "").strip()
        carrier = "DHL" if cheaper.upper().startswith("D") else "Fedex"
        price_col = block["dhl"] if carrier == "DHL" else block["fedex"]
        if price_col >= len(row):
            return None
        price_str = (row[price_col] or "").replace("¥", "").replace(",", "").strip()
        try:
            price = int(float(price_str))
        except ValueError:
            return None
        return RateResult(carrier=carrier, price_jpy=price, bracket_kg=bracket, block_header=block_header)

    def reload(self) -> dict[str, Any]:
        return self._load(force=True)

    def get_max_kg(self) -> float:
        return float(self._load().get("max_kg", 0))

    def get_block_count(self) -> int:
        return len(self._load().get("blocks", {}))
