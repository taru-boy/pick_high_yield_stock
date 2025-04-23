import pandas as pd


def pick_stock_by_yield(df_stocks, held_sector):
    """
    配当利回り上位5銘柄から未保有セクターの銘柄を選定する。
    """
    df_yield = df_stocks.head(5)
    for _, stock in df_yield.iterrows():
        if stock["セクター"] not in held_sector:
            return stock
    return None


def pick_stock_by_duplicates(df_stocks, held_sector):
    """
    複数指数に重複カウントされている銘柄から未保有セクターの銘柄を選定する。
    """
    duplicate_codes = df_stocks["証券コード"].value_counts()
    duplicate_codes = duplicate_codes[duplicate_codes > 1]
    df_duplicates = df_stocks[df_stocks["証券コード"].isin(duplicate_codes.index)]
    df_duplicates = df_duplicates.drop_duplicates(subset=["証券コード"], keep="first")

    for _, stock in df_duplicates.iterrows():
        if stock["セクター"] not in held_sector:
            return stock
    return None


def pick_stock_by_low_market_cap(df_stocks, df_latest_holdings, sector_order):
    """
    時価総額の低いセクターから銘柄を選定する。
    """
    df_yield = df_stocks.head(5)
    duplicate_codes = df_stocks["証券コード"].value_counts()
    duplicate_codes = duplicate_codes[duplicate_codes > 1]
    df_duplicates = df_stocks[df_stocks["証券コード"].isin(duplicate_codes.index)]
    df_duplicates = df_duplicates.drop_duplicates(subset=["証券コード"], keep="first")
    temp_df = pd.concat([df_yield, df_duplicates], ignore_index=True)
    temp_df = df_stocks.drop(columns=["URL", "指数"])
    for sector in sector_order[::-1]:  # 時価総額の低い順にセクターをループ
        if sector in temp_df["セクター"].values:
            sector_stocks = temp_df[temp_df["セクター"] == sector]
            for _, stock in sector_stocks.iterrows():
                if stock["証券コード"] not in df_latest_holdings["証券コード"].values:
                    return stock

            # 未保有銘柄がない場合、時価総額の最も低い銘柄を選択
            matching_rows = df_latest_holdings[
                df_latest_holdings["証券コード"].isin(sector_stocks["証券コード"])
            ]
            if not matching_rows.empty:
                return matching_rows.sort_values(by="時価総額").iloc[0]
    return None


def select_stock(df_stocks, df_latest_holdings, held_sector, sector_order):
    """
    銘柄選定のメイン処理。
    """
    # 配当利回り上位5銘柄から選定
    stock = pick_stock_by_yield(df_stocks, held_sector)
    if stock is not None:
        return stock

    # 複数指数に重複カウントされている銘柄から選定
    stock = pick_stock_by_duplicates(df_stocks, held_sector)
    if stock is not None:
        return stock

    # 時価総額の低いセクターから選定
    stock = pick_stock_by_low_market_cap(df_stocks, df_latest_holdings, sector_order)
    if stock is not None:
        return stock

    return None
