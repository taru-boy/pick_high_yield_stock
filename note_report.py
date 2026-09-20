"""週次運用レポートを生成するスクリプト。

毎週金曜に pick_high_yield_stock.py が更新する Google スプレッドシートの
3タブ（購入履歴 / 時価総額 / 配当推移）を**読むだけ**で、note記事用の
Markdown とトレンドグラフ（PNG）を生成する。再スクレイピングはしない。

cron では run_pick_high_yield_stock.sh の末尾から本体実行の後に呼ばれる。
ただしスプレッドシートを読むだけなので、いつでも単体で再生成できる（疎結合）。

既存方針に倣い fail-open（データ不足時は例外を投げず警告して終了）。
"""

import json
import os
import re
from datetime import datetime

import gspread
import matplotlib
import pandas as pd
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

from stock_splits import load_splits, marker

matplotlib.use("Agg")  # 画面の無いcron環境でも動かす
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mticker  # noqa: E402
from matplotlib import font_manager  # noqa: E402

# グラフを日本語ラベルで描くためのフォント設定。
# システムにある日本語フォント（Noto Sans CJK JP 等）を順に探して使う。
# 見つからなければ英語ラベルにフォールバックして豆腐化を避ける。
_JP_FONT_CANDIDATES = [
    "Noto Sans CJK JP",
    "IPAexGothic",
    "IPAGothic",
    "TakaoGothic",
    "VL Gothic",
    "Droid Sans Fallback",
]
_available_fonts = {f.name for f in font_manager.fontManager.ttflist}
JP_FONT = next((name for name in _JP_FONT_CANDIDATES if name in _available_fonts), None)
if JP_FONT:
    plt.rcParams["font.family"] = JP_FONT
    plt.rcParams["axes.unicode_minus"] = False  # マイナス記号の豆腐化を防ぐ

# レポート（Markdown / グラフPNG）の出力先。
# 手書きのnote下書き（note/drafts/）とは性格が違う機械生成物なので専用フォルダに分ける。
# ファイル名は固定で毎週上書きする（最新版が1セットだけ残る運用。公開済みのアーカイブはnote側が持つ）。
OUTPUT_DIR = "/home/taru-boy/Desktop/journaling/note/reports"
REPORT_FILENAME = "週次運用レポート.md"

# 所感の下書き用の素材メモ。note には載せず、headless Claude が読むためだけに置く
# （docs/weekly-report.md「一言所感の書き方」参照）。数字はすべてここで確定させ、
# Claude 側には計算をさせない（Web 検索を許しているので、拾ってきた数字と食い違うのを防ぐ）。
MATERIAL_MEMO_FILENAME = "週次素材メモ.md"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 保有銘柄の週次スナップショット。「時価総額」タブは毎週 clear() されて上書きされるため、
# 個別銘柄の先週の株価はここにしか残らない（＝週次騰落の唯一の土台）。
HOLDINGS_HISTORY_CSV = os.path.join(BASE_DIR, "holdings_history.csv")
# pick_high_yield_stock.py が書く選定の裏側（選定理由・減配予想・除外）。
PICK_META_PATH = os.path.join(BASE_DIR, "last_pick_meta.json")
# watch_dividend.py が毎回書き出す今週の高配当候補一覧。
CANDIDATES_CSV = os.path.join(BASE_DIR, "high_dividend_stocks.csv")

# 旗艦の有料記事（「分析しない」高配当株投資の仕組み）への導線。
# レポート末尾に毎週このCTAを自動で載せ、無料の集客導線→有料記事 の funnel を
# 手作業（旧 docs/weekly-report.md 手順6）に頼らず必ず通す。
# ★ここに旗艦記事の note URL を入れる。空のままなら CTA は出力しない（fail-safe：
#   壊れた/プレースホルダのリンクを公開しないため）。
FLAGSHIP_ARTICLE_URL = "https://note.com/tarutaru_bouzu/n/n22a7f1da8e1c"

# 環境変数を読み込む（pick_high_yield_stock.py と同じ認証パターンを流用）
load_dotenv(dotenv_path="/home/taru-boy/Desktop/get_stock/.env")
SPREADSHEET_KEY = os.getenv("SPREADSHEET_KEY")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")
SCOPE = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _to_number(value):
    """通貨記号・カンマ混じりの文字列を float に変換する。失敗時は None。

    例: '¥31,420' / '19,629円' / '5.09' などを受け付ける。
    """
    if value is None:
        return None
    # 数字・小数点・マイナス符号以外（¥ , 円 空白 等）を取り除く
    text = re.sub(r"[^\d.\-]", "", str(value))
    if text in ("", "-", ".", "-."):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _read_worksheet(gc, title):
    """指定タブを DataFrame で返す。タブが無ければ None。"""
    try:
        worksheet = gc.open_by_key(SPREADSHEET_KEY).worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        print(f"[warn] タブが見つかりません: {title}")
        return None
    values = worksheet.get_all_values()
    if not values or len(values) < 2:
        print(f"[warn] タブにデータがありません: {title}")
        return pd.DataFrame(columns=values[0] if values else [])
    return pd.DataFrame(values[1:], columns=values[0])


def _clean_trend(df_trend):
    """配当推移タブを解析し、日付で重複排除（最新を残す）して時系列順に返す。

    同一週に2回実行された場合などの重複行を畳む。空なら None。
    """
    if df_trend is None or df_trend.empty:
        return None
    df = df_trend.copy()
    df["日付"] = pd.to_datetime(df["日付"], errors="coerce")
    df["総年間配当(円)"] = df["総年間配当(円)"].map(_to_number)
    df["総時価総額(円)"] = df["総時価総額(円)"].map(_to_number)
    df = df.dropna(subset=["日付"]).sort_values("日付")
    df = df.drop_duplicates(subset=["日付"], keep="last").reset_index(drop=True)
    return df if not df.empty else None


def _yen(value):
    """整数の円表記（カンマ区切り）。"""
    return f"{int(round(value)):,}円"


def _signed_yen(value):
    """符号付きの円表記。プラスには + を付ける。"""
    sign = "+" if value >= 0 else "−"
    return f"{sign}{abs(int(round(value))):,}円"


def _signed_pct(value):
    sign = "+" if value >= 0 else "−"
    return f"{sign}{abs(value):.2f}%"


def _cumulative_cost_series(df_holding, dates):
    """各トレンド日における累積取得額 Σ(取得単価×株数) を dates と同じ並びで返す。

    配当推移タブは取得額を持たない（日付/年間配当/時価総額の3列）ため、購入履歴
    （日付・取得単価・株数）から日付 ≤ d の購入を積み上げて再構成する。
    必要列が無い・全て解析不能なら None（取得額ラインは描かない）。
    """
    if df_holding is None or df_holding.empty or "日付" not in df_holding:
        return None
    price_col = "取得単価" if "取得単価" in df_holding else "株価"
    if price_col not in df_holding or "株数" not in df_holding:
        return None
    h = pd.DataFrame(
        {
            "日付": pd.to_datetime(df_holding["日付"], errors="coerce"),
            "_cost": df_holding[price_col].map(_to_number)
            * df_holding["株数"].map(_to_number),
        }
    ).dropna(subset=["日付", "_cost"])
    if h.empty:
        return None
    return [h.loc[h["日付"] <= d, "_cost"].sum() for d in dates]


def build_trend_graphs(df_trend, df_holding=None):
    """配当推移タブからトレンドグラフのPNGを生成し、(表示名, ファイル名) のリストを返す。

    日本語フォントが見つかればラベルも日本語にする。見つからない環境では
    豆腐化を避けるため英語ラベルにフォールバックする（JP_FONT で判定）。
    総時価総額グラフには購入履歴から再構成した取得額ラインを重ね、評価損益を可視化する。
    """
    df = _clean_trend(df_trend)
    if df is None:
        return []

    # 累積見込み配当（実受取記録が無いため予想ベースで代用）:
    # 各区間の頭の予想年間配当額を、前回スナップショットからの経過日数で
    # 日割り（年間配当 × 経過日数/365）して積み上げる。
    cumulative = []
    running = 0.0
    prev_date = None
    prev_annual = None
    for _, row in df.iterrows():
        if prev_date is not None and prev_annual is not None:
            days = (row["日付"] - prev_date).days
            running += prev_annual * days / 365
        cumulative.append(running)
        prev_date = row["日付"]
        prev_annual = row["総年間配当(円)"]
    df["累積見込み配当(円)"] = cumulative

    # (データ列, 日本語タイトル, 英語タイトル, ファイル名スラッグ)
    specs = [
        ("総年間配当(円)", "予想年間配当額（円）", "Forecast Annual Dividend (JPY)", "trend_annual_dividend"),
        ("総時価総額(円)", "総時価総額と取得額（円）", "Market Value vs. Cost (JPY)", "trend_market_value"),
        ("累積見込み配当(円)", "累積見込み配当（円・概算）", "Cumulative Dividend, est. (JPY)", "trend_cumulative_dividend"),
    ]
    # 総時価総額グラフに重ねる取得額（購入履歴から再構成）。
    cost_series = _cumulative_cost_series(df_holding, df["日付"])

    filenames = []
    for column, jp_title, en_title, slug in specs:
        title = jp_title if JP_FONT else en_title
        if df[column].dropna().empty:
            continue
        overlay_cost = slug == "trend_market_value" and cost_series is not None
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot(df["日付"], df[column], marker="o", linewidth=2, color="#2a7ae2")
        # 取得額を重ねる総時価総額グラフでは、青い全面塗りの代わりに損益バンドを使う。
        if not overlay_cost:
            ax.fill_between(df["日付"], df[column], alpha=0.12, color="#2a7ae2")
        # 総時価総額グラフには取得額ラインを重ね、面（評価損益）を塗り分ける。
        if overlay_cost:
            value_label = "総時価総額" if JP_FONT else "Market value"
            cost_label = "取得額" if JP_FONT else "Cost"
            ax.lines[-1].set_label(value_label)
            ax.plot(
                df["日付"], cost_series,
                marker="o", linewidth=2, linestyle="--", color="#888888",
                label=cost_label,
            )
            ax.fill_between(
                df["日付"], cost_series, df[column],
                where=[v >= c for v, c in zip(df[column], cost_series)],
                alpha=0.18, color="#37b24d", interpolate=True,
            )
            ax.fill_between(
                df["日付"], cost_series, df[column],
                where=[v < c for v, c in zip(df[column], cost_series)],
                alpha=0.18, color="#f03e3e", interpolate=True,
            )
            ax.legend(loc="upper left", fontsize=9)
        ax.set_title(title)
        ax.grid(True, alpha=0.3)
        ax.get_yaxis().set_major_formatter(
            mticker.FuncFormatter(lambda x, _: f"{int(x):,}")
        )
        fig.autofmt_xdate()
        fig.tight_layout()
        filename = f"{slug}.png"  # 固定名で毎週上書き
        fig.savefig(os.path.join(OUTPUT_DIR, filename), dpi=120)
        plt.close(fig)
        filenames.append((title, filename))
    return filenames


def _collapse_by_share(series, min_share=0.02):
    """評価額降順に並べ、構成比 min_share 未満の項目だけを「その他」に畳む。

    構成比 min_share（既定 2%）以上の項目は必ず単独スライスとして残す
    （「分散している」を細かく見せたいので件数では畳まない）。全項目が閾値以上なら
    「その他」スライスは作らない。
    """
    series = series.sort_values(ascending=False)
    total = series.sum()
    if total <= 0:
        return series
    keep = series[series / total >= min_share]
    rest = series[series / total < min_share]
    if not rest.empty:
        keep = keep.copy()
        keep["その他"] = rest.sum()
    return keep


def build_composition_graphs(df_market):
    """時価総額タブからポートフォリオ構成の円グラフ（PNG）を生成する。

    セクター別・銘柄別の2枚。19行/32行の表の代わりに「分散している」ことを
    一目で見せる集客向けビジュアル。構成比2%未満だけを「その他」に畳み、2%以上は単独表示。
    日本語フォントが無ければラベルを伏せて豆腐化を避ける（autopct の％は出す）。
    戻り値は build_trend_graphs と同じ (表示名, ファイル名) のリスト。
    """
    if df_market is None or "時価総額" not in df_market:
        return []
    dfm = df_market.copy()
    dfm["_cap"] = dfm["時価総額"].map(_to_number)
    dfm = dfm.dropna(subset=["_cap"])
    dfm = dfm[dfm["_cap"] > 0]
    if dfm.empty:
        return []

    def _draw_pie(series, jp_title, en_title, slug):
        title = jp_title if JP_FONT else en_title
        labels = list(series.index)
        # スライスが多い（2%閾値で銘柄数が増える）と外周ラベルが重なって潰れるので、
        # 一定数を超えたら社名は凡例に逃がし、スライスには％だけ載せる。
        use_legend = not JP_FONT or len(series) > 12
        if use_legend:
            fig, ax = plt.subplots(figsize=(9, 6))
        else:
            fig, ax = plt.subplots(figsize=(6, 6))
        wedges, *_ = ax.pie(
            list(series.values),
            labels=None if use_legend else labels,
            autopct="%1.1f%%",
            startangle=90,
            counterclock=False,
            pctdistance=0.8,
            textprops={"fontsize": 9},
        )
        if use_legend and JP_FONT:
            ax.legend(
                wedges,
                [f"{n}（{v / series.sum() * 100:.1f}%）" for n, v in series.items()],
                loc="center left",
                bbox_to_anchor=(1.0, 0.5),
                fontsize=8,
                frameon=False,
            )
        ax.set_title(title)
        ax.axis("equal")
        fig.tight_layout()
        filename = f"{slug}.png"  # 固定名で毎週上書き
        fig.savefig(os.path.join(OUTPUT_DIR, filename), dpi=120)
        plt.close(fig)
        return title, filename

    results = []
    if "セクター" in dfm:
        sector_cap = dfm.groupby("セクター")["_cap"].sum()
        if not sector_cap.empty:
            sector_cap = _collapse_by_share(sector_cap)
            results.append(
                _draw_pie(sector_cap, "セクター別構成", "Sector Allocation", "pie_sector")
            )
    if "会社名" in dfm:
        holding_cap = dfm.groupby("会社名")["_cap"].sum()
        if not holding_cap.empty:
            holding_cap = _collapse_by_share(holding_cap)
            results.append(
                _draw_pie(
                    holding_cap,
                    "銘柄別構成",
                    "Holdings",
                    "pie_holding",
                )
            )
    return results


# ---------------------------------------------------------------------------
# 所感の素材メモ（note には載せない・headless Claude が読む用）
# ---------------------------------------------------------------------------


# 過去スナップショットに適用済みの分割マーカーを残す列。
# 分割前の株価をそのまま持っていると、分割をまたいだ週の騰落が毎回 −75% になって
# 3節が使い物にならない。株価を遡及調整しつつ、この列で二重調整を防ぐ。
SPLIT_ADJUST_COL = "分割調整"


def _backadjust_history(history, splits):
    """holdings_history.csv の分割前の株価を分割後ベースに揃える。

    証券取引所の調整済み株価と同じ考え方で、効力発生日より前の行の株価を比率で割る。
    適用済みマーカーを SPLIT_ADJUST_COL に積むので、何度実行しても二重には効かない。

    Returns:
        tuple: (history, 調整した行数)
    """
    if history is None or history.empty or not splits or "株価" not in history:
        return history, 0
    if SPLIT_ADJUST_COL not in history:
        history[SPLIT_ADJUST_COL] = ""
    history[SPLIT_ADJUST_COL] = history[SPLIT_ADJUST_COL].fillna("")

    changed = 0
    codes = history["証券コード"].astype(str).str.strip()
    for code, code_splits in splits.items():
        for split in code_splits:
            mark = marker(split["効力発生日"], split["分割比率"])
            target = (
                (codes == code)
                & (history["日付"] < split["効力発生日"])
                & (~history[SPLIT_ADJUST_COL].str.contains(re.escape(mark)))
            )
            if not target.any():
                continue
            history.loc[target, "株価"] = (
                history.loc[target, "株価"].map(_to_number) / split["分割比率"]
            )
            history.loc[target, SPLIT_ADJUST_COL] = (
                history.loc[target, SPLIT_ADJUST_COL] + " " + mark
            ).str.strip()
            changed += int(target.sum())
    return history, changed


def _market_data_date(df_trend, fallback):
    """時価総額タブの株価が「いつ取れたものか」を返す。

    pick_high_yield_stock.py は時価総額タブの上書きと配当推移タブへの追記を
    必ず同じ実行の中でやるので、配当推移の最新日付＝いま時価総額タブに入って
    いる株価の営業日になる。

    実行日で記録してはいけない。2026-08-06 に手で note_report.py を走らせた
    とき、時価総額タブはまだ 7/31 のままだったので「7/31 の株価を 8/6 の記録」
    として保存してしまい、翌日の週次レポートが「8/6 と比べて」「1日ぶんの動き」
    と書く事故になった（実際は 7/31 → 8/7 の1週間ぶん）。

    Returns:
        tuple: (ISO日付, 配当推移から取れたか)
    """
    df_t = _clean_trend(df_trend)
    if df_t is None or df_t.empty:
        return fallback, False
    return df_t.iloc[-1]["日付"].strftime("%Y-%m-%d"), True


def _snapshot_holdings(df_market, date_str):
    """保有銘柄を holdings_history.csv に記録し、(今回, 前回) を返す。

    「時価総額」タブは毎週 clear() で上書きされるので、先週の株価はここにしか
    残らない。date_str は実行日ではなく **その株価が取れた営業日**
    （`_market_data_date`）。同一日付の行は入れ替えてから書くので、同じ週に
    何度実行しても重複しないし、日付と中身がズレない。

    読み込んだ過去ぶんには株式分割の遡及調整をかけてから前回値として使う。
    """
    if df_market is None or df_market.empty or "証券コード" not in df_market:
        return None, None

    current = pd.DataFrame(
        {
            "日付": date_str,
            "証券コード": df_market["証券コード"].astype(str).str.strip(),
            "会社名": df_market.get("会社名", ""),
            "株価": df_market["株価"].map(_to_number),
            "配当利回り(%)": df_market["配当利回り(%)"].map(_to_number),
        }
    )

    history = None
    if os.path.exists(HOLDINGS_HISTORY_CSV):
        try:
            history = pd.read_csv(HOLDINGS_HISTORY_CSV, dtype={"証券コード": str})
        except Exception as e:
            print(f"[warn] 保有スナップショットを読めませんでした: {e}")

    history, adjusted = _backadjust_history(history, load_splits())
    if adjusted:
        print(f"[info] 過去スナップショット {adjusted} 行に株式分割を遡及反映しました")

    prev = None
    merged = current
    if history is not None and not history.empty:
        past = history[history["日付"] < date_str]
        if not past.empty:
            prev = past[past["日付"] == past["日付"].max()].copy()
        merged = pd.concat(
            [history[history["日付"] != date_str], current], ignore_index=True
        )

    try:
        merged.sort_values(["日付", "証券コード"]).to_csv(
            HOLDINGS_HISTORY_CSV, index=False
        )
    except Exception as e:
        print(f"[warn] 保有スナップショットを書けませんでした: {e}")
    return current, prev


# 分割・併合をまたぐと株価が見かけ上 −50〜−80%（併合なら +100%〜）動く。
# スナップショットは分割調整をしないので、その週は本物の暴落と区別が付かない。
# 大型の高配当株が1週で±30%動くことは実際にはほぼ無いので、この幅を超えたら
# 「要確認」を付けて所感には使わせない（見逃すより、間違って書くほうが痛い）。
SPLIT_SUSPECT_PCT = 30.0
# 1株→N株（N=2〜5）と、その逆の併合。実際の比率がこれに近ければ分割とほぼ断定できる。
_SPLIT_RATIOS = [1 / 2, 1 / 3, 1 / 4, 1 / 5, 2, 3, 4, 5]


def _split_suspect_note(before, after, threshold=SPLIT_SUSPECT_PCT, drop_only=False):
    """分割・併合の疑いがあれば注記文を返す。無ければ空文字。

    stock_splits.csv に載っている分割は購入履歴・スナップショットの側で調整済みなので、
    ここが拾うのは「マスタに載っていない分割」＝ yfinance が取り逃した分だけ。

    threshold は判定の幅。1週間の値動き（±30%）と、取得来の損益（±50%。
    何年か持っていれば素直に倍になることもあるので広めに取る）で使い分ける。

    drop_only=True にすると下げ方向しか疑わない。取得来の損益には何年ぶんもの
    値上がりが混ざるので比率での判定が当てにならず（シチズン時計の +184% は
    3株→1株の併合と数字上は見分けが付かない）、素直に儲かった銘柄まで所感から
    締め出してしまう。取り逃した分割の症状は必ず「大きな下げ」側に出るので、
    そちらだけ見れば見逃さずに誤検知を落とせる。
    """
    if not before or not after:
        return ""
    change_pct = (after - before) / before * 100
    if abs(change_pct) < threshold:
        return ""
    if drop_only and change_pct > 0:
        return ""
    ratio = after / before
    near = next((r for r in _SPLIT_RATIOS if abs(ratio - r) / r < 0.1), None)
    if near is not None and near < 1:
        return f"　←要確認（1株→{round(1 / near)}株の分割の可能性が高い）"
    if near is not None:
        return f"　←要確認（{round(near)}株→1株の併合の可能性が高い）"
    return "　←要確認（分割・併合の可能性）"


def _avg_cost_map(df_holding):
    """証券コード → 取得平均単価（購入履歴の加重平均）の dict を返す。

    購入履歴タブは pick_high_yield_stock.py が分割・併合のたびに書き換えるので、
    ここで読む取得単価は今の株価とそのまま比べられる。
    """
    if df_holding is None or df_holding.empty:
        return {}
    price_col = "取得単価" if "取得単価" in df_holding else "株価"
    if price_col not in df_holding or "株数" not in df_holding:
        return {}

    h = pd.DataFrame(
        {
            "証券コード": df_holding["証券コード"].astype(str).str.strip(),
            "_shares": df_holding["株数"].map(_to_number),
            "_cost": df_holding[price_col].map(_to_number)
            * df_holding["株数"].map(_to_number),
        }
    ).dropna()
    if h.empty:
        return {}
    agg = h.groupby("証券コード")[["_shares", "_cost"]].sum()

    avg_cost = {}
    for code, r in agg.iterrows():
        shares, cost = r["_shares"], r["_cost"]
        if shares and cost:
            avg_cost[str(code)] = cost / shares
    return avg_cost


def _weekly_moves(current, prev, avg_cost=None):
    """前回スナップショットと突き合わせ、銘柄ごとの騰落率を大きい順に返す。

    avg_cost（`_avg_cost_map` の戻り値）を渡すと「取得利回り」も添える。
    買った値段に対する予想配当の利回りで、株価がどう動いても自分の受け取る
    配当は変わらない、という所感の着地に使う。
    """
    if current is None or prev is None or prev.empty:
        return []
    avg_cost = avg_cost or {}
    prev_price = {}
    prev_yield = {}
    for _, r in prev.iterrows():
        code = str(r["証券コード"]).strip()
        prev_price[code] = _to_number(r["株価"])
        prev_yield[code] = _to_number(r.get("配当利回り(%)"))

    moves = []
    for _, r in current.iterrows():
        code = str(r["証券コード"]).strip()
        now, before = r["株価"], prev_price.get(code)
        if not now or not before:
            continue
        # 予想1株配当を「今の株価 × 今の利回り」で復元し、取得単価で割り直す。
        # 数字の出どころは時価総額タブと購入履歴だけ（新しい情報源を足さない）。
        cost = avg_cost.get(code)
        now_yield = r["配当利回り(%)"]
        moves.append(
            {
                "証券コード": code,
                "会社名": r["会社名"],
                "騰落率": (now - before) / before * 100,
                "前": before,
                "後": now,
                "利回り前": prev_yield.get(code),
                "利回り後": now_yield,
                "取得平均": cost,
                "取得利回り": (now_yield * now / cost) if (cost and now_yield) else None,
            }
        )
    moves.sort(key=lambda m: m["騰落率"], reverse=True)
    return moves


def _cost_basis_moves(df_holding, current):
    """取得来の損益率を銘柄ごとに大きい順で返す（週次データが無い週の代替素材）。"""
    if current is None:
        return []
    avg_cost_map = _avg_cost_map(df_holding)
    if not avg_cost_map:
        return []

    moves = []
    for _, r in current.iterrows():
        code = str(r["証券コード"]).strip()
        avg_cost = avg_cost_map.get(code)
        if not avg_cost or not r["株価"]:
            continue
        moves.append(
            {
                "証券コード": code,
                "会社名": r["会社名"],
                "騰落率": (r["株価"] - avg_cost) / avg_cost * 100,
                "取得平均": avg_cost,
                "現在": r["株価"],
            }
        )
    moves.sort(key=lambda m: m["騰落率"], reverse=True)
    return moves


def _candidate_stats():
    """今週の高配当候補一覧（high_dividend_stocks.csv）の全体観を返す。"""
    if not os.path.exists(CANDIDATES_CSV):
        return None
    try:
        df = pd.read_csv(CANDIDATES_CSV, dtype={"証券コード": str})
    except Exception as e:
        print(f"[warn] 候補一覧を読めませんでした: {e}")
        return None
    if df.empty or "配当利回り(%)" not in df:
        return None
    uniq = df.drop_duplicates(subset=["証券コード"], keep="first")
    yields = uniq["配当利回り(%)"].map(_to_number).dropna()
    if yields.empty:
        return None
    top = uniq.sort_values("配当利回り(%)", ascending=False).head(5)
    return {
        "延べ件数": len(df),
        "銘柄数": len(uniq),
        "平均利回り": float(yields.mean()),
        # 候補一覧は watch_dividend.py が毎回上書きするので、更新時刻＝この数字が
        # 取れた日。指数と同じく「実行日」ではなくこの日付で履歴に積む。
        "更新日": datetime.fromtimestamp(os.path.getmtime(CANDIDATES_CSV)).strftime(
            "%Y-%m-%d"
        ),
        "上位5": [
            (str(r["証券コード"]), r.get("会社名", ""), _to_number(r["配当利回り(%)"]))
            for _, r in top.iterrows()
        ],
    }


def _load_pick_meta(date_str):
    """pick_high_yield_stock.py が書いた選定メタ情報を読む（無ければ None）。"""
    if not os.path.exists(PICK_META_PATH):
        return None
    try:
        with open(PICK_META_PATH, encoding="utf-8") as f:
            meta = json.load(f)
    except Exception as e:
        print(f"[warn] 選定メタ情報を読めませんでした: {e}")
        return None
    # 古い週のメタを今週の素材として出さない（pick が失敗した週など）。
    if meta.get("日付") != date_str:
        print(f"[warn] 選定メタ情報が今週のものではありません（{meta.get('日付')}）")
        return None
    return meta


def _pt(value):
    """パーセントポイントの差（利回り・損益率の増減）。"""
    sign = "+" if value >= 0 else "−"
    return f"{sign}{abs(value):.2f}pt"


def build_material_memo(df_holding, df_market, df_trend, date_str, index_summary):
    """所感の下書き用の素材メモ（Markdown 文字列）を組み立てる。

    数字はすべてここで確定させる。Claude には Web 検索を許しているので、
    「数値はこのメモから、理由の説明だけ Web から」という分担にして、
    拾ってきた数字とレポート本体が食い違うのを防ぐ。
    """
    lines = [
        f"# 週次素材メモ（{date_str}・自動生成）",
        "",
        "> 所感の下書き専用のメモ。note には載せない。",
        "> 所感に書く数字は**このメモにある値だけ**を使う（自分で計算しない・Web から拾わない）。",
        "",
    ]

    # --- 1. 相場（指数）---------------------------------------------------
    lines.append("## 1. 相場（指数）")
    lines.append("")
    if index_summary:
        for row in index_summary:
            # 利回りのような「%そのもの」の系列は、変化率ではなく pt 差で書く。
            is_pct_series = row["指数名"].endswith("(%)")
            unit = "%" if is_pct_series else ""
            # 日付は「いつの値か」を必ず添える。指数ごとに更新タイミングが違い、
            # 同じ実行でも営業日が1日ずれることがある（2026-08-06 に日経平均だけ
            # 当日の前引、他は前日終値、という混在が起きた）。
            parts = [f"- {row['指数名']}: {row['値']:,.2f}{unit}"]
            if row.get("日付"):
                raw = f" {row['データ日付']}" if row.get("データ日付") else ""
                parts.append(f"（{row['日付']} の値{raw}）")
            elif row.get("データ日付"):
                parts.append(f"（データ日付 {row['データ日付']}）")
            if row.get("前日比(%)") is not None:
                parts.append(f" 前日比 {_signed_pct(row['前日比(%)'])}")
            if not row.get("確定", True):
                parts.append(" ／ **場中の速報値。所感には使わない**")
            elif row.get("前回値") is not None:
                diff = (
                    _pt(round(row["値"], 2) - round(row["前回値"], 2))
                    if is_pct_series
                    else _signed_pct(row["前回比(%)"])
                )
                parts.append(
                    f" / 前回（{row['前回日付']} の値）の {row['前回値']:,.2f}{unit}"
                    f" から {diff}"
                )
            else:
                parts.append(" / 前回比なし（この指数の記録は今回が最初）")
            lines.append("".join(parts))
        lines.append("")
        lines.append(
            "**所感には「◯月◯日と比べて」と、上の日付どおりに書くこと。**"
            "指数ごとに比較相手の営業日が違うことがある。"
        )
    else:
        lines.append("- 指数値の取得に失敗しました。今週は相場の数字は使えません。")
    lines.append("")

    # --- 2. ポートフォリオ（前週比）---------------------------------------
    lines.append("## 2. ポートフォリオ（前週比）")
    lines.append("")
    df_t = _clean_trend(df_trend)
    if df_t is not None and len(df_t) >= 1:
        costs = _cumulative_cost_series(df_holding, df_t["日付"])
        last = df_t.iloc[-1]
        prev = df_t.iloc[-2] if len(df_t) >= 2 else None

        def _pnl_pct(i):
            if not costs or costs[i] in (None, 0):
                return None
            value = df_t.iloc[i]["総時価総額(円)"]
            return (value - costs[i]) / costs[i] * 100 if value else None

        def _port_yield(row):
            if row["総時価総額(円)"]:
                return row["総年間配当(円)"] / row["総時価総額(円)"] * 100
            return None

        div_line = f"- 予想年間配当: {_yen(last['総年間配当(円)'])}"
        val_line = f"- 評価額: {_yen(last['総時価総額(円)'])}"
        if prev is not None:
            div_line += (
                f"（前週 {_yen(prev['総年間配当(円)'])} /"
                f" {_signed_yen(last['総年間配当(円)'] - prev['総年間配当(円)'])}）"
            )
            val_line += (
                f"（前週 {_yen(prev['総時価総額(円)'])} /"
                f" {_signed_pct((last['総時価総額(円)'] - prev['総時価総額(円)']) / prev['総時価総額(円)'] * 100)}）"
            )
        lines.append(div_line)
        lines.append(val_line)

        # pt 差は表示に出す小数2桁の値どうしで引く。生値で引くと
        # 「4.53% と 4.58% なのに −0.04pt」のように、並べた数字と合わない見え方になる。
        y_now, y_prev = _port_yield(last), (_port_yield(prev) if prev is not None else None)
        if y_now is not None:
            text = f"- 平均利回り: {y_now:.2f}%"
            if y_prev is not None:
                text += f"（前週 {y_prev:.2f}% / {_pt(round(y_now, 2) - round(y_prev, 2))}）"
            lines.append(text)

        p_now = _pnl_pct(len(df_t) - 1)
        p_prev = _pnl_pct(len(df_t) - 2) if len(df_t) >= 2 else None
        if p_now is not None:
            text = f"- 評価損益率: {_signed_pct(p_now)}"
            if p_prev is not None:
                text += (
                    f"（前週 {_signed_pct(p_prev)} /"
                    f" {_pt(round(p_now, 2) - round(p_prev, 2))}）"
                )
            lines.append(text)
        if prev is None:
            lines.append("- ※ 配当推移の記録が1週ぶんしかないため、前週比は出せません。")
    else:
        lines.append("- 配当推移タブが読めませんでした。")
    lines.append("")

    # --- 3. 保有銘柄の値動き ----------------------------------------------
    # 記録・比較の日付は実行日ではなく、その株価が取れた営業日（_market_data_date）。
    snapshot_date, from_trend = _market_data_date(df_trend, date_str)
    current, snapshot_prev = _snapshot_holdings(df_market, snapshot_date)
    moves = _weekly_moves(current, snapshot_prev, _avg_cost_map(df_holding))
    lines.append("## 3. 保有銘柄の値動き（前回スナップショットとの比較）")
    lines.append("")
    if moves:
        # 何日ぶんの動きなのかを明示する。祝日や手動実行で間隔が7日にならないことが
        # あり、「週次」と決め打つと所感で「今週は」と書かれて事実とズレる。
        prev_date = str(snapshot_prev["日付"].iloc[0])
        try:
            days = (
                datetime.strptime(snapshot_date, "%Y-%m-%d")
                - datetime.strptime(prev_date, "%Y-%m-%d")
            ).days
            span = f"{days}日ぶん"
        except ValueError:
            span = "前回から"
        lines.append(
            f"（{prev_date} の株価 → {snapshot_date} の株価。{span}の動き"
            f" / 対象 {len(moves)}銘柄）"
        )
        lines.append(
            f"**所感には「{span}の動き」として書くこと**（「今週」と決め打たない）。"
        )
        if not from_trend:
            lines.append(
                "※ 配当推移タブが読めず、株価の営業日が確定できませんでした"
                f"（実行日 {date_str} で代用）。日付の扱いに注意してください。"
            )
        lines.append("")

        lines.append(
            "※ 株価は stock_splits.csv に載っている分割・併合ぶんを調整済み。"
            "それでも「要確認」が付いた銘柄はマスタに無い分割の疑いがあるので、"
            "所感には書かないこと。"
        )
        lines.append("")
        lines.append(
            "※ 「取得利回り」は買った値段に対する予想配当の利回り"
            "（予想1株配当 ÷ 取得平均単価）。株価が動いても変わらない数字なので、"
            "**所感の項目3・4はこの取得利回りで締めること**。"
        )
        lines.append("")

        def _move_line(m):
            text = (
                f"- {m['会社名']}（{m['証券コード']}）: {_signed_pct(m['騰落率'])}"
                f"（{m['前']:,.0f}円 → {m['後']:,.0f}円）"
            )
            if m["利回り前"] and m["利回り後"]:
                text += f" 利回り {m['利回り前']:.2f}% → {m['利回り後']:.2f}%"
            if m.get("取得利回り") and m.get("取得平均"):
                text += (
                    f" / 取得利回り {m['取得利回り']:.2f}%"
                    f"（取得平均 {m['取得平均']:,.0f}円）"
                )
            return text + _split_suspect_note(m["前"], m["後"])


        lines.append("**上げた順（上位3）**")
        lines.append("")
        for m in moves[:3]:
            lines.append(_move_line(m))
        lines.append("")
        lines.append("**下げた順（下位3）**")
        lines.append("")
        for m in list(reversed(moves))[:3]:
            lines.append(_move_line(m))
    else:
        lines.append(
            "- 前回のスナップショットがまだ無いため、今週は個別銘柄の値動きを出せません"
            "（来週から使えます）。項目3・4は「4. 取得来の損益」で代替してください。"
        )
    lines.append("")

    # --- 4. 取得来の損益 ---------------------------------------------------
    cost_moves = _cost_basis_moves(df_holding, current)
    lines.append("## 4. 取得来の損益（買った値段からの動き）")
    lines.append("")
    if cost_moves:
        lines.append(
            "※ 取得単価は購入履歴の時点で分割・併合ぶんを調整済み。"
            "それでも「要確認」が付いた銘柄はマスタに無い分割の疑いがあるので、"
            "所感には書かないこと。"
        )
        lines.append("")

        def _cost_line(m):
            # 取得来は何年ぶんもの値上がりが混ざるので、下げ方向だけ疑う
            # （そうしないと素直に儲かっただけの銘柄まで所感から弾かれる）。
            warn = _split_suspect_note(
                m["取得平均"], m["現在"], threshold=50.0, drop_only=True
            )
            return (
                f"- {m['会社名']}（{m['証券コード']}）: {_signed_pct(m['騰落率'])}"
                f"（取得平均 {m['取得平均']:,.0f}円 → {m['現在']:,.0f}円）{warn}"
            )

        lines.append("**含み益の大きい順（上位3）**")
        lines.append("")
        for m in cost_moves[:3]:
            lines.append(_cost_line(m))
        lines.append("")
        lines.append("**含み損の大きい順（下位3）**")
        lines.append("")
        for m in list(reversed(cost_moves))[:3]:
            lines.append(_cost_line(m))
    else:
        lines.append("- 取得来の損益を計算できませんでした。")
    lines.append("")

    # --- 5. 今週の買付（選定理由つき）--------------------------------------
    meta = _load_pick_meta(date_str)
    lines.append("## 5. 今週の買付（なぜこの銘柄か）")
    lines.append("")
    if meta and meta.get("買付"):
        for i, s in enumerate(meta["買付"]):
            lines.append(
                f"{i + 1}. {s['会社名']}（{s['証券コード']}） {s['セクター']} /"
                f" 利回り{s['配当利回り(%)']:.2f}% / {s['株価']:,.0f}円 / {s['株数']}株"
            )
            lines.append(f"   - 選定理由: {s['選定理由']}")
    elif meta:
        lines.append("- 今週は条件に合う銘柄が無く、買付はありませんでした。")
    else:
        lines.append("- 選定メタ情報がありません（買付の詳細はレポート本体を参照）。")
    lines.append("")

    # --- 6. 候補銘柄の全体観 -----------------------------------------------
    stats = _candidate_stats()
    lines.append("## 6. 今週の高配当候補（母集団の全体観）")
    lines.append("")
    if stats:
        lines.append(
            f"- 候補銘柄数: {stats['銘柄数']}銘柄（指数の重複を含む延べ {stats['延べ件数']}件）"
        )
        lines.append(f"- 平均利回り: {stats['平均利回り']:.2f}%")
        top_text = " / ".join(
            f"{name}（{code}）{y:.2f}%" for code, name, y in stats["上位5"] if y
        )
        lines.append(f"- 利回り上位5: {top_text}")
    else:
        lines.append("- 候補一覧を読めませんでした。")
    lines.append("")

    # --- 7. 注意情報 -------------------------------------------------------
    lines.append("## 7. 注意情報")
    lines.append("")
    if meta:
        reductions = meta.get("保有銘柄の減配予想") or {}
        if reductions:
            for code, r in sorted(reductions.items()):
                lines.append(
                    f"- 減配予想（保有中）: {r['会社名']}（{code}）"
                    f" {r['実績']:.0f}円 → {r['予想']:.0f}円"
                )
        else:
            lines.append("- 保有銘柄の減配予想: なし")
        cut = meta.get("減配予想で候補から除外") or []
        excluded = meta.get("買付不可で候補から除外") or []
        lines.append(f"- 減配予想で候補から除外: {'、'.join(cut) if cut else 'なし'}")
        lines.append(
            f"- 買付不可（かぶミニ非対応など）で除外: {'、'.join(excluded) if excluded else 'なし'}"
        )
    else:
        lines.append("- 選定メタ情報がないため、減配・除外の情報はありません。")
    lines.append("")

    return "\n".join(lines)


def build_markdown(df_holding, df_market, df_trend, graph_files, pie_files, date_str):
    """各データフレームからレポートMarkdownの文字列を組み立てる。

    graph_files はトレンド折れ線（build_trend_graphs）、pie_files は構成円グラフ
    （build_composition_graphs）の (表示名, ファイル名) リスト。
    """
    lines = [f"# 週次 高配当株レポート（{date_str}）", ""]

    # --- 今週の一言所感（自動下書きのプレースホルダ）---------------------
    # weekly_report_note.sh から呼ぶ headless Claude が <!-- AUTO_SHOKAN --> 行を
    # たる坊の声の所感1〜2文に置換する。Claude が失敗してもこの枠が残るだけで、
    # たる坊が手で書ける（fail-open）。公開時は見出しの「（…）」を外す。
    lines.append("## 今週の一言所感（自動下書き・公開前に確認）")
    lines.append("")
    lines.append("<!-- AUTO_SHOKAN -->")
    lines.append("")

    # --- 今週の買付 -------------------------------------------------------
    lines.append("## 今週の買付")
    lines.append("")
    bought = pd.DataFrame()
    if df_holding is not None and not df_holding.empty and "日付" in df_holding:
        latest_date = df_holding["日付"].max()
        bought = df_holding[df_holding["日付"] == latest_date]
    if bought.empty:
        lines.append("今週の買付銘柄はありませんでした。")
    else:
        # 利回りは時価総額タブから証券コードで引く
        yield_map = {}
        if df_market is not None and "証券コード" in df_market:
            for _, r in df_market.iterrows():
                yield_map[str(r["証券コード"]).strip()] = _to_number(
                    r.get("配当利回り(%)")
                )
        # 番号付きリスト（1. ）で出力する。note のエディタが入力ルールで番号リスト化し、
        # 番号は自動採番される。社名（コード）の後で改行し、詳細は項目内2行目に置く
        # （継続行 → post_to_note.py 側でソフト改行として送られる）。
        for i, (_, r) in enumerate(bought.iterrows()):
            code = str(r.get("証券コード", "")).strip()
            name = r.get("会社名", "")
            sector = r.get("セクター", "")
            price = _to_number(r.get("取得単価")) or _to_number(r.get("株価"))
            shares = _to_number(r.get("株数"))
            y = yield_map.get(code)
            yield_text = f"利回り{y:.2f}% / " if y is not None else ""
            price_text = f"{price:,.0f}円" if price is not None else "—"
            shares_text = f"{int(shares)}株" if shares is not None else "—株"
            lines.append(f"{i + 1}. **{name}**（{code}）")
            lines.append(f"   {sector} / {yield_text}{price_text} / {shares_text}")
    lines.append("")

    # --- ポートフォリオの育ち具合 ----------------------------------------
    # 「集客に効く数字」だけに絞る：予想年間配当の伸び・利回り・評価額（原価/損益は
    # 括弧でまとめて1行に畳む）。全保有一覧・全セクター表は後段の円グラフに任せる。
    lines.append("## ポートフォリオの育ち具合")
    lines.append("")
    # 取得原価 = Σ(取得単価 × 株数)。列名は 取得単価 を優先し、無ければ 株価。
    total_cost = None
    if df_holding is not None and not df_holding.empty and "株数" in df_holding:
        price_col = "取得単価" if "取得単価" in df_holding else "株価"
        if price_col in df_holding:
            prices = df_holding[price_col].map(_to_number)
            shares = df_holding["株数"].map(_to_number)
            cost = (prices * shares).dropna()
            if not cost.empty:
                total_cost = cost.sum()

    total_value = None
    if df_market is not None and "時価総額" in df_market:
        total_value = df_market["時価総額"].map(_to_number).dropna().sum()

    # 予想年間配当・総時価総額は配当推移タブの最新行から（前回比も）
    annual_div = None
    prev_annual_div = None
    trend_value = None
    df_t = _clean_trend(df_trend)
    if df_t is not None:
        annual_div = df_t.iloc[-1]["総年間配当(円)"]
        trend_value = df_t.iloc[-1]["総時価総額(円)"]
        if len(df_t) >= 2:
            prev_annual_div = df_t.iloc[-2]["総年間配当(円)"]

    if total_value is None:
        total_value = trend_value

    # 予想年間配当（前回比）を先頭に——「育っていく」のが主役のフック。
    if annual_div is not None:
        delta = ""
        if prev_annual_div is not None:
            delta = f"（前回比 {_signed_yen(annual_div - prev_annual_div)}）"
        lines.append(f"- 予想年間配当額: {_yen(annual_div)}{delta}")
    if annual_div is not None and total_value and total_value > 0:
        port_yield = annual_div / total_value * 100
        lines.append(f"- 平均利回り（予想年間配当 ÷ 評価額）: {port_yield:.2f}%")
    # 評価額の行に取得原価・評価損益を括弧でまとめて畳む（行数を減らす）。
    if total_value is not None:
        extra = ""
        if total_cost is not None and total_cost > 0:
            pnl = total_value - total_cost
            pnl_pct = pnl / total_cost * 100
            extra = (
                f"（取得原価 {_yen(total_cost)}"
                f" / 評価損益 {_signed_yen(pnl)}・{_signed_pct(pnl_pct)}）"
            )
        lines.append(f"- 評価額: {_yen(total_value)}{extra}")
    lines.append("")

    # --- トレンドグラフ ---------------------------------------------------
    if graph_files:
        lines.append("## トレンドグラフ")
        lines.append("")
        lines.append("※ 累積見込み配当は実際の受取額ではなく、予想年間配当額を日割りで積み上げた概算です。")
        lines.append("")
        for title, filename in graph_files:
            lines.append(f"![{title}]({filename})")
            lines.append("")

    # --- ポートフォリオの構成（円グラフ）---------------------------------
    # セクター別・銘柄別の構成は表ではなく円グラフで見せる（build_composition_graphs）。
    # 「ちゃんと分散している」が一目で伝わり、19行/32行の表よりノイズが少なく集客に効く。
    if pie_files:
        lines.append("## ポートフォリオの構成")
        lines.append("")
        for title, filename in pie_files:
            lines.append(f"![{title}]({filename})")
            lines.append("")

    # --- 有料記事への導線（CTA）------------------------------------------
    # 旗艦記事への入口を毎週固定で載せる（funnel。docs/weekly-report.md 参照）。
    # 固定文＝毎週同じなので所感の Claude パスは通さない。盛らず・売り込まず、
    # 説得は旗艦記事の無料パートに任せる。リンクは素のURLを単独行に置く
    # （note は自動リンク／カード化しやすく、GitHub/LINE プレビューでも崩れない）。
    # FLAGSHIP_ARTICLE_URL が空なら丸ごと出さない（壊れたリンクを公開しない）。
    if FLAGSHIP_ARTICLE_URL:
        lines.append("## レポートの裏側")
        lines.append("")
        lines.append(
            "このレポートを毎週動かしている「銘柄の選び方」そのものは、別の記事に"
            "全部書いています。罠銘柄の避け方や、分散のかけ方の考え方まで。"
            "よければどうぞ。"
        )
        lines.append("")
        lines.append(FLAGSHIP_ARTICLE_URL)
        lines.append("")

    # --- フッター ---------------------------------------------------------
    lines.append("---")
    lines.append("")
    lines.append(
        "※ 本レポートはスプレッドシートのデータから自動生成しています。"
        "数値は予想配当・スクレイピング時点の株価に基づく概算で、正確性を保証しません。"
        "投資は自己責任でお願いします。"
    )
    return "\n".join(lines)


def main():
    if not SPREADSHEET_KEY or not SERVICE_ACCOUNT_JSON:
        print("[error] SPREADSHEET_KEY / SERVICE_ACCOUNT_JSON が未設定です。")
        return

    credentials = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_JSON, scopes=SCOPE
    )
    gc = gspread.authorize(credentials)

    df_holding = _read_worksheet(gc, "購入履歴")
    df_market = _read_worksheet(gc, "時価総額")
    df_trend = _read_worksheet(gc, "配当推移")

    if df_market is None and df_holding is None and df_trend is None:
        print("[error] 読み込めるタブがありませんでした。")
        return

    date_str = datetime.today().strftime("%Y-%m-%d")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    graph_files = build_trend_graphs(df_trend, df_holding)
    pie_files = build_composition_graphs(df_market)
    markdown = build_markdown(
        df_holding, df_market, df_trend, graph_files, pie_files, date_str
    )

    output_path = os.path.join(OUTPUT_DIR, REPORT_FILENAME)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(markdown)

    print(markdown)
    print(f"\n[ok] レポートを書き出しました: {output_path}")
    for _, filename in graph_files + pie_files:
        print(f"[ok] グラフ: {os.path.join(OUTPUT_DIR, filename)}")

    # --- 所感の素材メモ ---------------------------------------------------
    # 指数の取得は Selenium を使うので重く、壊れやすい。ここで失敗しても
    # レポート本体は既に書けているので、警告だけ出して素材メモは作り続ける。
    # market_index は Selenium を引き込むため、レポート生成だけしたいときに
    # 巻き込まないよう、ここで局所 import する。
    index_summary = []
    try:
        from market_index import build_index_summary

        extra = {}
        stats = _candidate_stats()
        if stats:
            # 指数と同じく「その数字が取れた日」で積む。候補一覧は
            # watch_dividend.py が毎回上書きするので、更新日がその日付になる。
            extra["高配当候補の平均利回り(%)"] = {
                "値": stats["平均利回り"],
                "データ日付": "",  # 指数と違って生表記が無い（日付だけで足りる）
                "日付": stats["更新日"],
                "確定": True,
                "前日比(%)": None,
            }
        index_summary = build_index_summary(date_str, extra_values=extra)
    except Exception as e:
        print(f"[warn] 指数の取得に失敗しました: {e}")

    memo_path = os.path.join(OUTPUT_DIR, MATERIAL_MEMO_FILENAME)
    try:
        memo = build_material_memo(
            df_holding, df_market, df_trend, date_str, index_summary
        )
        with open(memo_path, "w", encoding="utf-8") as f:
            f.write(memo)
        print(f"[ok] 素材メモを書き出しました: {memo_path}")
    except Exception as e:
        print(f"[warn] 素材メモの生成に失敗しました: {e}")


if __name__ == "__main__":
    main()
