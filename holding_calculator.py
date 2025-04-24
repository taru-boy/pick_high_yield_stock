import pandas as pd

from watch_dividend import calculate_dividend_yield


def get_holding_sector_dict(df_holding, codes):
    """
    保有銘柄の証券コードに対応するセクターを辞書に格納する。
    """
    holding_sector_dict = {}
    for _code in codes:
        _sector = df_holding[df_holding["証券コード"] == _code]["セクター"].values[0]
        holding_sector_dict[_code] = _sector
    return holding_sector_dict


def calculate_latest_holdings(
    df_holding, df_holding_number, codes, holding_sector_dict
):
    """
    最新の株価データを取得し、保有銘柄の時価総額やセクター順序を計算する。
    """
    # 最新の株価データを取得
    df_latest_holdings = calculate_dividend_yield(
        codes=codes, sector_dict=holding_sector_dict
    )
    df_latest_holdings.drop(columns=["配当利回り(%)", "URL"], inplace=True)

    # 各銘柄の合計株数を計算
    df_latest_holdings["合計株数"] = 0
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
    df_latest_holdings.drop(columns=["セクター順序"], inplace=True)

    return df_latest_holdings, sector_order
