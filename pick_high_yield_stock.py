import math
import os
from datetime import datetime

import gspread
import pandas as pd
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

# 高配当株のコードを取得する関数をインポート
from get_high_dividend_stock_code import get_high_dividend_stock_codes

# 銘柄選定関数をインポート
from stock_selector import select_stock

# 最新の配当データフレームを作成する関数をインポート
from watch_dividend import calculate_dividend_yield, create_latest_dividend_dataframe

# 環境変数を読み込む
load_dotenv(dotenv_path=".env")

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

# 「購入履歴」シートを開き、データを取得
worksheet = gc.open_by_key(spreadsheet_key).worksheet("購入履歴")
data = worksheet.get_all_values()

# データをPandasデータフレームに変換
df_holding = pd.DataFrame(data[1:], columns=data[0])

if not df_holding.empty:
    # データ型を適切に変換（数値型に変換可能な列を変換）
    df_holding["証券コード"] = pd.to_numeric(df_holding["証券コード"], errors="coerce")
    df_holding["取得単価"] = pd.to_numeric(df_holding["取得単価"], errors="coerce")
    df_holding["株数"] = pd.to_numeric(df_holding["株数"], errors="coerce")

    # 証券コードごとに保有株数を集計
    df_holding_number = df_holding.groupby("証券コード", as_index=False)["株数"].sum()

    # 保有銘柄の時価総額を計算するための準備
    codes = list(df_holding["証券コード"].unique())
    holding_sector_dict = {}

    # 各証券コードに対応するセクターを辞書に格納
    for _code in codes:
        _sector = df_holding[df_holding["証券コード"] == _code]["セクター"].values[0]
        holding_sector_dict[_code] = _sector

    # 最新の株価データを取得し、データフレームを作成
    df_latest_holdings = calculate_dividend_yield(
        codes=codes, sector_dict=holding_sector_dict
    )
    df_latest_holdings.drop(columns=["配当利回り(%)", "URL"], inplace=True)
    df_latest_holdings.to_csv("latest_holdings.csv", index=False, encoding="utf-8")
    df_latest_holdings = pd.read_csv("latest_holdings.csv")
    df_latest_holdings["合計株数"] = 0

    # 各銘柄の合計株数を計算し、データフレームに反映
    for _index, _stock in df_latest_holdings.iterrows():
        _code = _stock["証券コード"]
        _matching_row = df_holding_number[df_holding_number["証券コード"] == _code]
        if not _matching_row.empty:
            df_latest_holdings.at[_index, "合計株数"] = _matching_row["株数"].values[0]

    # 各銘柄の時価総額を計算
    df_latest_holdings["時価総額"] = (
        df_latest_holdings["株価"] * df_latest_holdings["合計株数"]
    ).astype(int)

    # セクターごとの合計時価総額を計算
    sector_total_market_cap = (
        df_latest_holdings.groupby("セクター")["時価総額"]
        .sum()
        .sort_values(ascending=False)
    )

    # セクターの順序を時価総額の降順に設定
    sector_order = sector_total_market_cap.index.tolist()

    # セクターごとに並べ替え、セクター内は時価総額の降順に並べ替え
    df_latest_holdings["セクター順序"] = df_latest_holdings["セクター"].apply(
        lambda x: sector_order.index(x)
    )
    df_latest_holdings = df_latest_holdings.sort_values(
        by=["セクター順序", "時価総額"], ascending=[True, False]
    )

    # 並べ替えに使用した列を削除
    df_latest_holdings.drop(columns=["セクター順序"], inplace=True)

    # 並べ替えたデータを「時価総額」シートに書き込む
    worksheet = gc.open_by_key(spreadsheet_key).worksheet("時価総額")
    worksheet.clear()
    worksheet.update(
        values=[df_latest_holdings.columns.to_list()]
        + df_latest_holdings.values.tolist(),
        range_name="A1",
    )


# 最新の配当データを取得し、データフレームを作成
high_dividend_codes, progressive_codes, consecutive_codes, sector_dict = (
    get_high_dividend_stock_codes()
)
df_stocks = create_latest_dividend_dataframe(
    high_dividend_codes, progressive_codes, consecutive_codes, sector_dict
)

df_stocks = pd.read_csv("high_dividend_stocks.csv")
df_stocks.sort_values(by="配当利回り(%)", ascending=False, inplace=True)

# 並べ替えたデータを「今週の銘柄」シートに書き込む
worksheet = gc.open_by_key(spreadsheet_key).worksheet("今週の銘柄")
worksheet.clear()
worksheet.update(
    values=[df_stocks.columns.to_list()] + df_stocks.fillna("").values.tolist(),
    range_name="A1",
)

held_sector = df_holding["セクター"].unique()

# 銘柄選定
picked_stock = select_stock(df_stocks, df_latest_holdings, held_sector, sector_order)

if picked_stock is not None:
    picked_code = picked_stock["証券コード"]
    picked_name = picked_stock["会社名"]
    picked_sector = picked_stock["セクター"]
    picked_price = picked_stock["株価"]

    # 購入履歴に追加
    today = datetime.today().strftime("%Y-%m-%d")
    amount = math.ceil(2000 / picked_price)
    worksheet = gc.open_by_key(spreadsheet_key).worksheet("購入履歴")
    worksheet.append_row(
        [
            str(today),
            int(picked_code),
            str(picked_name),
            str(picked_sector),
            float(picked_price),
            int(amount),
        ]
    )
else:
    print("適切な銘柄が見つかりませんでした。")
