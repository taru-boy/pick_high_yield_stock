# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## アプリケーションの実行方法

```bash
# 仮想環境を有効化（Python 3.11.2）
source .venv/bin/activate

# メインスクリプトを手動実行
python pick_high_yield_stock.py

# cronラッパー経由での実行（venv有効化・ログ記録を含む）
bash run_pick_high_yield_stock.sh
```

cronジョブは毎週金曜日16:30 JSTに実行（大引15:30の1時間後。2026-09-19に金曜20:00→土曜04:40へ移したが、結果が届くのが土曜朝になり金曜のうちに見られなくなったため、2026-09-25に金曜16:30へ前倒しした。代償として、週次レポートの一言所感を書く Claude 実行が金曜夕方の対話と同じ5時間の使用量ウィンドウに入る。LINE_DEFER は付けないので通知は即時に届く）。149銘柄のNikkeiページに3秒＋0〜1秒のジッターを空けてリクエストを送るため、1回の実行に7〜10分かかる。

## アーキテクチャ

高配当日本株を毎週選定してGoogleスプレッドシートに記録するシステム。

**データフロー:**
1. `pick_high_yield_stock.py`（オーケストレーター）がGoogleスプレッドシートの「購入履歴」タブから購入履歴を取得
2. `holding_calculator.py` がリアルタイム価格スクレイピングでセクター別の現在の時価総額を計算
3. `get_high_dividend_stock_code.py` がSeleniumで3つのNikkeiインデックス（NK225HDY・NKPHD・NKCDG）から銘柄コードを取得。業種は指数ページのもの（日経の分類）を使わず、`tse_sector.py` の東証33業種の対応表で付け直す
4. `watch_dividend.py` がBeautifulSoup/requestsで各銘柄の配当利回りと株価をスクレイピングし、`high_dividend_stocks.csv` に保存
5. 候補銘柄を利回り順でGoogleスプレッドシートの「今週の銘柄」タブにアップロード
6. `stock_selector.py` が3段階アルゴリズムで最適銘柄を選定し、`select_stocks(n=2)` で毎週2銘柄を選ぶ。別セクター優先は第1・2段階のみで効き、第3段階（現状の主経路）はセクター20%上限のみで制御するため、同一週の2銘柄が同一セクターになることもある。このとき `excluded_stocks.py` の買付不可リストと `edinet_dividend.py` の減配銘柄を合わせた `cut_codes` を除外する
7. 選定した各銘柄を「購入履歴」タブに1銘柄あたり10,000円以上の購入として追記
8. 選定と並行して、保有銘柄についても `edinet_dividend.py` の `get_dividend_reductions()` で減配予想（性向条件なし）を検知する。売る/持つ/除外の判断は行わず検知のみ
9. `line_notify.py` が選定した2銘柄の詳細と（あれば）保有銘柄の減配警告をLINE（Messaging API push）に通知。選定0銘柄でも減配警告があればそれだけ通知する

**各モジュールの役割:**
- `get_high_dividend_stock_code.py` — Seleniumスクレーパー。NikkeiインデックスページからJS描画後の銘柄コード＋セクターを取得（セクターは返すが、呼び出し側は使わない）
- `tse_sector.py` — 東証33業種の対応表。`sector33.csv`（JPX「東証上場銘柄一覧」data_j.xlsx から作る）が正で、`refresh_sector_map()` が毎回 JPX から取り直して中身が変わったときだけ書き換える（JPX の更新は月1回。取得失敗は既存CSVで続行＝fail-open）。`load_sector_map()`／`sector_of()` で証券コード→業種を引き、対応表に無いコードは `不明`
- `migrate_sector33.py` — 2026-10 の業種区分切り替え用の一度きりの道具。`--report` で影響（新旧の業種構成・未保有業種・選定の変化）を表示、`--migrate` で購入履歴タブの「セクター」列を33業種に書き換える（旧値は `sector_migration_2026-10.csv`）
- `watch_dividend.py` — BeautifulSoupスクレーパー（`nikkei.com/nkd/company/`）。配当利回りと株価のDataFrameを返す
- `stock_selector.py` — 3段階選定: (1) 未保有セクターの利回りTop10, (2) 複数インデックス重複銘柄（未保有セクター優先）, (3) セクターリバランス（保有比率<4%かつセクター比率<20%の場合のみ。この段階は未保有セクターかどうかを見ない。第1・2段階は未保有業種の銘柄が利回り上位10・重複銘柄に入らないと動かないため、現状はここが主経路になり、同一セクターの銘柄が選ばれることもある）。全段階で `cut_codes`（来期減配予想の銘柄）を除外する。`select_stocks(n=2)` がこの単一銘柄選定（`select_stock`）を反復適用し、選定済みコードを除外集合に・選定済みセクターを保有済みセクターに加えて毎週2銘柄を選ぶ（コード重複は常に回避、セクター重複回避は第1・2段階でのみ効く）
- `edinet_dividend.py` — EDINET DB APIで配当・EPS予想を取得する。`get_dividend_cut_codes()` は候補銘柄向けに「減配かつ予想配当性向>100%」の証券コード集合を返す（選定フィルタ）。`get_dividend_reductions()` は保有銘柄向けに性向条件を課さず「減配予想が出たこと自体」を `{証券コード: (実績配当, 予想配当)}` で返す（保有銘柄監視・通知専用、選定ロジックには影響しない）。両者とも内部の `_latest_reduction()`（減配判定の共通ヘルパー）を土台にしている
- `excluded_stocks.py` — `excluded_stocks.csv` を読んで買付不可銘柄の証券コード集合を返す（買付不可フィルタ）。標準ライブラリの `csv` のみで外部依存なし
- `holding_calculator.py` — 購入履歴から保有株数を集計し、現在価格を取得してセクター別にグループ化（業種は購入履歴の列ではなく33業種の対応表から引く）
- `line_notify.py` — LINE Messaging APIの push で選定結果を通知する。`send_line(text)` が `.env` の `CHANNEL_ACCESS_TOKEN`/`USER_ID` を使って送信。`urllib` のみで外部依存なし。トークン未設定・API/ネットワークエラー時は**送らず False を返すだけ（fail-open）**で例外を投げず、cron週次実行を止めない

## Googleスプレッドシート構成

| タブ名 | 用途 |
|---|---|
| 購入履歴 | 購入履歴（ポートフォリオ状態のソース） |
| 時価総額 | セクター別の現在保有状況と時価総額 |
| 今週の銘柄 | 今週の高配当候補銘柄（利回り順） |

## 週次レポート生成（選定のあと）

**詳細は [docs/weekly-report-pipeline.md](docs/weekly-report-pipeline.md)。** ここは入口だけ。

`run_pick_high_yield_stock.sh` は銘柄選定で終わらず、そのまま週次運用レポートの生成まで一気通貫で回す：

1. `pick_high_yield_stock.py` — 選定・スプレッドシート更新・LINE 通知。**ここだけ fail-closed**
2. `note_report.py` — スプレッドシート3タブを読むだけでレポート本体・素材メモ・グラフ5枚を生成
3. `journaling/scripts/weekly_report_note.sh` — **journaling 側**。Claude が一言所感の候補6本を書き、push して LINE 通知
4. `post_to_note.py` — 所感入りの完成版を note の**下書きに保存**（公開はしない）

2段目以降は fail-open で、失敗しても最後にまとめて1通 LINE に出るだけ。出力先は `/home/taru-boy/Desktop/journaling/note/reports/` に**絶対パスで固定**されている（`note_report.py:51` ほか）。**journaling 側でこのパスを動かすと黙って壊れる**——対応表は docs 側の「journaling への書き込み口」にある。

ドキュメントの分担は「壊れたとき、どっちのコードを直しに行くか」：数字・銘柄・グラフ・スクレイピングは当リポジトリの `docs/`、声（一言所感）・note の見せ方・公開手順は `~/Desktop/journaling/docs/weekly-report.md`。

## 実装上の重要な注意点

- **業種は東証33業種**: 2026-10 に日経の指数ページの業種（28種）から切り替えた（Kindle 本と物差しをそろえるため）。列名は互換のため「セクター」のまま。業種は**毎回証券コードから `sector33.csv` で引く**ので、購入履歴タブの「セクター」列は人が読むための値で、計算には使わない。対応表が空なら `pick_high_yield_stock.py` は止まる（全部「不明」のまま選ぶと20%上限が効かないため）。対応表に無い候補（新規上場直後など）は `cut_codes` に入れて選定から外し、LINE に1行出す。`sector33.csv` は週次 cron の最後に履歴CSVと一緒に commit・push される。依存に `openpyxl`（xlsx の読み込み）が要る

- **絶対パスを使用**: cronで動作させるため、全ファイルパスは絶対パスでハードコードされている（例: `/home/taru-boy/Desktop/get_stock/`）。相対パスに変更しないこと。
- **Seleniumのボット対策**: `get_high_dividend_stock_code.py` は `navigator.webdriver` を隠し、カスタムUser-Agentを設定し、ユニークな `/tmp/` Chromeプロファイルを使ってNikkeiのBot検知を回避している。
- **2種類のスクレイピング戦略**: インデックスページ（JS描画が必要）はSeleniumを使用。個別銘柄ページは `watch_dividend.py` がrequests+BeautifulSoupで処理（高速、1銘柄あたり約2秒）。
- **`watch_dividend.py` のエラー処理**: 配当利回りのパース失敗は日付と銘柄名を `error.log` に記録して続行する。
- **購入金額**: 1銘柄あたり10,000円以上。現在株価で割って株数を切り上げる（`math.ceil`）。毎週2銘柄を選定し、それぞれ同様に購入として追記する。
- **環境変数**: `.env` に `SPREADSHEET_KEY`（GoogleスプレッドシートのドキュメントID）、`SERVICE_ACCOUNT_JSON`（Googleスプレッドシート認証用サービスアカウントJSONのパス）、`EDINETDB_API_KEY`（EDINET DB APIキー。減配フィルタ用）、`CHANNEL_ACCESS_TOKEN`・`USER_ID`（LINE Messaging API push用。選定結果のLINE通知）が必要。
- **買付不可フィルタ（除外リスト）**: 証券会社のサービス上そもそも買い付けられない銘柄（例: 野村不動産ホールディングス 3231 はかぶミニ非対応）を `excluded_stocks.csv`（列: `証券コード,会社名,理由`）に列挙して恒久除外する。銘柄を増やすときはこのCSVに1行足すだけでよく、コード変更は不要。`pick_high_yield_stock.py` が `load_excluded_codes()` で読み、**`candidate_codes()` の結果から先に減算してからEDINETに問い合わせる**（買えない銘柄で無料枠100回/日を消費しないため）。その後 `cut_codes = 減配銘柄 | 除外リスト` として `select_stocks()` に渡すので、`stock_selector.py` の3段階すべてに効く（全段が `str(証券コード) in cut_codes` で判定）。CSV未配置・読み込み失敗時は**空集合を返すだけ（fail-open）**で例外を投げず、cron週次実行を止めない。除外が実際に候補へ効いた場合のみ `買付不可のため除外: [...]` をログ出力する。
- **減配フィルタ（EDINET DB・選定候補向け）**: `edinet_dividend.py` の `get_dividend_cut_codes()` がEDINET DB API（`edinetdb.jp`）の決算短信(`/earnings`)から配当・EPSデータを取得し、**「減配（来期予想<直近実績）かつ予想配当性向>100%」の銘柄のみ**を選定候補から除外する。表示利回り自体が予想ベース（[watch_dividend.py](watch_dividend.py)）なので大幅減配は利回り低下で自然に候補落ちする。市況ピークからの正常化・下限着地など、減配でも利益で配当を賄えている銘柄（例: JFE 100→80円だが性向67.8%）は除外しないのが狙い。`_latest_dividends()` がearnings配列（新しい順）から**最新の実績・予想・予想EPSを別々に**最初のnon-null値で拾い（同一行に揃っている必要はない＝期中の予想修正を反映しstale化を防ぐ）、`_latest_reduction()` が `forecast < actual` を減配と判定する。実績は分割調整後の `adjusted_annual_dividend_per_share` を優先し、無ければ生値 `dividend_per_share` にフォールバック。ただし調整後実績は経路依存で、分割の効力発生をまたいでも未調整のまま返ることがある（8309 は2026-08-01の1:4分割後も185.0のまま、予想は分割後基準の47.5で、減配と誤検知した）。そこでearningsに分割シグナル（`forecast_split_adjustment_factor`・`forecast_share_basis`）がある銘柄は `_align_split_basis()` で実績と予想の株数基準をそろえてから比較する（確定できない側は予想/実績の比が1に最も近い解釈を採る。限界として、分割と同時に1/√分割比を超える減配は見逃しうる）。分割シグナルが無い銘柄は従来どおり、生値フォールバック時のみ比率ヒューリスティック `_looks_like_split` で分割誤検知をガードする。テストは `python -m unittest discover -s tests -v`（8309の実レスポンスを `tests/fixtures/` に保存）。予想配当性向は `_exceeds_full_payout()` が `forecast / forecast_eps > 1.0` で判定し、**予想EPS≤0（赤字予想）で配当が正なら100%超とみなす**。判定は必ず `is None`（`0.0`＝無配転落を欠損扱いしない）。予想EPS未開示時は性向判定不能として除外しない（fail-open）。レート節約のため`stock_selector.candidate_codes()`が返す候補集合のみを叩く（無料枠100回/日）。証券コード→EDINETコードの解決は`/companies?per_page=5000`の一括取得で行い、`pick_high_yield_stock.py` が `build_code_map()` を1回だけ呼んで候補チェック・保有チェックの両方に共有する。APIエラー・予想未開示時は**除外せず続行（fail-open）**し、cron週次実行を止めない。
- **保有銘柄の減配監視（通知のみ・選定ロジックには影響しない）**: `get_dividend_reductions()` が保有銘柄（`df_latest_holdings`の証券コード）に対して同じ`_latest_reduction()`で減配予想を検知するが、**性向>100%の条件は課さない**（減配発表そのものに気づくのが目的）。検知した銘柄は「⚠️保有銘柄の減配予想」としてLINE通知の先頭に載る（選定0銘柄の週は単独で通知）。売る/持つ/除外の判断は行わず人間が行う。EDINET側で減配予想データが残る限り**毎週再通知される**（リマインダーとして機能させる設計であり、既読管理や状態ファイルは持たない）。APIキー未設定・コード未解決・APIエラー時はfail-openでスキップする。
- **決算発表当日のデータ反映**: `edinetdb.jp`は毎日8:00 JST自動更新のため、金曜夕方発表の決算はcron実行（金曜16:30。edinetdbの更新は翌朝8:00）に間に合わず、翌週以降の実行で初めて減配フィルタ・保有監視の両方に反映される。この関係は金曜20:00・土曜04:40実行だった頃から変わっていないため、発表当日〜翌営業日の銘柄はEDINET側データが未反映のまま選定される可能性がある点に留意（2026-07-10 ディップ(2379)決算はこのケース。ただし減配ではなかったため実害はなかった）。
