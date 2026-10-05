"""
東証33業種の対応表（証券コード→33業種区分）を扱う。

業種の物差しは東証33業種にそろえる（Kindle 本と同じ物差しにするため）。
以前は日経の指数ページの見出しに出る業種（28種）を使っていたが、
2026-10 に切り替えた。日経の業種名はもう計算に使わない。

真実の源は sector33.csv（列: 証券コード, 銘柄名, 33業種区分, データ日付）。
JPX「東証上場銘柄一覧」(data_j.xlsx、月1回更新) からの取り直しはベストエフォートで、
落ちても既存CSVで走り続ける（stock_splits.csv と同じ構え）。
"""

import csv
import logging
import os
import re
from io import BytesIO
from urllib.parse import urljoin

import requests

logging.basicConfig(level=logging.ERROR, filename="error.log")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SECTOR_CSV = os.path.join(BASE_DIR, "sector33.csv")

CSV_FIELDS = ["証券コード", "銘柄名", "33業種区分", "データ日付"]

# 一覧ページから data_j.* のリンクを拾う（2026-10 に .xls → .xlsx へ変わった前例があるため直書きしない）
JPX_LIST_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
_DATA_LINK_RE = re.compile(r'href="([^"]*data_j\.xlsx?)"')

# 対応表に無いコードの業種。保有の集計で行を落とさないために使う。
UNKNOWN_SECTOR = "不明"

REQUEST_TIMEOUT = 60
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def normalize_code(code):
    """7203 / 7203.0 / "7203" / " 7203 " → "7203"。英字入りコード（130A など）はそのまま。"""
    text = str(code).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def load_sector_map(path=SECTOR_CSV):
    """
    対応表を {証券コード(str): 33業種区分} で返す。

    ファイル未配置・読み込み失敗時は空辞書（fail-open）。空のときに業種を
    「不明」で埋めて選定すると全候補が落ちるので、呼び出し側で空かどうかを見ること。
    """
    sector_map = {}
    try:
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                code = normalize_code(row.get("証券コード", ""))
                sector = (row.get("33業種区分") or "").strip()
                if code and sector:
                    sector_map[code] = sector
    except (OSError, csv.Error) as e:
        logging.error(f"33業種の対応表を読めませんでした: {path}: {e}")
        return {}
    return sector_map


def sector_of(code, sector_map):
    """証券コードの33業種。対応表に無ければ UNKNOWN_SECTOR。"""
    return sector_map.get(normalize_code(code), UNKNOWN_SECTOR)


def parse_jpx_listing(content):
    """
    data_j.xlsx（または .xls）のバイト列を CSV 行のリストにする。

    ETF・REIT など 33業種区分が "-" の行は落とす（業種を持たない）。
    """
    import pandas as pd  # cron 以外（テスト）でも import を軽くするためここで読む

    df = pd.read_excel(BytesIO(content), dtype=str)
    missing = {"コード", "銘柄名", "33業種区分"} - set(df.columns)
    if missing:
        raise ValueError(f"JPX の銘柄一覧に想定の列がありません: {sorted(missing)}")

    rows = []
    for _, r in df.iterrows():
        code = normalize_code(r["コード"])
        sector = str(r["33業種区分"]).strip()
        if not code or sector in ("", "-", "nan"):
            continue
        rows.append(
            {
                "証券コード": code,
                "銘柄名": str(r["銘柄名"]).strip(),
                "33業種区分": sector,
                "データ日付": str(r.get("日付", "")).strip(),
            }
        )
    rows.sort(key=lambda row: row["証券コード"])
    return rows


def _read_csv_rows(path):
    try:
        with open(path, encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def _write_csv_rows(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _download_listing():
    headers = {"User-Agent": USER_AGENT}
    page = requests.get(JPX_LIST_PAGE, headers=headers, timeout=REQUEST_TIMEOUT)
    page.raise_for_status()
    match = _DATA_LINK_RE.search(page.text)
    if not match:
        raise ValueError("JPX の一覧ページに data_j のリンクが見つかりません")
    url = urljoin(JPX_LIST_PAGE, match.group(1))
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.content


def refresh_sector_map(path=SECTOR_CSV):
    """
    JPX から銘柄一覧を取り直し、中身が変わっていれば sector33.csv を書き換える。

    週1回の実行で毎回呼ぶ（230KB 程度）。JPX の更新は月1回なので、実際に
    書き換わるのは月1回。失敗しても例外は投げず、既存CSVで走り続ける。

    Returns:
        list: 業種が変わった銘柄の [(証券コード, 旧業種, 新業種), ...]。
              新規上場・上場廃止の出入りは含めない。取得失敗・変化なしは空リスト。
    """
    try:
        rows = parse_jpx_listing(_download_listing())
    except Exception as e:
        print(f"[warn] 33業種の対応表を取り直せませんでした（既存の sector33.csv で続行）: {e}")
        logging.error(f"33業種の対応表の取得に失敗: {e}")
        return []
    if not rows:
        print("[warn] JPX の銘柄一覧が空でした（既存の sector33.csv で続行）")
        return []

    old_rows = _read_csv_rows(path)
    if old_rows == rows:
        return []

    old_map = {normalize_code(r["証券コード"]): r["33業種区分"] for r in old_rows}
    changed = [
        (r["証券コード"], old_map[r["証券コード"]], r["33業種区分"])
        for r in rows
        if r["証券コード"] in old_map and old_map[r["証券コード"]] != r["33業種区分"]
    ]
    _write_csv_rows(path, rows)
    print(f"[info] 33業種の対応表を更新しました（データ日付 {rows[0]['データ日付']}、{len(rows)}銘柄）")
    return changed
