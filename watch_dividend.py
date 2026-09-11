import logging
import re
from datetime import datetime, timedelta
from random import uniform
from time import sleep

import pandas as pd
import requests
from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from get_high_dividend_stock_code import get_high_dividend_stock_codes, setup_driver

logging.basicConfig(level=logging.ERROR, filename="error.log")


# 日経の個別銘柄ページは1回の実行で149件叩く。等間隔で連打すると弾かれやすいので、
# 間隔を広めに取ったうえでジッターを足す。
FETCH_INTERVAL = 3.0
FETCH_JITTER = 1.0
# ページごと取れなかったときの粘り（秒）。一時的なブロックはここで復帰する。
RETRY_WAITS = (10, 30, 60)


def _fetch_stock_page(session, url, headers):
    """
    個別銘柄ページを取得し、(会社名, 株価文字列, 配当文字列) を返す。

    ページごと取れなかったときは None を返す（呼び出し側がリトライする）。
    会社名と株価がどちらも取れないことを「ページ取得の失敗」と判定する——
    健全なページはこの2つが必ず取れるので、値の欠損と区別できる。
    """
    try:
        response = session.get(url, headers=headers, timeout=30)
    except requests.RequestException as e:
        logging.error(f"{url} リクエスト失敗: {e}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    name_elm = soup.select_one("h1.m-headlineLarge_text")
    price_elm = soup.select_one("dd.m-stockPriceElm_value")

    if name_elm is None and price_elm is None:
        # 次に同じことが起きたとき、間隔を伸ばせば済む話かを推測でなく判定するための証拠。
        logging.error(
            f"{url} ページ取得失敗: status={response.status_code} "
            f"len={len(response.text)} retry_after={response.headers.get('Retry-After')}"
        )
        return None

    dividend_elm = soup.select_one(
        "div.m-stockInfo_detail_right li:nth-child(3) span.m-stockInfo_detail_value"
    )
    return (
        name_elm.text if name_elm else None,
        price_elm.text if price_elm else None,
        dividend_elm.text if dividend_elm else None,
    )


def calculate_dividend_yield(codes, sector_dict):
    """
    指定された証券コードリストに対して配当利回りを計算し、結果を出力する。

    ページが取れない銘柄は間を置いてリトライし、それでも取れなければ例外を送出する。
    1銘柄でも欠けると利回りランキングが別物になるため、途中で諦めない。
    一方、ページは取れていて配当利回りだけ載っていない銘柄は None のまま通す
    （恒常的に利回りが出ない銘柄が実在するため。利回り None はソートで末尾に行き、
    上位20の候補には入らない）。

    Args:
        codes (list): 証券コードのリスト
        sector_dict (dict): 証券コードとセクターの対応辞書

    Raises:
        RuntimeError: リトライしてもページを取得できなかったとき
    """
    base_url = "https://www.nikkei.com/nkd/company/?scode="
    data = []

    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept-Language": "ja-JP,ja;q=0.9",
        "Referer": "https://www.nikkei.com/",
    }

    for code in codes:
        url = base_url + str(code)

        fetched = _fetch_stock_page(session, url, headers)
        for wait in RETRY_WAITS:
            if fetched is not None:
                break
            print(f"{code}のページを取得できませんでした。{wait}秒後に再試行します。")
            sleep(wait)
            fetched = _fetch_stock_page(session, url, headers)
        if fetched is None:
            raise RuntimeError(
                f"{code}のページを{len(RETRY_WAITS) + 1}回取得できませんでした: {url}"
            )

        company_name, stock_price, dividend = fetched

        try:
            stock_price = float(
                re.search(r"[\d,]+", stock_price).group().replace(",", "")
            )
        except (AttributeError, TypeError) as e:
            today = datetime.now().strftime("%Y-%m-%d")
            logging.error(f"{today}:{code} {company_name} {e}")
            stock_price = None

        try:
            dividend_yield = float(re.search(r"(\d+(\.\d+)?)", dividend).group())
        except (AttributeError, TypeError) as e:
            today = datetime.now().strftime("%Y-%m-%d")
            logging.error(f"{today}:{code} {company_name} {e}")
            dividend_yield = None

        if dividend_yield is None:
            # 恒常欠損なら毎週同じ顔ぶれが並ぶ。見慣れない銘柄が混じったら気づける。
            print(f"利回り欠損: {code} {company_name}")

        sector = sector_dict.get(code, "Unknown")
        data.append(
            {
                "証券コード": code,
                "セクター": sector,
                "配当利回り(%)": dividend_yield,
                "会社名": company_name,
                "株価": stock_price,
                "URL": url,
            }
        )
        sleep(FETCH_INTERVAL + uniform(0, FETCH_JITTER))

    df = pd.DataFrame(data)
    df = df.sort_values(by="配当利回り(%)", ascending=False)
    return df


def create_latest_dividend_dataframe(
    high_dividend_codes, progressive_codes, consecutive_codes, sector_dict
):
    """
    最新の配当データを計算し、データフレームを返す。

    Args:
        high_dividend_codes (list): 高配当株の証券コードリスト
        progressive_codes (list): 累進高配当株の証券コードリスト
        consecutive_codes (list): 連続増配株の証券コードリスト
        sector_dict (dict): 証券コードとセクターの対応辞書

    Returns:
        pd.DataFrame: 全ての配当利回りデータを含むデータフレーム
    """
    df_high_dividend = calculate_dividend_yield(high_dividend_codes, sector_dict)
    df_high_dividend["指数"] = "日経平均高配当株50指数"

    df_progressive = calculate_dividend_yield(progressive_codes, sector_dict)
    df_progressive["指数"] = "日経累進高配当株指数"

    df_consecutive = calculate_dividend_yield(consecutive_codes, sector_dict)
    df_consecutive["指数"] = "日経連続増配株指数"

    df_all = pd.concat(
        [df_high_dividend, df_progressive, df_consecutive], ignore_index=True
    )
    df_all.to_csv(
        "/home/taru-boy/Desktop/get_stock/high_dividend_stocks.csv",
        index=False,
        encoding="utf-8",
    )
    print("配当利回りの計算が完了しました。")
    return df_all


if __name__ == "__main__":
    high_dividend_codes, progressive_codes, consecutive_codes, sector_dict = (
        get_high_dividend_stock_codes()
    )
    create_latest_dividend_dataframe(
        high_dividend_codes, progressive_codes, consecutive_codes, sector_dict
    )
