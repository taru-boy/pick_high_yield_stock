import math
import os
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
from stock_selector import select_stock

# 最新の配当データフレームを作成する関数をインポート
from watch_dividend import calculate_dividend_yield, create_latest_dividend_dataframe

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


# 最新の配当データを取得し、データフレームを作成
high_dividend_codes, progressive_codes, consecutive_codes, sector_dict = (
    get_high_dividend_stock_codes()
)
df_stocks = create_latest_dividend_dataframe(
    high_dividend_codes, progressive_codes, consecutive_codes, sector_dict
)

df_stocks = pd.read_csv("/home/taru-boy/Desktop/get_stock/high_dividend_stocks.csv")
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
