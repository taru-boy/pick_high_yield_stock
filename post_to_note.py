"""週次運用レポートを note の「下書き」に流し込むスクリプト（自動公開はしない）。

high_dividend_stock_report/週次運用レポート.md（所感入りの完成版）を読み、
note の新規投稿エディタに タイトル・本文・画像5枚 を入れ、マガジンを指定して
**下書き保存**するところまでを Selenium で自動化する。公開ボタンは押さない。
公開はたる坊が note 上で所感を確認・手直ししてから手で行う（品質ゲート）。

cron では run_pick_high_yield_stock.sh から weekly_report_note.sh の後に呼ばれる。
レポート .md があればいつでも単体で再実行できる（疎結合）。失敗しても止めない設計
（既存スクリプトの fail-open 思想）。

────────────────────────────────────────────────────────────────────────
重要：note には公式の投稿APIが無く、エディタは独自のJSブロックエディタ。
このスクリプトのセレクタ（NOTE_SELECTORS）と手順は **実サイトでの確認・微調整が前提**
です。note 側の UI 変更で壊れやすい部分なので、`SELECTOR 要確認` のコメント箇所を
ブラウザの開発者ツールで突き合わせてから本番投入してください。

使い方:
  # 1) 初回だけ：表示ありブラウザで note にログイン（2段階認証もここで通す）。
  #    ログイン状態は専用プロファイル(~/.note_profile)に残り、以後は自動で開ける。
  python post_to_note.py --login

  # 2) 下書き保存（既定）。note のエディタは headless だと描画されないため headful で動く
  #    （常駐機の DISPLAY=:0 を使う。スクリプトが未設定時に補うので cron でもそのまま動く）。
  python post_to_note.py            # 下書き保存（headful）

  # セレクタ調査用の診断モード：
  python post_to_note.py --dump          # 実DOMを note_dom_dump.html / note_dump.png に
  python post_to_note.py --probe-image   # 画像挿入の仕組みを確認
────────────────────────────────────────────────────────────────────────
"""

import argparse
import os
import re
import subprocess
import sys
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# --- 設定 -----------------------------------------------------------------
REPORT_DIR = "/home/taru-boy/Desktop/journaling/high_dividend_stock_report"
REPORT_MD = os.path.join(REPORT_DIR, "週次運用レポート.md")
PROFILE_DIR = os.path.expanduser("~/.note_profile")  # ログイン維持用の永続プロファイル
CHROMEDRIVER_PATH = "/usr/bin/chromedriver"  # 既存スクリプトと同じ
MAGAZINE_NAME = "高配当株・週次運用ログ"  # 入れ先マガジン
NOTE_TOP_URL = "https://note.com/"
NOTE_NEW_URL = "https://note.com/notes/new"  # 新規投稿エディタ
ERROR_SHOT = os.path.join(REPORT_DIR, "note_post_error.png")  # 失敗時のスクショ
SEND_LINE = "/home/taru-boy/Desktop/journaling/scripts/send_line.sh"

# note のエディタ DOM セレクタ。2026-06 時点の editor.note.com の実DOMで確認済み。
# note の UI 変更でここが最初に壊れる。崩れたら `--dump` で実DOMを取り直して合わせる。
NOTE_SELECTORS = {
    # タイトル入力（textarea[placeholder="記事タイトル"]）
    "title": (By.CSS_SELECTOR, "textarea[placeholder='記事タイトル']"),
    # 本文エディタ（ProseMirror の contenteditable）
    "body": (By.CSS_SELECTOR, "div.ProseMirror[contenteditable='true']"),
    # 本文の「+」ブロック挿入ハンドル（インライン画像はここから）
    "block_plus": (By.CSS_SELECTOR, "button[aria-label='メニューを開く']"),
    # 「+」メニュー内の「画像」項目
    "menu_image": (By.XPATH, "//button[normalize-space()='画像']"),
    # 「画像」選択で現れる本文画像の file input（multiple。複数枚を一括送信できる）
    "image_input": (By.ID, "note-editor-image-upload-input"),
    # 下書き保存ボタン（テキスト完全一致。id は動的なので使わない）
    "save_draft": (By.XPATH, "//button[normalize-space()='下書き保存']"),
    # 「公開に進む」ボタン（★絶対に押さない。誤クリック回避の参照用）
    "to_publish": (By.XPATH, "//button[normalize-space()='公開に進む']"),
}

WAIT = 40  # 要素待ちの最大秒（エディタ SPA の描画が遅い）


def log(msg):
    print(f"[post_to_note] {msg}", flush=True)


def notify_line(text):
    """失敗などを LINE に流す（送れなくても致命的にはしない）。"""
    try:
        subprocess.run([SEND_LINE], input=text.encode("utf-8"), check=False, timeout=30)
    except Exception as e:  # noqa: BLE001
        log(f"LINE 通知に失敗（無視）: {e}")


# 箇条書き/番号付き項目「内」の改行を表す内部マーカー。
# parse_report がインデント継続行をこのマーカーで連結し、送信側（cmd_post）が
# Shift+Enter（項目内ソフト改行）に変換する。単独 Enter＝「新しい項目」と区別するため。
SOFT_BREAK = "\x00"


# --- レポート .md のパース ------------------------------------------------
def parse_report(md_path):
    """レポート Markdown を (タイトル, ブロックのリスト) に分解する。

    - タイトル: 先頭の `# ...` 行（記事タイトル欄に入れる）。
    - ブロック: 本文を順番どおりに h2/li/ol/p/image に分けて並べる。markdown 記号
      （`## ` 見出し / `- ` 箇条書き / `1. ` 番号付き / `**` 太字）の意味は残し、リストは
      先頭の記号を外してテキストだけ持つ（リスト化・採番は送信側 cmd_post で制御する）。
    - リスト項目のインデント継続行（詳細行）は SOFT_BREAK で連結し、送信側で項目内の
      ソフト改行（Shift+Enter）にする。
    - 画像参照 `![alt](file.png)` はその位置に ("image", パス) として挟む（見出しの
      下に正しく入る）。HTMLコメント（未置換の所感プレースホルダ）は除く。
    """
    with open(md_path, encoding="utf-8") as f:
        raw = f.read()

    title = None
    blocks = []  # ("h2","## …") | ("li",text) | ("ol",text) | ("p",str) | ("image",path)
    for line in raw.split("\n"):
        s = line.rstrip()
        # 記事タイトル（先頭の # 行）
        m_title = re.match(r"^#\s+(.*)$", s)
        if m_title and title is None:
            title = m_title.group(1).strip()
            continue
        # 画像参照
        m_img = re.match(r"^!\[[^\]]*\]\(([^)]+)\)\s*$", s)
        if m_img:
            blocks.append(("image", os.path.join(REPORT_DIR, m_img.group(1).strip())))
            continue
        # 未置換の所感プレースホルダ等のHTMLコメント
        if s.strip().startswith("<!--") and s.strip().endswith("-->"):
            continue
        # 空行は段落を作らない（空ブロックの量産を防ぐ。note 側が見出し等に余白を付ける）
        if s.strip() == "":
            continue
        # インデントされた継続行は直前のブロックに連結（リスト項目の2行目=詳細行など）。
        # リスト項目内は SOFT_BREAK で繋ぎ、送信側で項目内ソフト改行（Shift+Enter）にする。
        if re.match(r"^\s+\S", line) and blocks and blocks[-1][0] in ("li", "ol", "p"):
            kind, prev = blocks[-1]
            joiner = SOFT_BREAK if kind in ("li", "ol") else "　"
            blocks[-1] = (kind, f"{prev}{joiner}{s.strip()}")
            continue
        # 見出し（"## …"）。markdown を残す（送信側で入力ルールが h2 化する）。
        if re.match(r"^#{2,6}\s+", s):
            blocks.append(("h2", s.strip()))
            continue
        # 番号付きリスト項目（"1. " を外してテキストだけ持つ。番号は送信側で自動採番）
        m_ol = re.match(r"^\d+\.\s+(.*)$", s)
        if m_ol:
            blocks.append(("ol", m_ol.group(1).strip()))
            continue
        # 箇条書き項目（先頭の "- " を外してテキストだけ持つ。リスト化は送信側で制御）
        m_li = re.match(r"^-\s+(.*)$", s)
        if m_li:
            blocks.append(("li", m_li.group(1).strip()))
            continue
        # それ以外は段落（"---" など markdown もそのまま）
        blocks.append(("p", s.strip()))

    if title is None:
        title = "週次 高配当株レポート"
    return title, blocks


# --- ブラウザ -------------------------------------------------------------
def setup_driver(headless=False):
    """永続プロファイルで Chrome を起動する。プロファイルにログインが残る。

    note のエディタ(editor.note.com)は headless だと永遠にローディングのまま描画されない
    （headless 検出かGPU依存）。このため既定は headful。常駐機の Xwayland(:0) を使う。
    cron でも DISPLAY=:0 を使えるよう、未設定なら :0 を補う。
    """
    # cron は DISPLAY/XAUTHORITY を持たない。常駐機の Xwayland(:0) に繋げるよう補う。
    os.environ.setdefault("DISPLAY", ":0")
    os.environ.setdefault("XAUTHORITY", os.path.expanduser("~/.Xauthority"))
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")  # ★ここがログイン維持の肝
    options.add_argument("--window-size=1280,1600")
    service = ChromeService(CHROMEDRIVER_PATH)
    driver = webdriver.Chrome(service=service, options=options)
    return driver


def wait_for_editor(driver, timeout=WAIT):
    """エディタ SPA の本文(contenteditable)が現れるまで待つ。出たら True。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        n = driver.execute_script(
            "return document.querySelectorAll(\"div.ProseMirror[contenteditable='true']\").length;"
        )
        if n and n > 0:
            time.sleep(2)  # 描画の落ち着き待ち
            return True
        time.sleep(2)
    return False


def cmd_login():
    """表示ありブラウザで note を開き、手動ログインを待つ（初回セットアップ用）。"""
    os.makedirs(PROFILE_DIR, exist_ok=True)
    driver = setup_driver(headless=False)
    try:
        driver.get(NOTE_TOP_URL)
        log("ブラウザで note にログインしてください（2段階認証も）。")
        log("ログインが終わったら、この端末で Enter を押すと終了します。")
        input("  ログイン完了後に Enter > ")
        log(f"プロファイルに保存: {PROFILE_DIR}")
    finally:
        driver.quit()


DOM_DUMP_HTML = os.path.join(REPORT_DIR, "note_dom_dump.html")
DOM_DUMP_PNG = os.path.join(REPORT_DIR, "note_dump.png")


def cmd_dump(headless=True):
    """新規エディタを開き、操作対象になりそうな要素の実DOMを書き出す（セレクタ特定用）。

    何もタイプせず・公開せず、input/textarea/[contenteditable]/button の outerHTML を
    note_dom_dump.html に、画面を note_dump.png に保存して終了する。
    Claude がこの2ファイルを読んで NOTE_SELECTORS を実DOMに合わせる。
    """
    if not os.path.isdir(PROFILE_DIR):
        log("ログイン用プロファイルがありません。先に `python post_to_note.py --login` を実行してください。")
        return 1
    driver = setup_driver(headless=headless)
    try:
        driver.get(NOTE_NEW_URL)
        # エディタ SPA は描画にしばらくかかる（最初はローディングのドットだけ）。
        # contenteditable か textarea が現れるまで最大 40 秒ポーリングしてから dump する。
        deadline = time.time() + 40
        while time.time() < deadline:
            n = driver.execute_script(
                "return document.querySelectorAll(\"[contenteditable='true'], textarea\").length;"
            )
            if n and n > 0:
                break
            time.sleep(2)
        time.sleep(2)  # 描画の落ち着き待ち
        # 操作対象になりそうな要素を JS でまとめて outerHTML 収集（属性付きで素性が分かる）。
        script = r"""
        const sel = "input, textarea, [contenteditable], button, [role='textbox'], [data-name], [class*='Editor'], [class*='editor']";
        const seen = new Set();
        const out = [];
        document.querySelectorAll(sel).forEach(el => {
          let html = el.outerHTML;
          // 子要素が巨大な場合に備え、開始タグ＋短いテキストだけに切り詰める。
          const open = html.split('>')[0] + '>';
          const text = (el.textContent || '').trim().slice(0, 40);
          const key = open + '|' + text;
          if (seen.has(key)) return;
          seen.add(key);
          out.push(open + (text ? '  «' + text + '»' : ''));
        });
        return out.join('\n');
        """
        dom = driver.execute_script(script)
        with open(DOM_DUMP_HTML, "w", encoding="utf-8") as f:
            f.write(f"<!-- URL: {driver.current_url} / title: {driver.title} -->\n")
            f.write(dom or "(要素が取れませんでした)")
        driver.save_screenshot(DOM_DUMP_PNG)
        log(f"DOM ダンプ: {DOM_DUMP_HTML}")
        log(f"スクショ: {DOM_DUMP_PNG}")
        log(f"current_url: {driver.current_url}")
        return 0
    finally:
        driver.quit()



def _is_logged_in(driver):
    """ログイン済みかの簡易判定（ログインボタンが見えなければログイン済みとみなす）。"""
    driver.get(NOTE_TOP_URL)
    time.sleep(3)
    page = driver.page_source
    # ★要確認：'ログイン' リンクの有無で判定（UI 変更で要調整）。
    return ("ログイン" not in page) or ("ログアウト" in page) or ("creator" in page)


def cmd_probe_image(headless=False):
    """画像アップロードの仕組みを調べる診断モード（file input の出方を確認）。"""
    driver = setup_driver(headless=headless)
    try:
        driver.get(NOTE_NEW_URL)
        if not wait_for_editor(driver):
            log("エディタが出ませんでした")
            return 1
        from selenium.webdriver.common.keys import Keys

        body_el = driver.find_element(*NOTE_SELECTORS["body"])
        body_el.click()
        body_el.send_keys("段落テスト")
        body_el.send_keys(Keys.ENTER)  # 末尾に空段落を作る（+ハンドルが出る）
        time.sleep(1)

        def file_inputs():
            return driver.find_elements(By.CSS_SELECTOR, "input[type='file']")

        # 本文左の「+」(メニューを開く) を探して押す → 本文ブロック挿入メニュー
        plus = driver.find_elements(By.CSS_SELECTOR, "button[aria-label='メニューを開く']")
        log(f"「メニューを開く」(+) ボタン数: {len(plus)}")
        if plus:
            driver.execute_script("arguments[0].click();", plus[-1])
            time.sleep(2)
        # メニューの「画像」を押す
        imgopt = driver.find_elements(By.XPATH, "//button[normalize-space()='画像']")
        log(f"メニュー「画像」項目数: {len(imgopt)}")
        if imgopt:
            driver.execute_script("arguments[0].click();", imgopt[-1])
            time.sleep(2)
        log(f"画像選択後 file input 数: {len(file_inputs())}")
        ups = driver.find_elements(By.XPATH, "//*[normalize-space(text())='画像をアップロード']")
        log(f"「画像をアップロード」項目数: {len(ups)}")
        if ups:
            driver.execute_script("arguments[0].click();", ups[-1])
            time.sleep(2)
        after = file_inputs()
        log(f"最終 file input 数: {len(after)}")
        for i, el in enumerate(after):
            html = driver.execute_script("return arguments[0].outerHTML;", el)
            log(f"  input[{i}]: {html[:220]}")
        # クリックで現れた可能性のあるメニュー/パネルも記録
        script = r"""
        const out=[];
        document.querySelectorAll("input[type='file'], [role='menu'] *, [data-testid*='image'], [class*='image' i] input, [aria-label*='画像']").forEach(el=>{
          out.push((el.outerHTML.split('>')[0]+'>') + ' «'+(el.textContent||'').trim().slice(0,30)+'»');
        });
        return [...new Set(out)].join('\n');
        """
        dom = driver.execute_script(script)
        with open(DOM_DUMP_HTML, "w", encoding="utf-8") as f:
            f.write(f"<!-- probe image / URL: {driver.current_url} -->\n")
            f.write(dom or "(なし)")
        driver.save_screenshot(DOM_DUMP_PNG)
        log(f"ダンプ: {DOM_DUMP_HTML} / {DOM_DUMP_PNG}")
        return 0
    finally:
        driver.quit()


def _count_uploaded_images(driver):
    """本文(ProseMirror)内で、サーバ(st-note CDN)へアップ済みの <img> 枚数を数える。

    アップロード中は blob:/data: の仮 img になり、完了すると https の実URLに差し替わる。
    https の img だけ数えることで「実際に保存される枚数」を確認できる。
    """
    return driver.execute_script(
        "return [...document.querySelectorAll(\"div.ProseMirror[contenteditable='true'] img\")]"
        ".filter(im => /^https?:/i.test(im.getAttribute('src') || '')).length;"
    ) or 0


def insert_images_at_cursor(driver, paths):
    """カーソル位置（本文末尾）に画像を **1枚ずつ確実に** 挿入する（成功枚数を返す）。

    手順（実DOMで確認）：「+」(メニューを開く) → 「画像」 → 現れる file input
    (#note-editor-image-upload-input) に1パス送信、を画像ごとに繰り返す。
    note のアップローダは file input に複数パスを一括送信するとバッチ末尾の1枚を
    取りこぼすことがあるため、まとめ送りはしない。各挿入後、本文のアップ済み <img>
    枚数が1枚増える（=サーバ保存完了）まで待ってから次へ進む。これで取りこぼしと
    「保存が早すぎてアップロード未完で落ちる」レースの両方を防ぐ。
    """
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        return 0
    inserted = 0
    for p in paths:
        before = _count_uploaded_images(driver)
        try:
            plus = driver.find_elements(*NOTE_SELECTORS["block_plus"])
            if not plus:
                log("  「+」ブロックメニューが見つからず（以降の画像スキップ）")
                break
            driver.execute_script("arguments[0].click();", plus[-1])
            time.sleep(1.2)
            imgopt = driver.find_elements(*NOTE_SELECTORS["menu_image"])
            if not imgopt:
                log("  メニュー「画像」が見つからず（以降の画像スキップ）")
                break
            driver.execute_script("arguments[0].click();", imgopt[-1])
            time.sleep(1.2)
            inputs = driver.find_elements(*NOTE_SELECTORS["image_input"])
            if not inputs:
                log("  画像アップロード input が見つからず（以降の画像スキップ）")
                break
            inputs[-1].send_keys(p)  # 1枚だけ送る
            # アップ済み <img> が1枚増える（=サーバ保存完了）まで待つ。
            deadline = time.time() + 30
            uploaded_ok = False
            while time.time() < deadline:
                if _count_uploaded_images(driver) >= before + 1:
                    uploaded_ok = True
                    break
                time.sleep(1)
            # 次の挿入/本文入力に備え、カーソルを本文末尾へ戻す
            body = driver.find_element(*NOTE_SELECTORS["body"])
            body.click()
            body.send_keys(Keys.CONTROL, Keys.END)
            if uploaded_ok:
                inserted += 1
                log(f"  画像挿入: {os.path.basename(p)}（{inserted}/{len(paths)}）")
            else:
                log(f"  画像のアップロード確認がタイムアウト（スキップ）: {os.path.basename(p)}")
        except Exception as e:  # noqa: BLE001
            log(f"  画像挿入に失敗（スキップ）: {os.path.basename(p)}: {e}")
            continue
    return inserted


def cmd_post(headless=False):
    """新規エディタに流し込んで下書き保存する。公開ボタンは押さない。"""
    if not os.path.exists(REPORT_MD):
        log(f"レポートが見つかりません: {REPORT_MD}")
        return 0  # 何もすることがない＝正常終了（fail-open）
    if not os.path.isdir(PROFILE_DIR):
        log("ログイン用プロファイルがありません。先に `python post_to_note.py --login` を実行してください。")
        notify_line("⚠️ note 自動下書き: 未ログイン。post_to_note.py --login を実行してね")
        return 1

    title, blocks = parse_report(REPORT_MD)
    n_text = sum(1 for k, _ in blocks if k in ("h2", "li", "ol", "p"))
    n_img = sum(1 for k, _ in blocks if k == "image")
    log(f"タイトル: {title}")
    log(f"本文ブロック: 文 {n_text} / 画像 {n_img}")

    driver = setup_driver(headless=headless)
    wait = WebDriverWait(driver, WAIT)
    try:
        if not _is_logged_in(driver):
            log("ログインしていないようです。--login で再ログインしてください。")
            notify_line("⚠️ note 自動下書き: セッション切れ。post_to_note.py --login を実行してね")
            return 1

        # 新規投稿エディタを開く（note が自動で下書きを1本作り /notes/xxx/edit/ に遷移）
        driver.get(NOTE_NEW_URL)
        if not wait_for_editor(driver):
            raise RuntimeError("エディタ本文が現れませんでした（headless では描画されない点に注意）")
        log(f"エディタを開いた: {driver.current_url}")

        # タイトル
        title_el = wait.until(EC.presence_of_element_located(NOTE_SELECTORS["title"]))
        title_el.click()
        title_el.send_keys(title)
        log("タイトル入力 完了")

        # 本文をブロック順に入力する。markdown 入力ルール（"## "→見出し, "- "→箇条書き,
        # "1. "→番号付き, "**"→太字）はテキスト入力で効くので、各ブロックの markdown を
        # 残したまま送る。ブロック境界は Enter キーで割る：
        #   ・同種のリスト項目どうし（li→li / ol→ol）は Enter 1回（ProseMirror が次項目を
        #     自動生成し、番号も自動採番する。マーカー "- "/"1. " は先頭項目だけに付ける——
        #     2項目目以降に付けると既にリスト内なのでリテラルの "- "/"1." が残ってしまう）。
        #   ・見出しの後も Enter 1回（見出しを抜けて新しい段落になる）。
        #   ・それ以外（段落↔段落/見出し、リスト→見出し等）は Enter 2回でブロックを割る
        #     （リストは空項目での Enter でリストを抜ける）。
        # 項目「内」の改行（SOFT_BREAK）は Shift+Enter（ソフト改行）で送り、単独 Enter＝
        # 「新しい項目」と区別する。画像はその位置でインライン挿入する。
        def _enters(prev_kind, cur_kind):
            if prev_kind == "h2" or (prev_kind in ("li", "ol") and cur_kind == prev_kind):
                return 1
            return 2

        body_el = wait.until(EC.presence_of_element_located(NOTE_SELECTORS["body"]))
        body_el.click()
        uploaded = 0
        run = []  # 連続テキストブロックの (kind, val)
        img_run = []  # 連続する画像パス
        after_image = [False]  # 直前が画像挿入だったか（次のテキストの直前に改行を入れる）

        def flush_text():
            if not run:
                return
            # 画像（atomic block）の直後に区切りなしでテキストを打つと、直前の図が巻き込まれて
            # 保存時に消える。画像の後に始まるテキストは、まず空段落へ抜けてから打つ。
            if after_image[0]:
                body_el.send_keys(Keys.ENTER, Keys.ENTER)
                after_image[0] = False
            for i, (k, v) in enumerate(run):
                if i > 0:
                    for _ in range(_enters(run[i - 1][0], k)):
                        body_el.send_keys(Keys.ENTER)
                # リスト走（run）の先頭項目だけにマーカーを付ける（以降は自動継続・自動採番）。
                if k in ("li", "ol") and (i == 0 or run[i - 1][0] != k):
                    body_el.send_keys("- " if k == "li" else "1. ")
                    time.sleep(0.3)  # 入力ルールがリスト化するのを待ってから本文を打つ
                # 項目内のソフト改行は Shift+Enter（単独 Enter＝新項目と区別する）。
                for j, piece in enumerate(v.split(SOFT_BREAK)):
                    if j > 0:
                        body_el.send_keys(Keys.SHIFT, Keys.ENTER)
                    body_el.send_keys(piece)
            time.sleep(1.5)
            run.clear()

        for kind, val in blocks + [("flush", "")]:
            if kind in ("h2", "li", "ol", "p"):
                if img_run:  # 直前までの画像群を先に流す
                    uploaded += insert_images_at_cursor(driver, img_run)
                    img_run.clear()
                    after_image[0] = True
                run.append((kind, val))
            elif kind == "image":
                flush_text()  # 直前までのテキストを先に流す
                img_run.append(val)
            else:  # 末尾の flush 番兵
                flush_text()
                if img_run:
                    uploaded += insert_images_at_cursor(driver, img_run)
                    img_run.clear()
                    after_image[0] = True
        log(f"本文入力 完了（画像 {uploaded}/{n_img} 枚）")

        # 画像のサーバー側アップロード完了を待ってから保存（早すぎると画像が落ちる）。
        time.sleep(3 + 2 * uploaded)

        # 下書き保存（「公開に進む」は絶対に押さない）
        save_btn = wait.until(EC.element_to_be_clickable(NOTE_SELECTORS["save_draft"]))
        driver.execute_script("arguments[0].click();", save_btn)
        log("下書き保存を押下")
        time.sleep(6)  # 保存の確定待ち

        # マガジン指定は公開設定パネル内で UI が深い。下書きを作るところまでを確実にし、
        # マガジン指定は公開時（手動ゲート）にたる坊が行う運用にする。
        log(f"下書き保存 完了（公開はしていない）: {driver.current_url}")
        notify_line("📝 note に週次レポートの下書きを保存したよ。所感を確認して公開してね")
        return 0
    except Exception as e:  # noqa: BLE001
        try:
            driver.save_screenshot(ERROR_SHOT)
            log(f"失敗時スクショ: {ERROR_SHOT}")
        except Exception:  # noqa: BLE001
            pass
        log(f"note 下書き保存に失敗: {e}")
        notify_line("⚠️ note 自動下書きに失敗。今回は手でコピペして下書き保存してね")
        return 1
    finally:
        driver.quit()


def main():
    parser = argparse.ArgumentParser(description="週次レポートを note の下書きに保存する")
    parser.add_argument("--login", action="store_true", help="初回ログイン（表示ありブラウザ）")
    parser.add_argument("--dump", action="store_true", help="実DOMを書き出す（セレクタ特定用）")
    parser.add_argument("--probe-image", action="store_true", help="画像アップロードの仕組みを調べる")
    # note のエディタは headless だと描画されないため常に headful。--headless は実験用。
    parser.add_argument("--headless", action="store_true", help="（実験）headless で動かす")
    args = parser.parse_args()

    if args.login:
        cmd_login()
        return 0
    if args.dump:
        return cmd_dump(headless=args.headless)
    if args.probe_image:
        return cmd_probe_image(headless=args.headless)
    return cmd_post(headless=args.headless)


if __name__ == "__main__":
    sys.exit(main())
