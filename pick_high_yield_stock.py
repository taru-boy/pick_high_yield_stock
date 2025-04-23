import math
import os
from datetime import datetime

import gspread
import pandas as pd
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

# 高配当株のコードを取得する関数をインポート
from get_high_dividend_stock_code import get_high_dividend_stock_codes

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

picked_code = None
held_sector = df_holding["セクター"].unique()

# 配当利回り上位５銘柄から未保有セクターの銘柄をピックアップ
if picked_code is None:
    df_yield = df_stocks.head(5)
    for _, _stock in df_yield.iterrows():
        sector_name = _stock["セクター"]
        if not sector_name in held_sector:
            picked_sector = sector_name
            picked_code = _stock["証券コード"]
            picked_name = _stock["会社名"]
            picked_price = _stock["株価"]
            break

# 複数指数に重複カウントされている銘柄から未保有セクターの銘柄をピックアップ
if picked_code is None:
    duplicate_codes = df_stocks["証券コード"].value_counts()
    duplicate_codes = duplicate_codes[duplicate_codes > 1]
    df_duplicates = df_stocks[df_stocks["証券コード"].isin(duplicate_codes.index)]
    df_duplicates = df_duplicates.drop_duplicates(subset=["証券コード"], keep="first")

    for _, _stock in df_duplicates.iterrows():
        sector_name = _stock["セクター"]
        if not sector_name in held_sector:
            picked_sector = sector_name
            picked_code = _stock["証券コード"]
            picked_name = _stock["会社名"]
            picked_price = _stock["株価"]
            break

# 時価総額の低いセクターから銘柄をピックアップ
if picked_code is None:
    temp_df = pd.concat([df_yield, df_duplicates], ignore_index=True)
    temp_df = temp_df.drop(columns=["URL", "指数"])
    # 時価総額の低い順にセクターをループ
    for _sector in sector_order[::-1]:
        # 銘柄候補の中にセクターに属する銘柄があるか確認
        if _sector in temp_df["セクター"].values:
            temp_df = temp_df[temp_df["セクター"] == _sector]

            # セクターに属する銘柄候補のうち、未保有の銘柄を選択
            for _, _row in temp_df.iterrows():
                _code = _row["証券コード"]
                if _code not in df_latest_holdings["証券コード"].values:
                    picked_sector = _row["セクター"]
                    picked_code = _code
                    picked_name = _row["会社名"]
                    picked_price = _row["株価"]
                    break

            # セクターに属する銘柄候補のうち、時価総額の最も低い銘柄を選択
            if picked_code is None:
                _matching_rows = df_latest_holdings[
                    df_latest_holdings["証券コード"].isin(temp_df["証券コード"])
                ]
                _matching_row = _matching_rows.sort_values(by="時価総額").iloc[0]
                picked_code = _matching_row["証券コード"]
                picked_name = _matching_row["会社名"]
                picked_sector = _matching_row["セクター"]
                picked_price = _matching_row["株価"]


# 共有設定されたスプレッドシートの「購入履歴」シートを開く
worksheet = gc.open_by_key(spreadsheet_key).worksheet("購入履歴")

# データをPythonの標準型に変換
today = datetime.today().strftime("%Y-%m-%d")
amount = math.ceil(2000 / picked_price)

# すべての値を標準型に変換してappend_rowに渡す
worksheet.append_row(
    [
        str(today),  # 日付を文字列に変換
        int(picked_code),  # 証券コードをint型に変換
        str(picked_name),  # 会社名を文字列に変換
        str(picked_sector),  # セクターを文字列に変換
        float(picked_price),  # 株価をfloat型に変換
        int(amount),  # 数量をint型に変換
    ]
)
