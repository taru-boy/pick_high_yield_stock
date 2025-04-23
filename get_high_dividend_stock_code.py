import logging
from time import sleep

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


def setup_driver(chromedriver_path="/usr/bin/chromedriver"):
    """Selenium WebDriverをセットアップして返す"""
    options = Options()
    options.add_argument("--headless")
    service = ChromeService(chromedriver_path)
    driver = webdriver.Chrome(service=service, options=options)
    return driver


def extract_stock_codes(driver, url):
    """
    指定されたURLから証券コードとセクター情報を取得する。

    Args:
        driver: Selenium WebDriverオブジェクト
        url: データを取得する対象のURL

    Returns:
        codes: 証券コードのリスト
        sector_dict: 証券コードをキー、セクター名を値とする辞書
    """
    driver.get(url)

    # ページの読み込みを待機
    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located(
            (By.CSS_SELECTOR, "div.idx-index-components.table-responsive-md")
        )
    )

    codes = []
    sector_dict = {}

    try:
        # テーブルデータを取得
        sector_rows = driver.find_elements(
            By.CSS_SELECTOR,
            "div.idx-index-components.table-responsive-md",
        )
        for row in sector_rows:
            sector = row.find_element(By.CSS_SELECTOR, "h3.idx-section-subheading")
            tickers = row.find_elements(By.TAG_NAME, "tr")
            for ticker in tickers:
                cells = ticker.find_elements(By.TAG_NAME, "td")
                if cells:
                    code = cells[0].text.strip()
                    if code.isdigit():
                        codes.append(code)
                        sector_dict[code] = sector.text.strip()
    except Exception as e:
        logging.error(f"Error extracting stock codes from {url}: {e}")

    return codes, sector_dict


def get_high_dividend_stock_codes():
    """
    高配当株、累進配当株、連続増配株の証券コードとセクター情報を取得する。

    Returns:
        tuple: 以下の4つの要素を含むタプル
            - high_dividend_codes (list): 高配当株の証券コードリスト
            - progressive_codes (list): 累進配当株の証券コードリスト
            - consecutive_codes (list): 連続増配株の証券コードリスト
            - sector_dict (dict): 証券コードをキー、セクター名を値とする辞書
    """
    driver = setup_driver()

    try:
        # 高配当株のデータを取得
        high_dividend_url = (
            "https://indexes.nikkei.co.jp/nkave/index/component?idx=nk225hdy"
        )
        high_dividend_codes, sector_dict = extract_stock_codes(
            driver, high_dividend_url
        )
        # 累進配当株のデータを取得
        progressive_url = "https://indexes.nikkei.co.jp/nkave/index/component?idx=nkphd"
        progressive_codes, progressive_sector_dict = extract_stock_codes(
            driver, progressive_url
        )

        # sector_dictとprogressive_sector_dictをマージ
        sector_dict.update(progressive_sector_dict)

        # 連続増配株のデータを取得
        consecutive_url = "https://indexes.nikkei.co.jp/nkave/index/component?idx=nkcdg"
        consecutive_codes, consecutive_sector_dict = extract_stock_codes(
            driver, consecutive_url
        )

        # sector_dictとconsecutive_sector_dictをマージ
        sector_dict.update(consecutive_sector_dict)

        return high_dividend_codes, progressive_codes, consecutive_codes, sector_dict

    finally:
        driver.quit()


# このスクリプトが直接実行された場合のみ動作
if __name__ == "__main__":
    high_dividend, progressive, consecutive, sectors = get_high_dividend_stock_codes()
    print("高配当株:", high_dividend)
    print("累進配当株:", progressive)
    print("連続増配株:", consecutive)
    print("セクター情報:", sectors)
