"""
業種区分を日経の指数ページの業種（28種）から東証33業種へ移す、一度きりの道具。

    python migrate_sector33.py --report    # 影響を見るだけ（何も書かない）
    SECTOR_DRY_RUN=1 python migrate_sector33.py --migrate   # 書き換え差分の表示だけ
    python migrate_sector33.py --migrate   # 購入履歴タブの「セクター」列を33業種に書き換える

--migrate は書き換え前に旧値を sector_migration_2026-10.csv に残す（戻せるように）。
計算側（pick_high_yield_stock.py）は購入履歴の列ではなく sector33.csv から業種を
引くので、この書き換えはシートを読む人のため。何度実行しても結果は同じ。
"""

import argparse
import csv
import os
import sys

import gspread
import pandas as pd
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

from excluded_stocks import load_excluded_codes
from note_report import _to_number
from stock_selector import select_stocks
from tse_sector import UNKNOWN_SECTOR, load_sector_map, normalize_code, sector_of

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_CSV = os.path.join(BASE_DIR, "sector_migration_2026-10.csv")
CANDIDATES_CSV = os.path.join(BASE_DIR, "high_dividend_stocks.csv")
CAP_LIMIT = 0.20


def _open_spreadsheet():
    load_dotenv(dotenv_path=os.path.join(BASE_DIR, ".env"))
    credentials = Credentials.from_service_account_file(
        os.getenv("SERVICE_ACCOUNT_JSON"),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    return gspread.authorize(credentials).open_by_key(os.getenv("SPREADSHEET_KEY"))


def _share_table(df, column, total):
    caps = df.groupby(column)["時価総額"].sum().sort_values(ascending=False)
    lines = [f"| {column} | 時価総額 | 構成比 |", "|---|---:|---:|"]
    for name, cap in caps.items():
        mark = " ⚠️" if cap >= total * CAP_LIMIT else ""
        lines.append(f"| {name} | {cap:,.0f}円 | {cap / total * 100:.1f}%{mark} |")
    return lines


def report(spreadsheet, sector_map):
    values = spreadsheet.worksheet("時価総額").get_all_values()
    df = pd.DataFrame(values[1:], columns=values[0])
    df["時価総額"] = df["時価総額"].map(_to_number)
    df = df.rename(columns={"セクター": "日経業種"})
    df["33業種"] = df["証券コード"].map(lambda c: sector_of(c, sector_map))
    total = df["時価総額"].sum()

    out = [f"# 業種区分の切り替え：影響の確認（保有{len(df)}銘柄・時価総額 {total:,.0f}円）", ""]

    unknown = df[df["33業種"] == UNKNOWN_SECTOR]
    if not unknown.empty:
        out += [f"- ⚠️ 33業種が引けない保有銘柄: {unknown['証券コード'].tolist()}", ""]

    out += ["## 銘柄ごとの付け直し（日経業種 → 33業種）", "",
            "| 証券コード | 会社名 | 日経業種 | 33業種 |", "|---|---|---|---|"]
    for _, r in df.sort_values(["33業種", "証券コード"]).iterrows():
        out.append(f"| {r['証券コード']} | {r['会社名']} | {r['日経業種']} | {r['33業種']} |")

    out += ["", f"## 旧：日経業種（{df['日経業種'].nunique()}業種を保有）", ""]
    out += _share_table(df, "日経業種", total)
    out += ["", f"## 新：東証33業種（{df['33業種'].nunique()}業種を保有）", ""]
    out += _share_table(df, "33業種", total)

    all_sectors = set(sector_map.values())
    held = set(df["33業種"])
    out += ["", f"## 未保有になる33業種（{len(all_sectors - held)}業種）", "",
            " / ".join(sorted(all_sectors - held)) or "なし"]

    # 候補（先週の high_dividend_stocks.csv）を33業種で並べ直し、選定がどう変わるかを見る。
    # 減配フィルタ（EDINET）はここでは掛けない（無料枠を使わないため）。買付不可リストだけ効かせる。
    df_stocks = pd.read_csv(CANDIDATES_CSV, dtype={"証券コード": str})
    df_stocks = df_stocks.sort_values("配当利回り(%)", ascending=False)
    df_new = df_stocks.copy()
    df_new["セクター"] = df_new["証券コード"].map(lambda c: sector_of(c, sector_map))
    unheld_candidates = df_new[~df_new["セクター"].isin(held)].drop_duplicates("証券コード")
    out += ["", "## 候補のうち未保有33業種の銘柄（先週の候補リストから）", ""]
    if unheld_candidates.empty:
        out.append("なし")
    else:
        out += ["| 証券コード | 会社名 | 33業種 | 利回り | 指数 |", "|---|---|---|---:|---|"]
        for _, r in unheld_candidates.iterrows():
            out.append(f"| {r['証券コード']} | {r['会社名']} | {r['セクター']} | {r['配当利回り(%)']}% | {r['指数']} |")

    holdings_old = df.rename(columns={"日経業種": "セクター"})
    holdings_new = df.rename(columns={"33業種": "セクター"})
    excluded = load_excluded_codes()
    out += ["", "## 先週の候補で選定を回すと（減配フィルタなし・購入履歴には書かない）", ""]
    for label, stocks, holdings in (
        ("旧（日経業種）", df_stocks, holdings_old),
        ("新（33業種）", df_new, holdings_new),
    ):
        picked = select_stocks(stocks, holdings, set(holdings["セクター"]), excluded, n=2)
        names = " / ".join(
            f"{p['会社名']}（{p['証券コード']}・{p['セクター']}・{p['選定理由']}）" for p in picked
        ) or "なし"
        out.append(f"- {label}: {names}")

    print("\n".join(out))


def migrate(spreadsheet, sector_map, dry_run):
    worksheet = spreadsheet.worksheet("購入履歴")
    data = worksheet.get_all_values()
    header = data[0]
    code_col, name_col, sector_col = (header.index(c) for c in ("証券コード", "会社名", "セクター"))
    col_letter = chr(ord("A") + sector_col)

    updates, backup, unknown = [], {}, set()
    for i, row in enumerate(data[1:], start=2):
        code = normalize_code(row[code_col])
        if not code:
            continue
        new = sector_map.get(code)
        if new is None:
            unknown.add(code)
            continue
        old = row[sector_col]
        backup.setdefault(code, (row[name_col], old, new))
        if old != new:
            updates.append({"range": f"{col_letter}{i}", "values": [[new]]})

    if unknown:
        print(f"[warn] 33業種が引けないので書き換えない銘柄: {sorted(unknown)}")
    print(f"[info] 書き換え {len(updates)} セル（{len(backup)}銘柄）")
    for code, (name, old, new) in sorted(backup.items()):
        if old != new:
            print(f"  {code} {name}: {old} → {new}")
    if dry_run:
        print("[info] SECTOR_DRY_RUN=1 のため書き込みはしません")
        return
    if not updates:
        return

    # 旧値の控えは初回だけ作る（2回目以降は旧値がもう33業種になっているので上書きしない）
    if not os.path.exists(BACKUP_CSV):
        with open(BACKUP_CSV, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["証券コード", "会社名", "日経業種", "33業種"])
            for code, (name, old, new) in sorted(backup.items()):
                writer.writerow([code, name, old, new])
        print(f"[info] 旧値の控えを書き出しました: {BACKUP_CSV}")
    worksheet.batch_update(updates)
    print("[info] 購入履歴タブの「セクター」列を33業種に書き換えました")


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--report", action="store_true", help="影響を見るだけ")
    group.add_argument("--migrate", action="store_true", help="購入履歴タブを書き換える")
    args = parser.parse_args()

    sector_map = load_sector_map()
    if not sector_map:
        sys.exit("sector33.csv が読めません。先に tse_sector.refresh_sector_map() を実行してください")

    spreadsheet = _open_spreadsheet()
    if args.report:
        report(spreadsheet, sector_map)
    else:
        migrate(spreadsheet, sector_map, dry_run=os.getenv("SECTOR_DRY_RUN") == "1")


if __name__ == "__main__":
    main()
