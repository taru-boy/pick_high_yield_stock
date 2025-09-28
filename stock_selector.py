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


def pick_stock_in_holding_sector(df_stocks, df_latest_holdings):
    """
    対象銘柄の保有比率が高すぎない範囲で高配当銘柄を選定する。
    """
    df_yield = df_stocks.head(5)
    duplicate_codes = df_stocks["証券コード"].value_counts()
    duplicate_codes = duplicate_codes[duplicate_codes > 1]
    df_duplicates = df_stocks[df_stocks["証券コード"].isin(duplicate_codes.index)]
    df_duplicates = df_duplicates.drop_duplicates(subset=["証券コード"], keep="first")
    temp_df = pd.concat([df_yield, df_duplicates], ignore_index=True)
    temp_df = temp_df.drop(columns=["URL", "指数"])
    total_cap = df_latest_holdings["時価総額"].sum()
    for _, stock in temp_df.iterrows():
        sector = stock["セクター"]
        code = stock["証券コード"]
        stock_cap = df_latest_holdings[df_latest_holdings["証券コード"] == code][
            "時価総額"
        ].sum()
        if stock_cap > total_cap * 0.05:
            continue
        sector_stocks = df_latest_holdings[df_latest_holdings["セクター"] == sector]
        sector_cap = sector_stocks["時価総額"].sum()
        if sector_cap < total_cap * 0.2:
            return stock
    return None


def select_stock(df_stocks, df_latest_holdings, held_sector):
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

    # 保有済みセクターから高配当銘柄を選定
    stock = pick_stock_in_holding_sector(df_stocks, df_latest_holdings)
    if stock is not None:
        return stock

    return None
