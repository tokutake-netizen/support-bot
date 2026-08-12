"""送料設定（発送ルール・国一覧）のマスター複製と取り込み。

Musubot を他社に導入するときの流れ:
  1. マスターをダウンロード（このモジュールが Excel を組み立てる）
  2. 相手先に合わせて中身を直す
  3. スプレッドシートから範囲をコピーして、ダッシュボードに貼り付けて取り込む

取り込んだ内容はそのサーバーの deployments/<guild>/data/shipping_config.json に
入る。ファイルが無いサーバーは、リポジトリ同梱の i18n/countries.json を
そのまま使う（＝いまの挙動のまま）。
"""
from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Optional

from . import config_store

log = logging.getLogger(__name__)

CONFIG_FILE = "shipping_config.json"
CARRIER_RULES = ("DHL固定", "Fedex固定", "安い方")

RULES_HEADERS = ("ブロックコード", "運送会社ルール")
COUNTRY_HEADERS = ("国名", "ブロックコード")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_master() -> dict:
    """リポジトリ同梱のマスター（i18n/countries.json）。"""
    return json.loads((_repo_root() / "i18n" / "countries.json").read_text("utf-8"))


def config_path(guild_id: str) -> Path:
    return config_store.deployment_dir(str(guild_id)) / "data" / CONFIG_FILE


def load_config(guild_id: str) -> dict:
    """そのサーバーの送料設定。未設定ならマスターを返す。"""
    p = config_path(guild_id)
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except (OSError, ValueError):
            log.warning("shipping_config.json を読めませんでした（%s）。マスターを使います", guild_id)
    return load_master()


def has_own_config(guild_id: str) -> bool:
    return config_path(guild_id).exists()


def save_config(guild_id: str, data: dict) -> None:
    p = config_path(guild_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    tmp.replace(p)


def reset_config(guild_id: str) -> bool:
    """独自設定を捨ててマスターに戻す。"""
    p = config_path(guild_id)
    if not p.exists():
        return False
    p.unlink()
    return True


# ---------------------------------------------------------------- 貼り付け取り込み

def _split_rows(text: str) -> list[list[str]]:
    """スプレッドシートからのコピーはタブ区切り。CSV で貼られても拾う。"""
    rows = []
    for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.strip():
            continue
        cells = line.split("\t") if "\t" in line else line.split(",")
        rows.append([c.strip().strip('"') for c in cells])
    return rows


def detect_kind(rows: list[list[str]]) -> Optional[str]:
    """貼られたのが「発送ルール」か「国一覧」かを見出しから判定する。"""
    for row in rows[:5]:
        joined = " ".join(row)
        if all(h in joined for h in RULES_HEADERS):
            return "rules"
        if all(h in joined for h in COUNTRY_HEADERS):
            return "countries"
    return None


def _header_index(rows: list[list[str]], keyword: str) -> tuple[int, int]:
    for ri, row in enumerate(rows[:5]):
        for ci, cell in enumerate(row):
            if keyword in cell:
                return ri, ci
    raise ValueError(f"見出し「{keyword}」が見つかりません")


def parse_rules(rows: list[list[str]]) -> tuple[dict, list[str]]:
    """発送ルールの貼り付けを解釈する。戻り値 (ルール, 警告)。"""
    hr, c_code = _header_index(rows, "ブロックコード")
    header = rows[hr]

    def col(keyword: str) -> Optional[int]:
        for ci, cell in enumerate(header):
            if keyword in cell:
                return ci
        return None

    c_disp = col("見出し")
    c_rule = col("運送会社")
    c_memo = col("理由") or col("メモ")

    out, warn = {}, []
    for row in rows[hr + 1:]:
        if c_code >= len(row):
            continue
        code = row[c_code].strip()
        if not code or not code.startswith("block_"):
            continue
        rule = row[c_rule].strip() if c_rule is not None and c_rule < len(row) else ""
        if rule and rule not in CARRIER_RULES:
            warn.append(f"{code}: 「{rule}」は使えません。安い方として扱います")
            rule = "安い方"
        out[code] = {
            "display": row[c_disp].strip() if c_disp is not None and c_disp < len(row) else "",
            "carrier": rule or "安い方",
            "memo": row[c_memo].strip() if c_memo is not None and c_memo < len(row) else "",
        }
    if not out:
        raise ValueError("ブロックが1件も読み取れませんでした")
    return out, warn


def parse_countries(rows: list[list[str]]) -> tuple[list[dict], list[str]]:
    """国一覧の貼り付けを解釈する。"""
    hr, _ = _header_index(rows, "国名")
    header = rows[hr]

    def col(*keywords: str) -> Optional[int]:
        for ci, cell in enumerate(header):
            if any(k in cell for k in keywords):
                return ci
        return None

    c_ja = col("国名（日本語）", "国名(日本語)") or col("国名")
    c_en = col("英語", "English")
    c_iso2 = col("ISO2")
    c_iso3 = col("ISO3")
    c_flag = col("国旗")
    c_block = col("ブロックコード")
    c_alias = col("別名")
    c_excl = col("除外")

    region_of = {c["iso2"]: c["region"] for c in load_master()["countries"]}
    out, warn = [], []
    seen = set()
    for row in rows[hr + 1:]:
        def g(ci: Optional[int]) -> str:
            return row[ci].strip() if ci is not None and ci < len(row) else ""

        ja, iso2 = g(c_ja), g(c_iso2).upper()
        if not ja or not iso2:
            continue
        if iso2 in seen:
            warn.append(f"{ja}: ISO2 {iso2} が重複しています。後の行を無視しました")
            continue
        seen.add(iso2)
        block = g(c_block)
        if not block and not g(c_excl):
            warn.append(f"{ja}: ブロックコードが空です。この国は選べません")
        out.append({
            "name_ja": ja,
            "name_en": g(c_en) or ja,
            "iso2": iso2,
            "iso3": g(c_iso3).upper() or iso2,
            "flag": g(c_flag) or _flag(iso2),
            "region": region_of.get(iso2, "asia"),
            "block": block or None,
            "aliases": [a.strip() for a in g(c_alias).split(",") if a.strip()],
            **({"excluded": True} if g(c_excl) else {}),
        })
    if not out:
        raise ValueError("国が1件も読み取れませんでした")
    return out, warn


def _flag(iso2: str) -> str:
    if len(iso2) != 2 or not iso2.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in iso2.upper())


def apply_paste(guild_id: str, text: str) -> tuple[str, int, list[str]]:
    """貼り付けを取り込む。戻り値 (種別, 件数, 警告)。"""
    rows = _split_rows(text)
    if not rows:
        raise ValueError("貼り付けが空です")
    kind = detect_kind(rows)
    if kind is None:
        raise ValueError(
            "見出し行が見当たりません。「ブロックコード」を含む見出しごとコピーしてください"
        )

    cfg = load_config(guild_id)
    if kind == "rules":
        rules, warn = parse_rules(rows)
        cfg["shipping_rules"] = rules
        # 表示名が入っていればブロック別名も更新する
        al = cfg.setdefault("blocks", {}).setdefault("_aliases", {})
        for code, v in rules.items():
            if v.get("display"):
                al[code] = v["display"]
        save_config(guild_id, cfg)
        return "発送ルール", len(rules), warn

    countries, warn = parse_countries(rows)
    cfg["countries"] = countries
    save_config(guild_id, cfg)
    return "国一覧", len(countries), warn


# ---------------------------------------------------------------- マスター書き出し

def build_workbook(guild_id: Optional[str] = None) -> bytes:
    """設定を Excel にして返す。guild_id 省略時はマスター。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    cfg = load_config(guild_id) if guild_id else load_master()
    aliases = cfg.get("blocks", {}).get("_aliases", {})
    rules = cfg.get("shipping_rules", {})
    countries = cfg.get("countries", [])
    regions = {k: v["name_ja"] for k, v in cfg.get("regions", {}).items()}

    FONT, NAVY, BLUE, YELLOW = "Arial", "091E41", "2457C9", "FFF7CC"
    thin = Side(style="thin", color="D5D9E0")

    def bd():
        return Border(left=thin, right=thin, top=thin, bottom=thin)

    wb = Workbook()

    def header(ws, values):
        ws.append(values)
        for ci in range(1, len(values) + 1):
            c = ws.cell(row=1, column=ci)
            c.font = Font(name=FONT, bold=True, color="FFFFFF", size=11)
            c.fill = PatternFill("solid", fgColor=NAVY)
            c.alignment = Alignment(vertical="center", wrap_text=True)
            c.border = bd()

    def body(ws, ncols, nrows, wrap=()):
        for ri in range(2, 2 + nrows):
            for ci in range(1, ncols + 1):
                c = ws.cell(row=ri, column=ci)
                c.font = Font(name=FONT, size=10)
                c.border = bd()
                c.alignment = Alignment(vertical="top", wrap_text=(ci in wrap))

    # 使い方
    ws = wb.active
    ws.title = "使い方"
    guide = [
        ("Musubot 送料設定", "", True),
        ("", "", False),
        ("このファイルは何か", "Discordの「送料ロボ」が読む設定です。ダッシュボードの送料ロボ画面から貼り付けて取り込みます。", False),
        ("", "", False),
        ("導入の手順", "", True),
        ("1", "このファイルを Google ドライブにドラッグしてスプレッドシートにする", False),
        ("2", "導入先に合わせて「発送ルール」「国一覧」を直す", False),
        ("3", "タブごとに、見出し行を含めて全体をコピー", False),
        ("4", "ダッシュボード → 送料ロボ → 「設定を貼り付けて取り込む」に貼って取り込む", False),
        ("", "", False),
        ("触ってよい列（黄色）", "", True),
        ("運送会社ルール", "「DHL固定」「Fedex固定」「安い方」のいずれか。", False),
        ("理由・メモ", "なぜその会社にしたかを残す。あとから見た人に意図が伝わります。", False),
        ("国一覧", "国の追加・削除・ブロックの割当を変えられます。", False),
        ("", "", False),
        ("触ってはいけない列（灰色）", "", True),
        ("ブロックコード", "機械が使う目印です。変えると送料が引けなくなります。表示用の見出しは自由に変えて構いません。", False),
        ("", "", False),
        ("20kgを超える荷物", "1箱20kgを上限に自動で分割し、各箱の送料を合算します。例: 商品100kg → 6箱（各18kg）。", False),
        ("上乗せ", "送料ロボ画面の「追加サーチャージ」に率を入れると、表の金額に上乗せして案内します。", False),
    ]
    for r, (a, b, is_head) in enumerate(guide, start=1):
        ca, cb = ws.cell(row=r, column=1, value=a), ws.cell(row=r, column=2, value=b)
        if r == 1:
            ca.font = Font(name=FONT, bold=True, size=16, color=NAVY)
        elif is_head:
            ca.font = Font(name=FONT, bold=True, size=11, color=BLUE)
        else:
            ca.font = Font(name=FONT, size=10, bold=True)
        cb.font = Font(name=FONT, size=10)
        cb.alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 88

    # 発送ルール
    ws = wb.create_sheet("発送ルール")
    header(ws, ["ブロックコード", "送料表の見出し（表示用・自由に変更可）",
                "運送会社ルール", "理由・メモ", "対象国数"])
    for code, disp in aliases.items():
        r = rules.get(code, {})
        ws.append([code, r.get("display") or disp,
                   r.get("carrier") or ("DHL固定" if code == "block_us" else "安い方"),
                   r.get("memo") or ("Fedexは使わない（運用判断）" if code == "block_us" else ""),
                   None])
    n = len(aliases)
    body(ws, 5, n, wrap=(2, 4))
    for ri in range(2, 2 + n):
        ws.cell(row=ri, column=5, value=f"=COUNTIF(国一覧!$F:$F,$A{ri})")
        ws.cell(row=ri, column=1).font = Font(name=FONT, size=10, color="8A93A6")
        ws.cell(row=ri, column=3).fill = PatternFill("solid", fgColor=YELLOW)
        ws.cell(row=ri, column=3).font = Font(name=FONT, size=10, bold=True)
        ws.cell(row=ri, column=5).alignment = Alignment(horizontal="center", vertical="top")
    for col, w in zip("ABCDE", (18, 62, 15, 32, 10)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    if n:
        ws.auto_filter.ref = f"A1:E{1 + n}"

    # 国一覧
    ws = wb.create_sheet("国一覧")
    header(ws, ["国名（日本語）", "国名（英語）", "ISO2", "ISO3", "国旗",
                "ブロックコード", "地域", "別名（検索用・カンマ区切り）", "取扱除外"])
    for co in countries:
        ws.append([co["name_ja"], co["name_en"], co["iso2"], co["iso3"], co.get("flag", ""),
                   co.get("block") or "", regions.get(co.get("region"), co.get("region", "")),
                   ",".join(co.get("aliases", [])), "除外" if co.get("excluded") else ""])
    m = len(countries)
    body(ws, 9, m, wrap=(8,))
    for ri in range(2, 2 + m):
        for ci in (3, 4, 5, 9):
            ws.cell(row=ri, column=ci).alignment = Alignment(horizontal="center", vertical="top")
    for col, w in zip("ABCDEFGHI", (22, 26, 8, 8, 8, 18, 12, 34, 10)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    if m:
        ws.auto_filter.ref = f"A1:I{1 + m}"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
