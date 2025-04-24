import logging
import re
from datetime import datetime, timedelta
from time import sleep

import pandas as pd
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from get_high_dividend_stock_code import get_high_dividend_stock_codes, setup_driver

logging.basicConfig(level=logging.ERROR, filename="error.log")


def calculate_dividend_yield(codes, sector_dict):
    """
    指定された証券コードリストに対して配当利回りを計算し、結果を出力する。

    Args:
        codes (list): 証券コードのリスト
        sector_dict (dict): 証券コードとセクターの対応辞書
    """
    driver = setup_driver()
    base_url = "https://www.nikkei.com/nkd/company/?scode="
    data = []

    try:
        for code in codes:
            url = base_url + str(code)
            driver.get(url)

            # 動的な要素の待機
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "h1.m-headlineLarge_text")
                )
            )

            company_name = driver.find_element(
                By.CSS_SELECTOR, "h1.m-headlineLarge_text"
            ).text
            stock_price = driver.find_element(
                By.CSS_SELECTOR, "dd.m-stockPriceElm_value"
            ).text
            try:
                stock_price = float(
                    re.search(r"[\d,]+", stock_price).group().replace(",", "")
                )
            except AttributeError as e:
                today = datetime.now().strftime("%Y-%m-%d")
                logging.error(f"{today}:{code} {company_name} {e}")
                stock_price = None
            dividend_element = driver.find_element(
                By.CSS_SELECTOR,
                "div.m-stockInfo_detail_right li:nth-child(3) span.m-stockInfo_detail_value",
            )
            try:
                dividend_yield = float(
                    re.search(r"(\d+(\.\d+)?)", dividend_element.text).group()
                )
            except AttributeError as e:
                today = datetime.now().strftime("%Y-%m-%d")
                logging.error(f"{today}:{code} {company_name} {e}")
                dividend_yield = None

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
    finally:
        driver.quit()

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
