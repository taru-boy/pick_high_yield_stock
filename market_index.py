"""週次レポートの「マクロ」素材として、日経の指数値を取得・記録する。

日経の指数公式サイトの「指数値一覧」ページ（1ページに全指数が載る）を Selenium で
開き、必要な指数の当日値だけを抜く。requests だと 403 で弾かれるため、既存の
get_high_dividend_stock_code.py と同じ headless Chrome を使い回す。

取得した値は index_history.csv に追記して、次週に「前週比」を計算するための
土台にする。**行のキーは実行日ではなく、その値が属する営業日**（ページの
「データ日付」）。実行日で記録していたときは、場中に手で走らせた実行が
「8/6の記録」として残り、中身は 8/5 の終値と 8/6 の前引が混ざる、という
事故が起きた（2026-08-07 の週次レポートで発覚）。同じ営業日の行は上書きなので、
何度実行しても増えない。

**確定値（終値・大引）しか記録しない。** 前引や場中の時刻表示は、その営業日の
値がまだ決まっていないということなので捨てる。cron は土曜 04:40（金曜の引け後）なので
本番は必ず通り、昼に手で試した実行が履歴を汚さない。

TOPIX は入れていない。日経の指数サイトが持っておらず、日経電子版・JPX 側は
ログイン壁や JS 描画で安定して取れなかったため、出典を1つに絞った。ポートフォリオが
そもそも「日経平均高配当株50 / 累進高配当株 / 連続増配株」の3指数から選ばれている
ので、その3本＋日経平均のほうが素材としても素直に対応する。

単体実行:
    .venv/bin/python market_index.py
"""

import csv
import logging
import os
import re
import time
from datetime import datetime

from selenium.webdriver.common.by import By

from get_high_dividend_stock_code import setup_driver

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_CSV = os.path.join(BASE_DIR, "index_history.csv")

INDEX_LIST_URL = "https://indexes.nikkei.co.jp/nkave/index"

# 「指数値一覧」ページの表示名の頭。保有ポートフォリオの母集団（pick_high_yield_stock.py
# が構成銘柄を取る3指数）と、相場全体の代表として日経平均を並べる。
# 前方一致で拾うのは、表示名に愛称が付くことがあるため
# （例：「日経累進高配当株指数（愛称：しっかりインカム）」）。愛称が変わっても
# 履歴 CSV のキーがブレないよう、記録するのはここに書いた正式名のほうにする。
TARGET_INDEXES = [
    "日経平均株価",
    "日経平均高配当株50指数",
    "日経累進高配当株指数",
    "日経連続増配株指数",
]

# 日付 = その値が属する営業日（ISO）。データ日付 = ページの生表記（"08.07(*大引)"）。
# 記録日 = 実際にスクリプトを走らせた日（追跡用。比較には使わない）。
HISTORY_HEADER = ["日付", "指数名", "指数値", "データ日付", "前日比(%)", "記録日"]

# 括弧の中がこれなら、その営業日の値が確定している。
# 日経平均は "(*大引)"、他の指数は "(終値)"。場中は "(*前引)" や "(15:30)" になる。
_CONFIRMED_MARKERS = ("終値", "大引")


def _to_float(text):
    """"103,020.90" や "+1.17%" のような表示文字列を float にする。失敗時は None。"""
    if text is None:
        return None
    m = re.search(r"[-+]?[\d,]+(?:\.\d+)?", str(text))
    if not m:
        return None
    try:
        return float(m.group().replace(",", "").replace("+", ""))
    except ValueError:
        return None


def _parse_data_date(text, today=None):
    """"08.07(*大引)" のようなデータ日付表記を (ISO日付, 確定値か) にする。

    ページは年を持たないので実行日から補う。実行日より未来になったら前年
    （年末年始をまたぐケース。1月に "12.30(終値)" を見たら前年の12月30日）。

    Returns:
        tuple: (str|None, bool) — 日付が読めなければ (None, False)
    """
    if not text:
        return None, False
    m = re.search(r"(\d{1,2})\.(\d{1,2})", str(text))
    if not m:
        return None, False
    today = today or datetime.today()
    month, day = int(m.group(1)), int(m.group(2))
    try:
        parsed = datetime(today.year, month, day)
    except ValueError:  # 2/30 のような表記崩れ
        return None, False
    if parsed > today:
        try:
            parsed = datetime(today.year - 1, month, day)
        except ValueError:
            return None, False
    confirmed = any(marker in str(text) for marker in _CONFIRMED_MARKERS)
    return parsed.strftime("%Y-%m-%d"), confirmed


def fetch_index_values(today=None):
    """指数値一覧ページから TARGET_INDEXES の最新値を取る。

    Returns:
        dict: {指数名: {"値", "データ日付"（生表記）, "日付"（ISO）,
                        "確定"（終値・大引なら True）, "前日比(%)"}}
              取得できなければ空の dict（呼び出し側は fail-open で扱う）
    """
    values = {}
    driver = None
    try:
        driver = setup_driver()
        driver.get(INDEX_LIST_URL)
        time.sleep(3)  # 指数値は描画後に入るので軽く待つ
        for item in driver.find_elements(By.CSS_SELECTOR, "div.idx-indexlist-item"):
            try:
                name = item.find_element(By.CSS_SELECTOR, "div.name").text.strip()
            except Exception:
                continue
            canonical = next(
                (t for t in TARGET_INDEXES if name.startswith(t)), None
            )
            if canonical is None:
                continue
            value = _to_float(item.find_element(By.CSS_SELECTOR, "div.value").text)
            if value is None:
                continue
            try:
                data_date = item.find_element(By.CSS_SELECTOR, "div.date").text.strip()
            except Exception:
                data_date = ""
            try:
                # "+1.17%" / "-3.66%" のどちらも符号ごと _to_float が拾う
                change = _to_float(
                    item.find_element(By.CSS_SELECTOR, "span.range").text
                )
            except Exception:
                change = None
            iso_date, confirmed = _parse_data_date(data_date, today)
            values[canonical] = {
                "値": value,
                "データ日付": data_date,
                "日付": iso_date,
                "確定": confirmed,
                "前日比(%)": change,
            }
    except Exception as e:
        logging.error(f"指数値の取得に失敗: {e}")
        print(f"[warn] 指数値の取得に失敗しました: {e}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
    return values


def _read_history():
    """履歴を読む。列が足りない古い行は補って返す（記録日は後から足した列）。"""
    if not os.path.exists(HISTORY_CSV):
        return []
    with open(HISTORY_CSV, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for key in HISTORY_HEADER:
            r.setdefault(key, "")
            if r[key] is None:
                r[key] = ""
    return rows


def _write_history(rows):
    with open(HISTORY_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_HEADER)
        writer.writeheader()
        writer.writerows(rows)


def record_index_values(values, record_date=None):
    """確定値だけを index_history.csv に記録する（同一営業日・同一指数は上書き）。

    キーは「その値が属する営業日」。実行日ではないので、同じ営業日のデータを
    何度取り直しても行は増えず、場中に走らせた実行は（確定していないので）
    そもそも記録されない。

    Returns:
        list: 記録した (指数名, 営業日) のリスト
    """
    record_date = record_date or datetime.today().strftime("%Y-%m-%d")
    confirmed = {
        name: v
        for name, v in values.items()
        if v.get("確定") and v.get("日付")
    }
    skipped = [name for name in values if name not in confirmed]
    if skipped:
        print(f"[info] 確定値でないため記録しません: {', '.join(skipped)}")
    if not confirmed:
        return []

    keys = {(v["日付"], name) for name, v in confirmed.items()}
    rows = [r for r in _read_history() if (r["日付"], r["指数名"]) not in keys]
    for name, v in confirmed.items():
        rows.append(
            {
                "日付": v["日付"],
                "指数名": name,
                "指数値": f"{v['値']}",
                "データ日付": v.get("データ日付", ""),
                "前日比(%)": "" if v.get("前日比(%)") is None else f"{v['前日比(%)']}",
                "記録日": record_date,
            }
        )
    rows.sort(key=lambda r: (r["日付"], r["指数名"]))
    _write_history(rows)
    return sorted(keys)


def build_index_summary(record_date=None, extra_values=None):
    """指数の当日値と前回記録との差を作る。素材メモ用のまとめ。

    比較の相手は「その指数自身の営業日より前の、直近の記録」。実行日で切ると、
    同じ日に取り直したデータを自分自身と比べてしまう。

    Args:
        extra_values: 指数以外にも同じ「週次で記録して前回と比べる」扱いをしたい
            系列を混ぜたいときに渡す。指数と同じ形
            （{名前: {"値", "データ日付", "日付", "確定", "前日比(%)"}}）で渡す。
            高配当候補銘柄の平均利回りなど、前回比が素材になるものを想定。

    Returns:
        list[dict]: {指数名, 値, データ日付, 日付, 確定, 前日比(%),
                     前回値, 前回日付, 前回比(%)}
                    何も取れなければ空リスト
    """
    today = (
        datetime.strptime(record_date, "%Y-%m-%d") if record_date else datetime.today()
    )
    values = fetch_index_values(today)
    extra_values = extra_values or {}
    values.update(extra_values)
    if not values:
        return []

    record_date = record_date or today.strftime("%Y-%m-%d")
    history = _read_history()  # 記録前に読む（前回＝先週の行を拾うため）

    names = list(TARGET_INDEXES) + [n for n in extra_values if n not in TARGET_INDEXES]
    summary = []
    for name in names:
        v = values.get(name)
        if not v:
            continue
        # 前回＝この指数の営業日より前の直近記録。営業日が読めなければ比較しない
        # （中身がどの日のものか分からないまま「前回比」を出すほうが危ない）。
        prev = None
        if v.get("日付"):
            past = [
                r
                for r in history
                if r["指数名"] == name and r["日付"] and r["日付"] < v["日付"]
            ]
            past.sort(key=lambda r: r["日付"])
            prev = past[-1] if past else None
        prev_value = _to_float(prev["指数値"]) if prev else None
        change_pct = None
        if prev_value:
            change_pct = (v["値"] - prev_value) / prev_value * 100
        summary.append(
            {
                "指数名": name,
                "値": v["値"],
                "データ日付": v.get("データ日付", ""),
                "日付": v.get("日付"),
                "確定": bool(v.get("確定")),
                "前日比(%)": v.get("前日比(%)"),
                "前回値": prev_value,
                "前回日付": prev["日付"] if prev else None,
                "前回比(%)": change_pct,
            }
        )

    record_index_values(values, record_date)
    return summary


if __name__ == "__main__":
    for row in build_index_summary():
        prev = (
            f"前回({row['前回日付']}) {row['前回値']:,.2f} → {row['前回比(%)']:+.2f}%"
            if row["前回値"]
            else "前回データなし"
        )
        state = "" if row["確定"] else "【場中の速報値・記録しません】"
        print(
            f"{row['指数名']}: {row['値']:,.2f}"
            f"（{row['データ日付']} = {row['日付']}）/ {prev} {state}"
        )
    print(f"[ok] 履歴: {HISTORY_CSV}")
