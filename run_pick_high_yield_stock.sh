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

# 仮想環境を無効化
deactivate

# 現在時刻とテスト終了メッセージをログに出力
echo "スクリプト終了: $(date)" >> /home/taru-boy/Desktop/get_stock/cron.log