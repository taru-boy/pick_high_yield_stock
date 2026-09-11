import json
import math
import os
import time
from datetime import datetime

import gspread
import pandas as pd
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

# 高配当株のコードを取得する関数をインポート
from get_high_dividend_stock_code import get_high_dividend_stock_codes

# 保有銘柄の計算関数をインポート
from holding_calculator import calculate_latest_holdings, get_holding_sector_dict

# 銘柄選定関数をインポート
from stock_selector import candidate_codes, select_stocks

# 減配フィルタ（EDINET DB）をインポート
from edinet_dividend import build_code_map, get_dividend_cut_codes, get_dividend_reductions

# 買付不可銘柄の除外リストをインポート
from excluded_stocks import load_excluded_codes

# 株式分割の補正をインポート
from stock_splits import (
    describe_updates,
    load_splits,
    plan_history_adjustments,
    refresh_splits_from_yfinance,
)

# LINE通知関数をインポート
from line_notify import send_line

# 最新の配当データフレームを作成する関数をインポート
from watch_dividend import calculate_dividend_yield, create_latest_dividend_dataframe

start_time = time.time()

# 環境変数を読み込む
load_dotenv(dotenv_path="/home/taru-boy/Desktop/get_stock/.env")

# スプレッドシートのキーを環境変数から取得
spreadsheet_key = os.getenv("SPREADSHEET_KEY")

# Google Sheets APIとGoogle Drive APIのスコープを設定
scope = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# サービスアカウントのJSONファイルパスを環境変数から取得
json_file = os.getenv("SERVICE_ACCOUNT_JSON")

# サービスアカウントの認証情報を作成
credentials = Credentials.from_service_account_file(json_file, scopes=scope)

# gspreadを使用してGoogle Sheets APIに認証
gc = gspread.authorize(credentials)


def apply_split_adjustments(worksheet, data):
    """
    「購入履歴」シートに株式分割・併合を反映し、反映後のデータを返す。

    購入履歴の取得単価・株数は約定時点の生の値なので、放っておくと分割のたびに
    合計株数→時価総額→総時価総額→銘柄選定の上限判定まで芋づるでズレる。
    ここで元データを直しておけば、下流（holding_calculator も note_report も）は
    シートを読むだけなので自動的に正しくなる。

    stock_splits.csv が真実の源。yfinance での取得はベストエフォートで、
    レート制限で落ちても既存CSVぶんの補正は効き続ける。
    G列の適用済みマーカーで二重適用を防ぐので、何度実行しても安全。

    SPLIT_DRY_RUN=1 を立てると差分の表示だけで書き込まない。

    Args:
        worksheet: gspread の「購入履歴」ワークシート
        data (list): worksheet.get_all_values() の戻り

    Returns:
        list: 補正後の get_all_values() 相当（補正が無ければ引数そのまま）
    """
    dry_run = os.getenv("SPLIT_DRY_RUN") == "1"
    if not data or len(data) < 2:
        return data

    header = data[0]
    try:
        code_col, date_col = header.index("証券コード"), header.index("日付")
    except ValueError:
        print("[warn] 購入履歴の列が想定と違うため分割補正をスキップします")
        return data

    # 銘柄ごとの初回買付日。これより前の分割は補正対象にならないので取りに行かない。
    since_by_code = {}
    for row in data[1:]:
        if len(row) <= max(code_col, date_col):
            continue
        code, bought = row[code_col].strip(), row[date_col].strip()
        if not code or not bought:
            continue
        if code not in since_by_code or bought < since_by_code[code]:
            since_by_code[code] = bought

    new_splits = refresh_splits_from_yfinance(
        since_by_code.keys(), since_by_code=since_by_code
    )
    if new_splits:
        print(f"[info] 分割マスタに {len(new_splits)} 件追加しました: {new_splits}")

    updates, warnings = plan_history_adjustments(data, load_splits())
    for w in warnings:
        print(f"[warn] {w}")
    if not updates:
        if warnings:
            send_line("【分割の要確認】\n" + "\n".join(warnings))
        return data

    lines = describe_updates(updates)
    print("[info] 購入履歴に分割を反映します:\n" + "\n".join(lines))
    if dry_run:
        print("[info] SPLIT_DRY_RUN=1 のため書き込みはしません")
        return data

    worksheet.batch_update(
        [{"range": a1, "values": [[value]]} for u in updates for a1, value in u["cells"]]
    )
    send_line("【購入履歴に株式分割を反映しました】\n" + "\n".join(lines + warnings))
    return worksheet.get_all_values()


def update_worksheet_with_holdings(gc, spreadsheet_key, df_latest_holdings):
    """
    並べ替えた保有銘柄データを「時価総額」シートに書き込む。
    """
    worksheet = gc.open_by_key(spreadsheet_key).worksheet("時価総額")
    worksheet.clear()
    worksheet.update(
        values=[df_latest_holdings.columns.to_list()]
        + df_latest_holdings.values.tolist(),
        range_name="A1",
    )


# 「購入履歴」シートを開き、データを取得
worksheet = gc.open_by_key(spreadsheet_key).worksheet("購入履歴")
data = worksheet.get_all_values()

# 株式分割・併合を購入履歴に反映してから集計する（下流はこのシートしか見ない）
data = apply_split_adjustments(worksheet, data)

# データをPandasデータフレームに変換
df_holding = pd.DataFrame(data[1:], columns=data[0])
df_latest_holdings = pd.DataFrame()
sector_order = []

if not df_holding.empty:
    # データ型を適切に変換（数値型に変換可能な列を変換）
    df_holding["証券コード"] = pd.to_numeric(df_holding["証券コード"], errors="coerce")
    df_holding["取得単価"] = pd.to_numeric(df_holding["取得単価"], errors="coerce")
    df_holding["株数"] = pd.to_numeric(df_holding["株数"], errors="coerce")

    # 証券コードごとに保有株数を集計
    df_holding_number = df_holding.groupby("証券コード", as_index=False)["株数"].sum()

    # 保有銘柄のセクター辞書を作成
    codes = list(df_holding["証券コード"].unique())
    holding_sector_dict = get_holding_sector_dict(df_holding, codes)

    # 最新の保有銘柄データを計算
    df_latest_holdings, sector_order = calculate_latest_holdings(
        df_holding, df_holding_number, codes, holding_sector_dict
    )

    # 並べ替えたデータを「時価総額」シートに書き込む
    update_worksheet_with_holdings(gc, spreadsheet_key, df_latest_holdings)

    # 全保有銘柄の総年間配当を計算して「配当推移」シートに追記
    total_annual_div = round(
        (
            df_latest_holdings["合計株数"]
            * df_latest_holdings["株価"]
            * df_latest_holdings["配当利回り(%)"]
            / 100
        ).sum()
    )
    total_market_cap = int(df_latest_holdings["時価総額"].sum())
    record_date = datetime.today().strftime("%Y-%m-%d")
    try:
        ws_trend = gc.open_by_key(spreadsheet_key).worksheet("配当推移")
    except gspread.exceptions.WorksheetNotFound:
        sp = gc.open_by_key(spreadsheet_key)
        ws_trend = sp.add_worksheet(title="配当推移", rows=500, cols=3)
        ws_trend.append_row(["日付", "総年間配当(円)", "総時価総額(円)"])
    ws_trend.append_row([record_date, total_annual_div, total_market_cap])


# 最新の配当データを取得し、データフレームを作成
high_dividend_codes, progressive_codes, consecutive_codes, sector_dict = (
    get_high_dividend_stock_codes()
)
df_stocks = create_latest_dividend_dataframe(
    high_dividend_codes, progressive_codes, consecutive_codes, sector_dict
)

# df_stocks = pd.read_csv("/home/taru-boy/Desktop/get_stock/high_dividend_stocks.csv")
df_stocks.sort_values(by="配当利回り(%)", ascending=False, inplace=True)

# 並べ替えたデータを「今週の銘柄」シートに書き込む
worksheet = gc.open_by_key(spreadsheet_key).worksheet("今週の銘柄")
worksheet.clear()
worksheet.update(
    values=[df_stocks.columns.to_list()] + df_stocks.fillna("").values.tolist(),
    range_name="A1",
)

held_sector = df_holding["セクター"].unique()

# 買付不可銘柄は候補から先に落とす（EDINETの無料枠を消費しない）
excluded_codes = load_excluded_codes()
all_codes = candidate_codes(df_stocks)
codes = [c for c in all_codes if str(c) not in excluded_codes]
hit_excluded = sorted(excluded_codes & set(str(c) for c in all_codes))
if hit_excluded:
    print(f"買付不可のため除外: {hit_excluded}")

# 証券コード→EDINETコードの対応表を1回だけ取得し、候補・保有チェックで共有する
code_map = build_code_map()

# 候補集合の来期減配予想銘柄を取得（候補集合のみ叩いてレート節約）
dividend_cut_codes = get_dividend_cut_codes(codes, code_map=code_map)
if dividend_cut_codes:
    print(f"減配予想のため除外: {sorted(dividend_cut_codes)}")
cut_codes = dividend_cut_codes | excluded_codes

# 保有銘柄の減配予想を検知する（性向条件なし・通知のみ。売る/持つの判断は人間が行う）
held_codes = (
    list(df_latest_holdings["証券コード"].unique())
    if not df_latest_holdings.empty
    else []
)
held_reductions = get_dividend_reductions(held_codes, code_map=code_map)
if held_reductions:
    print(f"保有銘柄の減配予想: {held_reductions}")
held_name_map = (
    dict(zip(df_latest_holdings["証券コード"].astype(str), df_latest_holdings["会社名"]))
    if not df_latest_holdings.empty
    else {}
)

# 銘柄選定（2銘柄）
picked_stocks = select_stocks(df_stocks, df_latest_holdings, held_sector, cut_codes, n=2)

# 選定の裏側（なぜこの銘柄か・何を除外したか・保有銘柄の減配予想）は、これまで
# print と LINE にしか出ておらず、週次レポートの所感からは見えなかった。
# note_report.py が素材メモに載せられるよう JSON に落とす。
PICK_META_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "last_pick_meta.json"
)
picked_meta = []

warning_lines = []
if held_reductions:
    warning_lines.append("⚠️保有銘柄の減配予想")
    for code_str, (actual, forecast) in sorted(held_reductions.items()):
        name = held_name_map.get(code_str, code_str)
        warning_lines.append(f"・{name} ({code_str}): {actual:.0f}円 → {forecast:.0f}円")
    warning_lines.append("")

if picked_stocks:
    today = datetime.today().strftime("%Y-%m-%d")
    worksheet = gc.open_by_key(spreadsheet_key).worksheet("購入履歴")
    circled = "①②③④⑤⑥⑦⑧⑨⑩"
    message_lines = [f"今週の高配当銘柄 ({today})", ""]
    for i, picked_stock in enumerate(picked_stocks):
        picked_code = int(picked_stock["証券コード"])
        picked_name = picked_stock["会社名"]
        picked_sector = picked_stock["セクター"]
        picked_price = picked_stock["株価"]
        picked_yield = picked_stock["配当利回り(%)"]

        # 購入履歴に追加（1万円以上になるよう株数を切り上げ）
        amount = math.ceil(10000 / picked_price)
        worksheet.append_row(
            [
                str(today),
                picked_code,
                str(picked_name),
                str(picked_sector),
                float(picked_price),
                int(amount),
            ]
        )

        picked_meta.append(
            {
                "証券コード": str(picked_code),
                "会社名": str(picked_name),
                "セクター": str(picked_sector),
                "株価": float(picked_price),
                "配当利回り(%)": float(picked_yield),
                "株数": int(amount),
                "選定理由": str(picked_stock.get("選定理由", "")),
            }
        )

        # LINE通知用のメッセージを組み立てる
        mark = circled[i] if i < len(circled) else f"{i + 1}."
        message_lines.append(f"{mark}{picked_name} ({picked_code})")
        message_lines.append(
            f" {picked_sector} / 利回り{picked_yield}% / "
            f"{picked_price:,.0f}円 / {amount}株"
        )
        message_lines.append("")
    print(f"{len(picked_stocks)}銘柄を購入履歴に追記しました。")

    # 減配警告があれば選定結果メッセージの先頭に載せて1回のpushにまとめる
    message_lines = warning_lines + message_lines

    # 選定結果をLINEに通知（失敗してもスクリプトは止めない）
    if send_line("\n".join(message_lines).strip()):
        print("LINEに選定結果を通知しました。")
    else:
        print("LINE通知に失敗しました。")
elif warning_lines:
    print("適切な銘柄が見つかりませんでした。")
    if send_line("\n".join(warning_lines).strip()):
        print("LINEに減配警告を通知しました。")
    else:
        print("LINE通知に失敗しました。")
else:
    print("適切な銘柄が見つかりませんでした。")

# 素材メモ用のメタ情報を書き出す（買付ゼロの週も、減配予想や除外は素材になるので残す）。
# 失敗してもレポート本体には影響しないので、ここで止めない。
try:
    with open(PICK_META_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "日付": datetime.today().strftime("%Y-%m-%d"),
                "買付": picked_meta,
                "保有銘柄の減配予想": {
                    code: {"実績": float(actual), "予想": float(forecast),
                           "会社名": held_name_map.get(code, code)}
                    for code, (actual, forecast) in held_reductions.items()
                },
                "減配予想で候補から除外": sorted(dividend_cut_codes),
                "買付不可で候補から除外": hit_excluded,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"選定メタ情報を書き出しました: {PICK_META_PATH}")
except Exception as e:
    print(f"選定メタ情報の書き出しに失敗しました: {e}")

end_time = time.time()
execution_time = end_time - start_time
print(f"スクリプトの実行時間: {execution_time:.2f}秒")
