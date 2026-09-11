#!/bin/bash
# filepath: /home/taru-boy/Desktop/get_stock/run_pick_high_yield_stock.sh

LOG=/home/taru-boy/Desktop/get_stock/cron.log
SEND_LINE=/home/taru-boy/Desktop/journaling/scripts/send_line.sh

# 週1の処理なので、黙って落ちると1週間気づけない。失敗は必ず LINE に出す。
# 通知自体が失敗しても本処理は止めない。
notify_fail() {
  {
    echo "週次高配当株レポート: $1"
    echo ""
    echo "--- cron.log の末尾 ---"
    tail -n 15 "$LOG"
  } | "$SEND_LINE" >> "$LOG" 2>&1 || true
}

# 2段目以降の失敗はここに溜めて、最後に1通だけ送る（LINE の無料枠は月200通）。
FAILURES=""

# 現在時刻とテスト開始メッセージをログに出力
echo "スクリプト開始: $(date)" >> "$LOG"

# 移動先のディレクトリ
cd /home/taru-boy/Desktop/get_stock || { echo "ディレクトリ移動失敗" >> "$LOG"; exit 1; }

# 仮想環境を有効化
source .venv/bin/activate || { echo "仮想環境有効化失敗" >> "$LOG"; notify_fail "仮想環境の有効化に失敗"; exit 1; }

# Pythonスクリプトを実行
# ここは fail-closed。1銘柄でも取れないと利回りランキングが別物になるため、
# 欠けたまま先へ進めずに中断する（watch_dividend.py がリトライ後に例外を上げる）。
python pick_high_yield_stock.py >> "$LOG" 2>&1 || { echo "スクリプト実行失敗" >> "$LOG"; notify_fail "銘柄選定に失敗（レポートは作られていません）"; exit 1; }

# 以降は fail-open。1つコケてもレポート本体と通知は残す。
# 本体実行後、更新済みのスプレッドシートから週次運用レポートを生成
python note_report.py >> "$LOG" 2>&1 || { echo "レポート生成失敗" >> "$LOG"; FAILURES="$FAILURES レポート生成"; }

# レポートの「一言所感」を Claude(headless) に下書きさせ、本体＋グラフを push して LINE 通知する。
# 順序が肝心: note_report.py（本文）→ weekly_report_note.sh（所感を .md に書く）。
/home/taru-boy/Desktop/journaling/scripts/weekly_report_note.sh >> "$LOG" 2>&1 || { echo "所感の自動下書き失敗" >> "$LOG"; FAILURES="$FAILURES 所感の自動下書き"; }

# 所感入りの完成版レポートを note の下書きに流し込む（公開はしない）。
python post_to_note.py >> "$LOG" 2>&1 || { echo "note下書き保存失敗" >> "$LOG"; FAILURES="$FAILURES note下書き保存"; }

# 仮想環境を無効化
deactivate

if [ -n "$FAILURES" ]; then
  notify_fail "一部の工程が失敗しました:$FAILURES"
fi

# 現在時刻とテスト終了メッセージをログに出力
echo "スクリプト終了: $(date)" >> "$LOG"
