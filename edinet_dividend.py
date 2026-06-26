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


def _latest_dividends(earnings):
    """
    新しい順のearnings配列から「最新の実績」「最新の予想」を別々に拾う。

    実績と予想は別レコードに散らばっていてよい（本決算は両方持つが、四半期更新は
    予想のみのことが多く、最新の本決算は配当未パースのこともある）。それぞれ独立に
    新しい順で最初のnon-null値を採ることで、期中の予想修正を反映しstale化を防ぐ。

    実績は分割調整後(adjusted_annual_dividend_per_share)を優先し、無ければ生値
    (dividend_per_share)にフォールバックする。

    判定は必ず `is None`（0.0＝無配予想/無配転落をfalsyで欠損扱いしないため）。

    Returns:
        tuple: (actual, forecast, actual_is_adjusted)。
               見つからない側はNone。actual_is_adjustedは調整後実績を採れたか。
    """
    actual = None
    actual_is_adjusted = False
    forecast = None

    for record in earnings:
        if actual is None:
            adjusted = record.get("adjusted_annual_dividend_per_share")
            raw = record.get("dividend_per_share")
            if adjusted is not None:
                actual = adjusted
                actual_is_adjusted = True
            elif raw is not None:
                actual = raw
                actual_is_adjusted = False
        if forecast is None:
            f = record.get("forecast_dividend_per_share")
            if f is not None:
                forecast = f
        if actual is not None and forecast is not None:
            break

    return actual, forecast, actual_is_adjusted


def _is_dividend_cut(edinet_code):
    """
    決算短信(earnings)から、来期配当予想が直近実績より減配かどうかを判定する。

    earnings.dataは新しい順の配列。_latest_dividendsで最新の実績と予想を別々に
    拾い比較する（当期通期予想は直近確定実績の翌期に一致しYoYで整合する）。
    分割調整後の実績を採れた場合はそのまま比較し、生値にフォールバックした場合のみ
    予想が分割後ベースと推定されるか（_looks_like_split）をチェックして誤検知を防ぐ。

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

    actual, forecast, actual_is_adjusted = _latest_dividends(earnings)
    if actual is None or forecast is None:
        return False
    if not actual_is_adjusted and _looks_like_split(actual, forecast):
        return False
    return forecast < actual


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
