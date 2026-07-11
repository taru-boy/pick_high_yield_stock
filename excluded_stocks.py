import csv
import logging

logging.basicConfig(level=logging.ERROR, filename="error.log")

EXCLUDED_STOCKS_CSV = "/home/taru-boy/Desktop/get_stock/excluded_stocks.csv"


def load_excluded_codes(path=EXCLUDED_STOCKS_CSV):
    """
    買付不可などの理由で選定対象から恒久的に外す証券コードのset（文字列）を返す。

    ファイル未配置・読み込み失敗時は空集合を返す（fail-open）。cron週次実行を止めない。

    Args:
        path (str): 除外リストCSVのパス（列: 証券コード, 会社名, 理由）

    Returns:
        set: 除外する証券コードの集合（str）
    """
    try:
        with open(path, encoding="utf-8") as f:
            return {
                row["証券コード"].strip()
                for row in csv.DictReader(f)
                if row.get("証券コード", "").strip()
            }
    except OSError as e:
        logging.error(f"excluded_stocks load failed: {e}")
        return set()
    except KeyError as e:
        logging.error(f"excluded_stocks column missing: {e}")
        return set()
