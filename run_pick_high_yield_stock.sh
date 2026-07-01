#!/bin/bash
# filepath: /home/taru-boy/Desktop/get_stock/run_pick_high_yield_stock.sh

# 現在時刻とテスト開始メッセージをログに出力
echo "スクリプト開始: $(date)" >> /home/taru-boy/Desktop/get_stock/cron.log

# 移動先のディレクトリ
cd /home/taru-boy/Desktop/get_stock || { echo "ディレクトリ移動失敗" >> /home/taru-boy/Desktop/get_stock/cron.log; exit 1; }

# 仮想環境を有効化
source .venv/bin/activate || { echo "仮想環境有効化失敗" >> /home/taru-boy/Desktop/get_stock/cron.log; exit 1; }

# Pythonスクリプトを実行
python pick_high_yield_stock.py >> /home/taru-boy/Desktop/get_stock/cron.log 2>&1 || { echo "スクリプト実行失敗" >> /home/taru-boy/Desktop/get_stock/cron.log; exit 1; }

# 本体実行後、更新済みのスプレッドシートから週次運用レポートを生成（失敗しても止めない）
python note_report.py >> /home/taru-boy/Desktop/get_stock/cron.log 2>&1 || echo "レポート生成失敗" >> /home/taru-boy/Desktop/get_stock/cron.log

# レポートの「一言所感」を Claude(headless) に下書きさせ、本体＋グラフを push して LINE 通知する。
# 順序が肝心: note_report.py（本文）→ weekly_report_note.sh（所感を .md に書く）。失敗しても止めない。
/home/taru-boy/Desktop/journaling/scripts/weekly_report_note.sh >> /home/taru-boy/Desktop/get_stock/cron.log 2>&1 || echo "所感の自動下書き失敗" >> /home/taru-boy/Desktop/get_stock/cron.log

# 所感入りの完成版レポートを note の下書きに流し込む（公開はしない・失敗しても止めない）。
python post_to_note.py >> /home/taru-boy/Desktop/get_stock/cron.log 2>&1 || echo "note下書き保存失敗" >> /home/taru-boy/Desktop/get_stock/cron.log

# 仮想環境を無効化
deactivate

# 現在時刻とテスト終了メッセージをログに出力
echo "スクリプト終了: $(date)" >> /home/taru-boy/Desktop/get_stock/cron.log