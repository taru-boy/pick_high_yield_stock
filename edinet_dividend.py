import logging
import math
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


def _split_scale_of_actual(adjusted, raw, factor):
    """
    実績配当がどちらの株数基準かを、調整後と生値の比から確定できる範囲で返す。

    adjusted_annual_dividend_per_share は経路依存で、分割の効力発生をまたいだ
    レコードでも未調整のまま（生値と同じ）で返ることがある（8309 の 2026-07-30 Q1 は
    185.0 のまま、同じ分割でも 2026-05-14 Q4 は 46.25 に調整済み）。なので
    「調整後を採れた＝予想と同じ基準」とは言えず、生値÷調整後が分割比に一致した
    ときだけ分割後基準と確定する。

    Returns:
        str | None: "post"（分割後基準と確定）。確定できなければNone。
    """
    if adjusted is None or raw is None or adjusted <= 0:
        return None
    if abs(raw / adjusted - factor) / factor <= _SPLIT_TOLERANCE:
        return "post"
    return None


def _latest_dividends(earnings):
    """
    新しい順のearnings配列から「最新の実績」「最新の予想」を別々に拾う。

    実績と予想は別レコードに散らばっていてよい（本決算は両方持つが、四半期更新は
    予想のみのことが多く、最新の本決算は配当未パースのこともある）。それぞれ独立に
    新しい順で最初のnon-null値を採ることで、期中の予想修正を反映しstale化を防ぐ。

    実績は分割調整後(adjusted_annual_dividend_per_share)を優先し、無ければ生値
    (dividend_per_share)にフォールバックする。

    判定は必ず `is None`（0.0＝無配予想/無配転落をfalsyで欠損扱いしないため）。

    予想配当性向の算定用に、最新の予想EPS(forecast_eps)も同様に新しい順で拾う
    （forecast_eps は forecast_dividend_per_share と同じ予想期のレコードに入る）。

    あわせて、実績・予想を拾ったレコードが持つ分割シグナル
    （forecast_split_adjustment_factor / forecast_share_basis）を拾う。分割比は
    予想側のレコードを優先し、無ければ実績側のレコードのものを使う（分割の効力発生後に
    出た予想には、関係する分割が無いとして null が入るため）。

    Returns:
        tuple: (actual, forecast, actual_is_adjusted, forecast_eps, split)。
               見つからない側はNone。actual_is_adjustedは調整後実績を採れたか。
               splitは分割シグナルが無ければNone、あれば
               {"factor": 分割比, "actual_scale": "post"|None,
                "forecast_scale": "pre"|"post"|None}。
    """
    actual = None
    actual_is_adjusted = False
    actual_record = None
    forecast = None
    forecast_record = None
    forecast_eps = None

    for record in earnings:
        if actual is None:
            adjusted = record.get("adjusted_annual_dividend_per_share")
            raw = record.get("dividend_per_share")
            if adjusted is not None:
                actual = adjusted
                actual_is_adjusted = True
                actual_record = record
            elif raw is not None:
                actual = raw
                actual_is_adjusted = False
                actual_record = record
        if forecast is None:
            f = record.get("forecast_dividend_per_share")
            if f is not None:
                forecast = f
                forecast_record = record
        if forecast_eps is None:
            e = record.get("forecast_eps")
            if e is not None:
                forecast_eps = e
        if actual is not None and forecast is not None and forecast_eps is not None:
            break

    split = None
    factor = None
    for record in (forecast_record, actual_record):
        if record is not None and record.get("forecast_split_adjustment_factor") is not None:
            factor = record.get("forecast_split_adjustment_factor")
            break
    if factor is not None and factor > 0 and factor != 1:
        basis = (
            forecast_record.get("forecast_share_basis")
            if forecast_record is not None
            else None
        )
        split = {
            "factor": factor,
            "actual_scale": (
                _split_scale_of_actual(
                    actual_record.get("adjusted_annual_dividend_per_share"),
                    actual_record.get("dividend_per_share"),
                    factor,
                )
                if actual_is_adjusted
                else None
            ),
            "forecast_scale": {"pre_split": "pre", "post_split": "post"}.get(basis),
        }

    return actual, forecast, actual_is_adjusted, forecast_eps, split


def _align_split_basis(actual, forecast, forecast_eps, split):
    """
    分割シグナルがある銘柄について、実績と予想を同じ株数基準にそろえる。

    基準が確定している側（予想は forecast_share_basis、実績は生値÷調整後の比）は
    それに従い、確定しない側は「同じ基準」「実績だけ分割前」「予想だけ分割前」の
    うち矛盾しない解釈から、予想/実績の比が対数距離で1に最も近いものを採る
    （分割の年に配当が数倍に動くことはまず無い、という前提）。

    実例: 8309 は 2026-08-01 に1:4分割。実績185.0（分割前基準・未調整）と
    予想47.5（分割後基準）を比べて減配と誤判定していた。本関数で 46.25 → 47.5 の
    増配にそろう。

    限界: 基準が確定しない銘柄で、分割と同時に 1/√分割比 を超える減配
    （1:4なら50%超、1:2なら29%超）をすると増配と読み違える。分割の年に大幅減配が
    重なるのはまれで、判定不能なら警告しない（fail-open）方針とも矛盾しない。

    Returns:
        tuple: 基準をそろえた (actual, forecast, forecast_eps)。予想EPSは予想配当と
               同じ係数で割る（性向は変わらない）。
    """
    if actual <= 0 or forecast <= 0:
        # 無配転落(0.0)などは基準によらず比較結果が変わらない
        return actual, forecast, forecast_eps

    factor = split["factor"]
    actual_scale = split["actual_scale"]
    forecast_scale = split["forecast_scale"]

    candidates = []
    # 同じ基準（両方確定して食い違うときだけ除く）。同点なら生値のまま出したいので先頭に置く
    if actual_scale is None or forecast_scale is None or actual_scale == forecast_scale:
        candidates.append((actual, forecast, forecast_eps))
    # 実績だけ分割前基準
    if actual_scale in (None, "pre") and forecast_scale in (None, "post"):
        candidates.append((actual / factor, forecast, forecast_eps))
    # 予想だけ分割前基準
    if actual_scale in (None, "post") and forecast_scale in (None, "pre"):
        candidates.append(
            (
                actual,
                forecast / factor,
                forecast_eps / factor if forecast_eps is not None else None,
            )
        )

    return min(candidates, key=lambda c: abs(math.log(c[1] / c[0])))


def _exceeds_full_payout(forecast_dividend, forecast_eps):
    """
    予想配当が予想EPSで賄えない（予想配当性向>100%）かを判定する。

    予想EPS未開示(None)は判定不能としてFalse（fail-open）。
    予想EPS<=0（赤字予想）で配当が正なら、利益で配当を賄えないため100%超とみなす。
    """
    if forecast_eps is None:
        return False
    if forecast_eps <= 0:
        return forecast_dividend > 0
    return forecast_dividend / forecast_eps > 1.0


def _fetch_earnings(edinet_code):
    """
    決算短信(earnings)配列を取得する。

    Returns:
        list | None: earnings配列（新しい順）。APIエラー時はNone（fail-open）。
    """
    try:
        r = requests.get(
            f"{BASE_URL}/companies/{edinet_code}/earnings",
            headers=_headers(),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("data", {}).get("earnings", [])
    except (requests.RequestException, ValueError) as e:
        logging.error(f"EDINET earnings fetch failed for {edinet_code}: {e}")
        return None


def _latest_reduction(edinet_code):
    """
    決算短信(earnings)から、来期予想が減配(forecast < actual)かを判定する。

    _latest_dividendsで最新の実績と予想を別々に拾い比較する（当期通期予想は
    直近確定実績の翌期に一致しYoYで整合する）。EDINET側に分割シグナルがあれば
    _align_split_basisで実績と予想の株数基準をそろえてから比較する。シグナルが
    無い銘柄は、生値にフォールバックした場合のみ予想が分割後ベースと推定
    されるか（_looks_like_split）をチェックして誤検知を防ぐ。

    Returns:
        tuple | None: 減配なら (actual, forecast, forecast_eps)（分割シグナルが
                      あれば基準をそろえた値）。
                      判定不能・未開示・分割推定・エラー時はNone（fail-open）。
    """
    earnings = _fetch_earnings(edinet_code)
    if earnings is None:
        return None

    actual, forecast, actual_is_adjusted, forecast_eps, split = _latest_dividends(
        earnings
    )
    if actual is None or forecast is None:
        return None
    if split is not None:
        actual, forecast, forecast_eps = _align_split_basis(
            actual, forecast, forecast_eps, split
        )
    elif not actual_is_adjusted and _looks_like_split(actual, forecast):
        return None
    if not forecast < actual:
        return None
    return actual, forecast, forecast_eps


def _is_dividend_cut(edinet_code):
    """
    決算短信(earnings)から、来期予想が減配かつ予想配当性向>100%かを判定する。

    除外は「減配(forecast < actual)」かつ「予想配当性向>100%（下げた後でも利益で
    配当を賄えない）」の両方を満たす場合のみ。市況ピークからの正常化や下限着地など、
    減配でも配当が利益でカバーできている銘柄は除外しない。

    Returns:
        bool: 減配かつ性向>100%ならTrue。判定不能・未開示・分割推定・エラー時は
              False（fail-open）。
    """
    reduction = _latest_reduction(edinet_code)
    if reduction is None:
        return False
    actual, forecast, forecast_eps = reduction
    return _exceeds_full_payout(forecast, forecast_eps)


def get_dividend_cut_codes(codes, code_map=None):
    """
    指定証券コードのうち、来期配当予想が減配の銘柄コードのset（文字列）を返す。

    選定アルゴリズムが実際に評価する候補集合のみを渡すことを想定（レート節約）。
    コード未解決・APIエラー・予想未開示の銘柄は除外せずスキップする（fail-open）。

    Args:
        codes (list): 証券コードのリスト（int/str混在可）
        code_map (dict, optional): build_code_map()の結果を呼び出し側で共有する場合に渡す。
                                    未指定なら内部でbuild_code_map()を呼ぶ。

    Returns:
        set: 減配と判定された証券コードの集合（str）
    """
    if not API_KEY:
        logging.error("EDINET get_dividend_cut_codes skipped: EDINETDB_API_KEY未設定")
        return set()

    if code_map is None:
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


def get_dividend_reductions(codes, code_map=None):
    """
    指定証券コードのうち、来期配当予想が減配（性向条件なし）の銘柄について
    {証券コード(str): (実績配当, 予想配当)} の辞書を返す。

    保有銘柄の減配監視用。選定フィルタ（get_dividend_cut_codes、減配かつ性向>100%）
    とは別に、性向条件を課さず「減配予想が出たこと自体」を検知する。売る/持つの
    判断は人間が行う前提で、通知のみに使う。

    Args:
        codes (list): 証券コードのリスト（int/str混在可）
        code_map (dict, optional): build_code_map()の結果を呼び出し側で共有する場合に渡す。

    Returns:
        dict: {"2379": (95.0, 80.0), ...}。APIキー未設定・コード未解決・APIエラー・
              未開示の銘柄はスキップする（fail-open）。
    """
    if not API_KEY:
        logging.error("EDINET get_dividend_reductions skipped: EDINETDB_API_KEY未設定")
        return {}

    if code_map is None:
        code_map = build_code_map()
    if not code_map:
        return {}

    reductions = {}
    for code in codes:
        code_str = str(code)
        edinet_code = code_map.get(code_str)
        if edinet_code is None:
            logging.error(f"EDINET code unresolved: {code_str}")
            continue
        reduction = _latest_reduction(edinet_code)
        if reduction is not None:
            actual, forecast, _ = reduction
            reductions[code_str] = (actual, forecast)
    return reductions
