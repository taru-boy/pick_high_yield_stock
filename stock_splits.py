"""
株式分割・併合を「購入履歴」シートに反映するための道具一式。

購入履歴の取得単価・株数は約定時点の生の値なので、そのあと分割があると
下流が全部ズレる（合計株数 → 時価総額 → 総時価総額 → 銘柄選定の4%/20%上限、
そして素材メモの取得来の損益）。実例: 三井住友トラストグループ(8309) は
2026-07-30 に 1株→4株の分割があり、取得来の損益が −61.70% と表示されていた。

真実の源は stock_splits.csv（列: 証券コード, 効力発生日, 分割比率, 取得元, 備考）。
yfinance はそこへ行を足すだけのベストエフォート係で、レート制限で落ちても
既に CSV にある分割の補正は効き続ける。取り逃したぶんは手で1行足せばよい。

分割比率は「1株が何株になったか」。4 なら 1株→4株の分割、0.5 なら 2株→1株の併合。
"""

import csv
import logging
import os
import re
from datetime import datetime

logging.basicConfig(level=logging.ERROR, filename="error.log")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SPLITS_CSV = os.path.join(BASE_DIR, "stock_splits.csv")

CSV_FIELDS = ["証券コード", "効力発生日", "分割比率", "取得元", "備考"]

# 購入履歴のG列に残す適用済みマーカー。これがある行は二度と補正しない。
_MARKER_RE = re.compile(r"\[split (\d{4}-\d{2}-\d{2}) x([\d.]+)\]")

# yfinance は素直に叩くとすぐ 429 を返すので、1銘柄ごとに間を空ける。
YF_SLEEP_SEC = 2.0
# レート制限に掛かると以降も全部落ちる。この回数続いたら打ち切って既存CSVで走る。
YF_MAX_CONSECUTIVE_FAILURES = 3
# 同じ比率の分割がこの日数以内に並んでいたら、同一イベントの重複とみなす。
DUPLICATE_WINDOW_DAYS = 7


def _fmt_ratio(ratio):
    """4.0 -> "4"、0.5 -> "0.5"。マーカー文字列を安定させるため末尾ゼロを落とす。"""
    return f"{float(ratio):g}"


def marker(effective_date, ratio):
    """購入履歴のG列に書く適用済みマーカー。"""
    return f"[split {effective_date} x{_fmt_ratio(ratio)}]"


def _parse_date(value):
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _safe_float(value):
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


def load_splits(path=SPLITS_CSV):
    """
    分割マスタを {証券コード(str): [{"効力発生日": str, "分割比率": float}, ...]} で返す。

    各リストは効力発生日の昇順。ファイル未配置・読み込み失敗時は空辞書を返す
    （fail-open。excluded_stocks.load_excluded_codes と同じ構え）。

    Args:
        path (str): 分割マスタCSVのパス

    Returns:
        dict: 証券コードごとの分割リスト
    """
    splits = {}
    try:
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                code = (row.get("証券コード") or "").strip()
                date = (row.get("効力発生日") or "").strip()
                if not code or not _parse_date(date):
                    continue
                try:
                    ratio = float((row.get("分割比率") or "").strip())
                except ValueError:
                    continue
                if ratio <= 0 or ratio == 1:
                    continue
                splits.setdefault(code, []).append(
                    {"効力発生日": date, "分割比率": ratio}
                )
    except FileNotFoundError:
        return {}
    except OSError as e:
        logging.error(f"stock_splits load failed: {e}")
        return {}

    for code in splits:
        splits[code].sort(key=lambda s: s["効力発生日"])
    return splits


def _write_splits(rows, path=SPLITS_CSV):
    """分割マスタCSVを丸ごと書き直す。失敗しても例外は投げない。"""
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        return True
    except OSError as e:
        logging.error(f"stock_splits write failed: {e}")
        return False


def _read_raw_rows(path=SPLITS_CSV):
    try:
        with open(path, encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []
    except OSError as e:
        logging.error(f"stock_splits read failed: {e}")
        return None


def refresh_splits_from_yfinance(codes, since_by_code=None, path=SPLITS_CSV):
    """
    yfinance から分割履歴を取ってきて stock_splits.csv にマージする（ベストエフォート）。

    yfinance は連続アクセスで即 YFRateLimitError を返すため、1銘柄ごとに待ち、
    どの例外も握りつぶす。落ちても既存CSVはそのまま残り、補正は効き続ける。

    since_by_code に「その銘柄を最初に買った日」を渡すと、それより前の分割は
    記録しない（補正対象にならないので、マスタを無駄に膨らませないため）。

    Args:
        codes (iterable): 証券コード（str/int どちらでも可）
        since_by_code (dict): {証券コード(str): "YYYY-MM-DD"}。省略可
        path (str): 分割マスタCSVのパス

    Returns:
        list: 新しく追加された分割 [{"証券コード","効力発生日","分割比率"}, ...]
    """
    try:
        import time

        import yfinance as yf
    except ImportError as e:
        logging.error(f"yfinance import failed: {e}")
        return []

    existing = _read_raw_rows(path)
    if existing is None:
        return []
    known = {
        ((r.get("証券コード") or "").strip(), (r.get("効力発生日") or "").strip())
        for r in existing
    }
    # 同じ分割が権利落ち日と効力発生日の2行に分かれて入っていることがある
    # （ホンダ(7267) の 1株→3株が 2023-09-28 と 2023-10-02 の両方に x3 で載る）。
    # そのまま両方適用すると9倍になるので、近い日付・同じ比率は同一イベントとみなす。
    seen = [
        (
            (r.get("証券コード") or "").strip(),
            _parse_date(r.get("効力発生日")),
            _safe_float(r.get("分割比率")),
        )
        for r in existing
    ]
    seen = [s for s in seen if s[1] and s[2]]

    def _is_duplicate(code, date, ratio):
        for s_code, s_date, s_ratio in seen:
            if s_code != code or abs(s_ratio - ratio) > 1e-6:
                continue
            if abs((s_date - date).days) <= DUPLICATE_WINDOW_DAYS:
                return True
        return False

    since_by_code = since_by_code or {}
    added = []
    consecutive_failures = 0
    for raw_code in codes:
        code = str(raw_code).strip()
        if not code:
            continue
        try:
            series = yf.Ticker(f"{code}.T").splits
        except Exception as e:
            # レート制限・ネットワーク断など。この銘柄は諦めて次へ。
            logging.error(f"yfinance splits failed ({code}): {e}")
            consecutive_failures += 1
            if consecutive_failures >= YF_MAX_CONSECUTIVE_FAILURES:
                # 一度レート制限に掛かると残りも全部落ちる。空回りを続けても
                # 週次実行を数分遅らせるだけなので、諦めて既存CSVで走る。
                logging.error("yfinance splits: 連続失敗のため打ち切り")
                break
            time.sleep(YF_SLEEP_SEC)
            continue
        consecutive_failures = 0

        since = since_by_code.get(code)
        for timestamp, ratio in series.items():
            date = timestamp.strftime("%Y-%m-%d")
            if since and date <= since:
                continue
            if (code, date) in known:
                continue
            ratio = round(float(ratio), 6)
            if ratio <= 0 or ratio == 1:
                continue
            if _is_duplicate(code, _parse_date(date), ratio):
                continue
            known.add((code, date))
            seen.append((code, _parse_date(date), ratio))
            row = {
                "証券コード": code,
                "効力発生日": date,
                "分割比率": _fmt_ratio(ratio),
                "取得元": "yfinance",
                "備考": (
                    f"1株→{_fmt_ratio(ratio)}株の分割"
                    if ratio > 1
                    else f"{_fmt_ratio(1 / ratio)}株→1株の併合"
                ),
            }
            existing.append(row)
            added.append(
                {"証券コード": code, "効力発生日": date, "分割比率": ratio}
            )
        time.sleep(YF_SLEEP_SEC)

    if added:
        existing.sort(
            key=lambda r: ((r.get("証券コード") or ""), (r.get("効力発生日") or ""))
        )
        if not _write_splits(existing, path):
            return []
    return added


def _column_index(header, name):
    try:
        return header.index(name)
    except ValueError:
        return None


def _a1_col(index):
    """0始まりの列インデックスを A1 記法の列名に変換する（0 -> "A"）。"""
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def plan_history_adjustments(data, splits):
    """
    「購入履歴」シートの生データから、分割補正が必要な行の更新内容を組み立てる。

    補正するのは「効力発生日より前に買った行」だけ（権利落ち日以降の買付は
    もう分割後の値段で約定しているため）。適用済みかどうかはG列のマーカーで見る
    ので、何度実行しても二重には効かない。

    取得単価 ÷ 比率、株数 × 比率。積（実際に払った金額）は変わらない。

    Args:
        data (list): worksheet.get_all_values() の戻り（1行目がヘッダ）
        splits (dict): load_splits() の戻り

    Returns:
        tuple: (updates, warnings)
            updates: [{"row": シート上の1始まり行番号, "証券コード", "会社名",
                       "取得単価": (旧, 新), "株数": (旧, 新), "備考": 新, "適用": [...]}]
            warnings: 人が判断すべきものの説明文リスト
    """
    updates, warnings = [], []
    if not data or len(data) < 2 or not splits:
        return updates, warnings

    header = data[0]
    idx = {name: _column_index(header, name) for name in
           ("日付", "証券コード", "会社名", "取得単価", "株数", "備考")}
    if any(idx[name] is None for name in ("日付", "証券コード", "取得単価", "株数")):
        logging.error(f"購入履歴の列が想定と違います: {header}")
        return updates, warnings

    def cell(row, name):
        i = idx[name]
        return row[i] if i is not None and i < len(row) else ""

    for offset, row in enumerate(data[1:]):
        code = cell(row, "証券コード").strip()
        bought = _parse_date(cell(row, "日付"))
        if not code or bought is None or code not in splits:
            continue

        note = cell(row, "備考")
        applied = {m.group(0) for m in _MARKER_RE.finditer(note)}

        try:
            price = float(cell(row, "取得単価"))
            shares = float(cell(row, "株数"))
        except ValueError:
            continue
        if price <= 0 or shares <= 0:
            continue

        name = cell(row, "会社名").strip() or code
        new_price, new_shares, newly = price, shares, []
        for split in splits[code]:
            effective = _parse_date(split["効力発生日"])
            ratio = split["分割比率"]
            if effective is None or bought >= effective:
                continue
            mark = marker(split["効力発生日"], ratio)
            if mark in applied:
                continue
            candidate = new_shares * ratio
            if abs(candidate - round(candidate)) > 1e-9:
                # 併合の端株は現金精算などケースバイケース。勝手に丸めない。
                warnings.append(
                    f"{name}（{code}）{split['効力発生日']} 比率{_fmt_ratio(ratio)}: "
                    f"{new_shares:g}株が整数になりません。手で調整してください。"
                )
                break
            new_price = new_price / ratio
            new_shares = round(candidate)
            newly.append(mark)

        if not newly:
            continue
        sheet_row = offset + 2  # ヘッダ1行 + 0始まりオフセット
        new_note = " ".join(filter(None, [note.strip()] + newly))
        cells = [
            (f"{_a1_col(idx['取得単価'])}{sheet_row}", round(new_price, 6)),
            (f"{_a1_col(idx['株数'])}{sheet_row}", new_shares),
        ]
        if idx["備考"] is not None:
            cells.append((f"{_a1_col(idx['備考'])}{sheet_row}", new_note))
        else:
            # 備考列が無いとマーカーを残せず、次回また補正してしまう。
            warnings.append(
                f"{name}（{code}）: 購入履歴に「備考」列が無いため補正を見送りました。"
            )
            continue
        updates.append(
            {
                "row": sheet_row,
                "証券コード": code,
                "会社名": name,
                "取得単価": (price, round(new_price, 6)),
                "株数": (shares, new_shares),
                "備考": new_note,
                "適用": newly,
                "cells": cells,
            }
        )
    return updates, warnings


def describe_updates(updates):
    """LINE通知・ログ用に更新内容を人が読める行の一覧にする。"""
    return [
        f"{u['会社名']}（{u['証券コード']}）"
        f" 取得単価 {u['取得単価'][0]:,.2f} → {u['取得単価'][1]:,.2f}円 /"
        f" 株数 {u['株数'][0]:g} → {u['株数'][1]:g}株"
        for u in updates
    ]
