import pandas as pd


def candidate_codes(df_stocks):
    """
    選定アルゴリズムが実際に評価する候補銘柄の証券コードを返す。

    「利回り上位20→重複排除→上位10」と「複数指数の重複銘柄」の和集合（重複排除）。
    減配チェックのAPIリクエストをこの候補集合に限定するために使う。

    Returns:
        list: 証券コードのリスト（重複排除済み）
    """
    df_yield = df_stocks.head(20)
    df_yield = df_yield.drop_duplicates(subset=["証券コード"], keep="first")
    df_yield = df_yield.head(10)

    duplicate_codes = df_stocks["証券コード"].value_counts()
    duplicate_codes = duplicate_codes[duplicate_codes > 1]

    codes = pd.concat(
        [df_yield["証券コード"], pd.Series(duplicate_codes.index)], ignore_index=True
    )
    return list(codes.drop_duplicates())


def pick_stock_by_yield(df_stocks, held_sector, cut_codes=frozenset()):
    """
    重複銘柄も考慮して、配当利回り上位10銘柄から未保有セクターの銘柄を選定する。
    来期減配予想の銘柄は除外する。
    """
    df_yield = df_stocks.head(20)
    df_yield = df_yield.drop_duplicates(subset=["証券コード"], keep="first")
    df_yield = df_yield.head(10)
    for _, stock in df_yield.iterrows():
        if str(stock["証券コード"]) in cut_codes:
            continue
        if stock["セクター"] not in held_sector:
            return stock
    return None


def pick_stock_by_duplicates(df_stocks, held_sector, cut_codes=frozenset()):
    """
    複数指数に重複カウントされている銘柄から未保有セクターの銘柄を選定する。
    来期減配予想の銘柄は除外する。
    """
    duplicate_codes = df_stocks["証券コード"].value_counts()
    duplicate_codes = duplicate_codes[duplicate_codes > 1]
    df_duplicates = df_stocks[df_stocks["証券コード"].isin(duplicate_codes.index)]
    df_duplicates = df_duplicates.drop_duplicates(subset=["証券コード"], keep="first")

    for _, stock in df_duplicates.iterrows():
        if str(stock["証券コード"]) in cut_codes:
            continue
        if stock["セクター"] not in held_sector:
            return stock
    return None


def pick_stock_in_holding_sector(df_stocks, df_latest_holdings, cut_codes=frozenset()):
    """
    対象銘柄の保有比率が高すぎない範囲で高配当銘柄を選定する。
    来期減配予想の銘柄は除外する。
    """
    df_yield = df_stocks.head(20)
    df_yield = df_yield.drop_duplicates(subset=["証券コード"], keep="first")
    df_yield = df_yield.head(10)
    duplicate_codes = df_stocks["証券コード"].value_counts()
    duplicate_codes = duplicate_codes[duplicate_codes > 1]
    df_duplicates = df_stocks[df_stocks["証券コード"].isin(duplicate_codes.index)]
    df_duplicates = df_duplicates.drop_duplicates(subset=["証券コード"], keep="first")
    temp_df = pd.concat([df_yield, df_duplicates], ignore_index=True)
    temp_df = temp_df.drop(columns=["URL", "指数"])
    temp_df = temp_df.drop_duplicates(subset=["証券コード"], keep="first")
    total_cap = df_latest_holdings["時価総額"].sum()
    for _, stock in temp_df.iterrows():
        sector = stock["セクター"]
        code = stock["証券コード"]
        if str(code) in cut_codes:
            continue
        stock_cap = df_latest_holdings[
            df_latest_holdings["証券コード"].astype(str) == str(code)
        ]["時価総額"].sum()
        if stock_cap > total_cap * 0.04:
            continue
        sector_stocks = df_latest_holdings[df_latest_holdings["セクター"] == sector]
        sector_cap = sector_stocks["時価総額"].sum()
        if sector_cap < total_cap * 0.2:
            return stock
    return None


def select_stock(df_stocks, df_latest_holdings, held_sector, cut_codes=frozenset()):
    """
    銘柄選定のメイン処理。
    cut_codes に含まれる証券コード（来期減配予想）は全段階で除外する。
    """
    # 配当利回り上位10銘柄から選定
    stock = pick_stock_by_yield(df_stocks, held_sector, cut_codes)
    if stock is not None:
        return stock

    # 複数指数に重複カウントされている銘柄から選定
    stock = pick_stock_by_duplicates(df_stocks, held_sector, cut_codes)
    if stock is not None:
        return stock

    # 保有済みセクターから高配当銘柄を選定
    stock = pick_stock_in_holding_sector(df_stocks, df_latest_holdings, cut_codes)
    if stock is not None:
        return stock

    return None


def select_stocks(df_stocks, df_latest_holdings, held_sector,
                  cut_codes=frozenset(), n=2):
    """
    select_stock を反復適用して最大 n 銘柄を選定する。
    各回で選定済みコードを除外集合に、選定済みセクターを保有済みセクターに加え、
    別セクター優先・コード重複回避を既存ロジックのまま実現する。
    """
    picked = []
    held = set(held_sector)
    excluded = set(cut_codes)
    for _ in range(n):
        stock = select_stock(df_stocks, df_latest_holdings, held, excluded)
        if stock is None:
            break
        picked.append(stock)
        excluded.add(str(stock["証券コード"]))
        held.add(stock["セクター"])
    return picked
