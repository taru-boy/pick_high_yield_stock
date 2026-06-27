"""週次運用レポートを生成するスクリプト。

毎週金曜に pick_high_yield_stock.py が更新する Google スプレッドシートの
3タブ（購入履歴 / 時価総額 / 配当推移）を**読むだけ**で、note記事用の
Markdown とトレンドグラフ（PNG）を生成する。再スクレイピングはしない。

cron では run_pick_high_yield_stock.sh の末尾から本体実行の後に呼ばれる。
ただしスプレッドシートを読むだけなので、いつでも単体で再生成できる（疎結合）。

既存方針に倣い fail-open（データ不足時は例外を投げず警告して終了）。
"""

import os
import re
from datetime import datetime

import gspread
import matplotlib
import pandas as pd
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

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
# 手書きのnote下書き（drafts/）とは性格が違う機械生成物なので専用フォルダに分ける。
# ファイル名は固定で毎週上書きする（最新版が1セットだけ残る運用。公開済みのアーカイブはnote側が持つ）。
OUTPUT_DIR = "/home/taru-boy/Desktop/journaling/high_dividend_stock_report"
REPORT_FILENAME = "週次運用レポート.md"

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


def build_trend_graphs(df_trend):
    """配当推移タブからトレンドグラフのPNGを生成し、(表示名, ファイル名) のリストを返す。

    日本語フォントが見つかればラベルも日本語にする。見つからない環境では
    豆腐化を避けるため英語ラベルにフォールバックする（JP_FONT で判定）。
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
        ("総時価総額(円)", "総時価総額（円）", "Total Market Value (JPY)", "trend_market_value"),
        ("累積見込み配当(円)", "累積見込み配当（円・概算）", "Cumulative Dividend, est. (JPY)", "trend_cumulative_dividend"),
    ]
    filenames = []
    for column, jp_title, en_title, slug in specs:
        title = jp_title if JP_FONT else en_title
        if df[column].dropna().empty:
            continue
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot(df["日付"], df[column], marker="o", linewidth=2, color="#2a7ae2")
        ax.fill_between(df["日付"], df[column], alpha=0.12, color="#2a7ae2")
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


def build_markdown(df_holding, df_market, df_trend, graph_files, date_str):
    """各データフレームからレポートMarkdownの文字列を組み立てる。"""
    lines = [f"# 週次 高配当株レポート（{date_str}）", ""]

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
        circled = "①②③④⑤⑥⑦⑧⑨⑩"
        for i, (_, r) in enumerate(bought.iterrows()):
            code = str(r.get("証券コード", "")).strip()
            name = r.get("会社名", "")
            sector = r.get("セクター", "")
            price = _to_number(r.get("取得単価")) or _to_number(r.get("株価"))
            shares = _to_number(r.get("株数"))
            mark = circled[i] if i < len(circled) else f"{i + 1}."
            y = yield_map.get(code)
            yield_text = f"利回り{y:.2f}% / " if y is not None else ""
            price_text = f"{price:,.0f}円" if price is not None else "—"
            shares_text = f"{int(shares)}株" if shares is not None else "—株"
            lines.append(f"- {mark}**{name}**（{code}）")
            lines.append(f"  {sector} / {yield_text}{price_text} / {shares_text}")
    lines.append("")

    # --- ポートフォリオ全体 ----------------------------------------------
    lines.append("## ポートフォリオ全体")
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

    if total_cost is not None:
        lines.append(f"- 取得原価: {_yen(total_cost)}")
    if total_value is not None:
        lines.append(f"- 現在評価額: {_yen(total_value)}")
    if total_cost is not None and total_value is not None and total_cost > 0:
        pnl = total_value - total_cost
        pnl_pct = pnl / total_cost * 100
        lines.append(f"- **評価損益: {_signed_yen(pnl)}（{_signed_pct(pnl_pct)}）**")
    if annual_div is not None:
        delta = ""
        if prev_annual_div is not None:
            delta = f"（前回比 {_signed_yen(annual_div - prev_annual_div)}）"
        lines.append(f"- 予想年間配当額: {_yen(annual_div)}{delta}")
    if annual_div is not None and total_value and total_value > 0:
        port_yield = annual_div / total_value * 100
        lines.append(f"- 平均利回り（予想年間配当 ÷ 評価額）: {port_yield:.2f}%")
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

    # --- セクター別構成 ---------------------------------------------------
    if df_market is not None and "セクター" in df_market and "時価総額" in df_market:
        dfm = df_market.copy()
        dfm["_cap"] = dfm["時価総額"].map(_to_number)
        dfm = dfm.dropna(subset=["_cap"])
        sector_cap = dfm.groupby("セクター")["_cap"].sum().sort_values(ascending=False)
        grand_total = sector_cap.sum()
        if grand_total > 0:
            lines.append("## セクター別構成")
            lines.append("")
            lines.append("| セクター | 評価額 | 構成比 |")
            lines.append("|---|--:|--:|")
            for sector, cap in sector_cap.items():
                lines.append(f"| {sector} | {_yen(cap)} | {cap / grand_total * 100:.1f}% |")
            lines.append("")

        # --- 保有銘柄一覧（評価額降順）-----------------------------------
        if "会社名" in dfm:
            dfm_sorted = dfm.sort_values("_cap", ascending=False)
            lines.append("## 保有銘柄一覧")
            lines.append("")
            lines.append("| 銘柄 | セクター | 評価額 |")
            lines.append("|---|---|--:|")
            for _, r in dfm_sorted.iterrows():
                lines.append(
                    f"| {r.get('会社名', '')}（{str(r.get('証券コード', '')).strip()}） "
                    f"| {r.get('セクター', '')} | {_yen(r['_cap'])} |"
                )
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

    graph_files = build_trend_graphs(df_trend)
    markdown = build_markdown(df_holding, df_market, df_trend, graph_files, date_str)

    output_path = os.path.join(OUTPUT_DIR, REPORT_FILENAME)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(markdown)

    print(markdown)
    print(f"\n[ok] レポートを書き出しました: {output_path}")
    for _, filename in graph_files:
        print(f"[ok] グラフ: {os.path.join(OUTPUT_DIR, filename)}")


if __name__ == "__main__":
    main()
