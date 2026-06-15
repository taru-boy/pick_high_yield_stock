import logging
import os

import requests
from dotenv import load_dotenv

logging.basicConfig(level=logging.ERROR, filename="error.log")

load_dotenv(dotenv_path="/home/taru-boy/Desktop/get_stock/.env")

BASE_URL = "https://edinetdb.jp/v1"
API_KEY = os.getenv("EDINETDB_API_KEY")
TIMEOUT = 20


def _headers():
    return {"X-API-Key": API_KEY}


def build_code_map():
    """
    EDINET DBの全企業一覧を1リクエストで取得し、証券コード→EDINETコードの辞書を返す。

    sec_codeは「4桁ティッカー+0」の5桁文字列（例: 三菱商事 8058 -> "80580"）。
    本システムの証券コードは4桁なので、4桁キーで引けるようにする。

    Returns:
        dict: {"8058": "E02529", ...} 形式。取得失敗時は空辞書。
    """
    try:
        r = requests.get(
            f"{BASE_URL}/companies",
            headers=_headers(),
            params={"per_page": 5000},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json().get("data", [])
    except (requests.RequestException, ValueError) as e:
        logging.error(f"EDINET build_code_map failed: {e}")
        return {}

    code_map = {}
    for row in rows:
        sec_code = row.get("sec_code")
        edinet_code = row.get("edinet_code")
        if not sec_code or not edinet_code:
            continue
        # 5桁sec_code("80580")の先頭4桁を4桁ティッカーのキーにする
        code_map[str(sec_code)[:4]] = edinet_code
    return code_map


# 一般的な株式分割比（forecast/actual がこれに近い場合は分割の可能性が高い）。
# 予想配当が分割後ベースで開示されると raw 比較で誤って減配判定するため除外する。
_SPLIT_RATIOS = (1 / 2, 1 / 3, 1 / 4, 1 / 5, 1 / 10)
_SPLIT_TOLERANCE = 0.04


def _looks_like_split(actual, forecast):
    """
    forecast/actual が一般的な分割比に近いなら、減配ではなく株式分割と見なす。
    earnings には分割後ベースの予想配当が入ることがあり（adjusted forecastは無い）、
    その場合 raw 比較すると実際は増配でも減配と誤判定するため。
    """
    if actual <= 0:
        return False
    ratio = forecast / actual
    return any(abs(ratio - r) / r <= _SPLIT_TOLERANCE for r in _SPLIT_RATIOS)


def _is_dividend_cut(edinet_code):
    """
    決算短信(earnings)から、来期配当予想が直近実績より減配かどうかを判定する。

    earnings.dataは新しい順の配列。実績(dividend_per_share)と予想
    (forecast_dividend_per_share)が両方non-nullの最新レコードで比較する。
    予想が分割後ベースと推定される場合（_looks_like_split）は減配としない。

    Returns:
        bool: 減配ならTrue。判定不能・未開示・分割推定・エラー時はFalse（fail-open）。
    """
    try:
        r = requests.get(
            f"{BASE_URL}/companies/{edinet_code}/earnings",
            headers=_headers(),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        earnings = r.json().get("data", {}).get("earnings", [])
    except (requests.RequestException, ValueError) as e:
        logging.error(f"EDINET earnings fetch failed for {edinet_code}: {e}")
        return False

    for record in earnings:
        actual = record.get("dividend_per_share")
        forecast = record.get("forecast_dividend_per_share")
        if actual is None or forecast is None:
            continue
        if _looks_like_split(actual, forecast):
            return False
        return forecast < actual
    return False


def get_dividend_cut_codes(codes):
    """
    指定証券コードのうち、来期配当予想が減配の銘柄コードのset（文字列）を返す。

    選定アルゴリズムが実際に評価する候補集合のみを渡すことを想定（レート節約）。
    コード未解決・APIエラー・予想未開示の銘柄は除外せずスキップする（fail-open）。

    Args:
        codes (list): 証券コードのリスト（int/str混在可）

    Returns:
        set: 減配と判定された証券コードの集合（str）
    """
    if not API_KEY:
        logging.error("EDINET get_dividend_cut_codes skipped: EDINETDB_API_KEY未設定")
        return set()

    code_map = build_code_map()
    if not code_map:
        return set()

    cut_codes = set()
    for code in codes:
        code_str = str(code)
        edinet_code = code_map.get(code_str)
        if edinet_code is None:
            logging.error(f"EDINET code unresolved: {code_str}")
            continue
        if _is_dividend_cut(edinet_code):
            cut_codes.add(code_str)
    return cut_codes
