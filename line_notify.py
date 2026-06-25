import os
import json
import urllib.request
import urllib.error

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LIMIT = 4900  # LINEは1吹き出し最大5000字。安全側で4900に制限


def _split_bubbles(text):
    """改行を尊重しつつ LIMIT 以下の吹き出しに分割する"""
    bubbles = []
    cur = ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > LIMIT:
            if cur:
                bubbles.append(cur)
            cur = line
        else:
            cur = cur + "\n" + line if cur else line
    if cur:
        bubbles.append(cur)
    return bubbles


def send_line(text):
    """LINE Messaging API の push でテキストを送信する。

    成功で True、失敗（トークン未設定・HTTP/ネットワークエラー）で False を返す。
    例外は投げない（fail-open: 減配フィルタと同様、cron週次実行を止めないため）。

    認証情報は .env の CHANNEL_ACCESS_TOKEN / USER_ID から取得する
    （呼び出し側で load_dotenv 済みの前提）。
    """
    text = (text or "").strip()
    if not text:
        return False

    token = os.getenv("CHANNEL_ACCESS_TOKEN")
    to = os.getenv("USER_ID")
    if not token or not to:
        print("LINE送信スキップ: CHANNEL_ACCESS_TOKEN または USER_ID が未設定")
        return False

    bubbles = _split_bubbles(text)
    # LINEは1リクエストで最大5吹き出し
    batches = [bubbles[i:i + 5] for i in range(0, len(bubbles), 5)]

    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    }

    for batch in batches:
        body = {"to": to, "messages": [{"type": "text", "text": b} for b in batch]}
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            LINE_PUSH_URL, data=data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")
            print(f"LINE送信失敗 HTTPError {e.code}: {detail}")
            return False
        except urllib.error.URLError as e:
            print(f"LINE送信失敗 URLError: {e.reason}")
            return False
        except Exception as e:  # fail-open: 想定外のエラーでも止めない
            print(f"LINE送信失敗: {e}")
            return False
    return True


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv(dotenv_path="/home/taru-boy/Desktop/get_stock/.env")
    msg = sys.stdin.read() if not sys.stdin.isatty() else "LINE通知テスト"
    print(send_line(msg))
