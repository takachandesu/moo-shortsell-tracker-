#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日本株 空売り比率 データ取得スクリプト
  - JPX「空売り集計（日次）」の -m.pdf から 空売り比率(あり/なし/合計) を抽出
  - Stooq ^nkx から 日経225 終値・前日比 を取得
  - 直近90営業日を data/shortsell-data.json にローリング蓄積し、public/ へコピー

設計方針（既存メモ踏襲）:
  - 出力JSONはStooqの自前データとJPX一次ソースのみ。ウェブ検索やニュース値は使わない。
  - cronはJST明示で夕方起動（JPXは営業日夕方に公表）。
  - public/ 配下に自テーマのファイルだけ置く（FTP衝突対策メモ準拠）。
"""

import io
import json
import re
import sys
import time
import urllib.parse
import datetime as dt
from pathlib import Path

import requests
import pdfplumber

# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────
JST = dt.timezone(dt.timedelta(hours=9))           # 時刻ズレ対策メモの教訓: JSTを明示
JPX_INDEX = "https://www.jpx.co.jp/markets/statistics-equities/short-selling/index.html"
JPX_HOST  = "https://www.jpx.co.jp"
YAHOO_NKX = "https://query1.finance.yahoo.com/v8/finance/chart/%5EN225"  # 日経225 (^N225)
STOOQ_NKX = "https://stooq.com/q/d/l/"             # 予備: ?s=^nkx&i=d で日経225日足CSV
KEEP_ALL  = True                                    # True=全履歴を蓄積（90日制限なし）／False=直近WINDOW日だけ表示
WINDOW    = 90                                       # KEEP_ALL=False のときの表示日数

ROOT = Path(__file__).resolve().parent.parent       # リポジトリルート想定
DATA_FILE   = ROOT / "data"   / "shortsell-data.json"
PUBLIC_FILE = ROOT / "public" / "shortsell-data.json"

HEADERS = {"User-Agent": "moo-shortsell-tracker/1.0 (+https://moo-stock-blog.com)"}


# ─────────────────────────────────────────────
# JPX: index.html をパースして 日付→-m.pdf URL を得る
#   URLのフォルダ部は日付ごとに変わる不規則トークンなので、必ずHTMLから拾う
# ─────────────────────────────────────────────
def list_jpx_pdfs():
    html = requests.get(JPX_INDEX, headers=HEADERS, timeout=30).text
    pairs = {}
    # 例: .../short-selling/t13vrt000001emfs-att/260529-m.pdf
    for m in re.finditer(r'href="(/markets/statistics-equities/short-selling/[^"]+/(\d{6})-m\.pdf)"', html):
        path, ymd = m.group(1), m.group(2)
        d = dt.date(2000 + int(ymd[0:2]), int(ymd[2:4]), int(ymd[4:6]))
        pairs[d.isoformat()] = JPX_HOST + path
    return pairs  # {"2026-05-29": "https://.../260529-m.pdf", ...}


def parse_shortsell_pdf(url):
    """-m.pdf から (あり%, なし%, 合計%) を計算して返す。代金から算出して丸め誤差を回避。"""
    raw = requests.get(url, headers=HEADERS, timeout=30).content
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    text = re.sub(r"\s+", " ", text)
    # 実注文(a) a% 規制あり(b) b% 規制なし(c) c% 合計(d)
    m = re.search(
        r"(\d{4})年(\d{1,2})月(\d{1,2})日\s+"
        r"([\d,]+)\s+[\d.]+%\s+"      # a 実注文
        r"([\d,]+)\s+[\d.]+%\s+"      # b 価格規制あり
        r"([\d,]+)\s+[\d.]+%\s+"      # c 価格規制なし
        r"([\d,]+)",                  # d 合計
        text,
    )
    if not m:
        raise ValueError(f"PDF解析失敗（書式変更の可能性）: {url}")
    to_i = lambda s: int(s.replace(",", ""))
    b, c, d = to_i(m.group(5)), to_i(m.group(6)), to_i(m.group(7))
    restricted   = round(b / d * 100, 1)            # 価格規制あり
    unrestricted = round(c / d * 100, 1)            # 価格規制なし
    total        = round((b + c) / d * 100, 1)      # 合計
    return {"sr_total": total, "sr_restricted": restricted, "sr_unrestricted": unrestricted}


# ─────────────────────────────────────────────
# 日経225 日足の取得: Yahoo Finance(^N225)を主、Stooqを予備
#   時刻ズレ対策メモの教訓: Yahooのtimestampはエポック秒 → JSTに変換して日付化する
# ─────────────────────────────────────────────
def _parse_yahoo_chart(payload):
    """Yahoo chart JSONを {date:{nk_close,nk_change}} に。形式が想定外なら空dictを返す（落とさない）。"""
    try:
        res = payload["chart"]["result"][0]
        ts     = res["timestamp"]
        closes = res["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        return {}
    out, prev = {}, None
    for t, c in zip(ts, closes):
        if c is None:
            continue                      # 休場・欠損バーはスキップ
        date = dt.datetime.fromtimestamp(t, JST).date().isoformat()  # JSTで日付化（UTC扱いだと日付がズレる）
        close = round(float(c), 2)
        out[date] = {"nk_close": close,
                     "nk_change": None if prev is None else round(close - prev, 2)}
        prev = close
    return out


def _parse_stooq_csv(text):
    """予備: StooqのCSVテキストを同じ形式に。CSVでなければ空dictを返す（落とさない）。"""
    lines = text.strip().splitlines()
    if not lines or not lines[0].lower().startswith("date"):
        return {}                       # HTML/エラーページ等。CSVではない
    out, prev = {}, None
    for ln in lines[1:]:
        row = ln.split(",")
        if len(row) < 5 or row[4] in ("", "N/D"):
            continue
        try:
            close = float(row[4])       # 異常な行（JS片など）はスキップ
        except ValueError:
            continue
        date = row[0]
        out[date] = {"nk_close": round(close, 2),
                     "nk_change": None if prev is None else round(close - prev, 2)}
        prev = close
    return out


def fetch_nikkei():
    """日経225日足を取得。主=Yahoo Finance ^N225（直近1年の日足）、予備=Stooq。
       いずれも失敗しても例外を投げず空dictを返す（その日のnk_closeは後日backfillされる）。"""
    # 1) Yahoo Finance 直叩き（最大3回リトライ）
    for i in range(3):
        try:
            r = requests.get(YAHOO_NKX, params={"range": "1y", "interval": "1d"},
                             headers=HEADERS, timeout=30)
            data = _parse_yahoo_chart(r.json())
            if data:
                return data
            print(f"  nikkei: Yahoo応答が想定外(試行{i+1}) HTTP{r.status_code}", file=sys.stderr)
        except Exception as e:
            print(f"  nikkei: Yahoo {i+1}回目失敗 {e}", file=sys.stderr)
        time.sleep(3 * (i + 1))
    # 2) Yahoo をPHPプロキシ経由で（Lolipop側IPから。許可ドメイン登録済み）
    try:
        inner = YAHOO_NKX + "?range=1y&interval=1d"
        proxy = "https://moo-stock-blog.com/stock-proxy.php?url=" + urllib.parse.quote(inner, safe="")
        h = dict(HEADERS, Referer="https://moo-stock-blog.com/")
        data = _parse_yahoo_chart(requests.get(proxy, headers=h, timeout=30).json())
        if data:
            return data
    except Exception as e:
        print(f"  nikkei: Yahooプロキシ失敗 {e}", file=sys.stderr)
    # 3) 最終予備: Stooq直叩き
    try:
        data = _parse_stooq_csv(requests.get(STOOQ_NKX, params={"s": "^nkx", "i": "d"},
                                             headers=HEADERS, timeout=30).text)
        if data:
            return data
    except Exception as e:
        print(f"  nikkei: Stooq予備も失敗 {e}", file=sys.stderr)
    print("  nikkei: 今回は取得できず（nk_closeは後日backfill）", file=sys.stderr)
    return {}


# ─────────────────────────────────────────────
# メイン
# ─────────────────────────────────────────────
def load_existing():
    if DATA_FILE.exists():
        return {rec["date"]: rec for rec in json.loads(DATA_FILE.read_text("utf-8"))}
    return {}


def main():
    records = load_existing()
    jpx   = list_jpx_pdfs()
    nikkei = fetch_nikkei()

    # 既存に無い日付だけ取りに行く（毎日1〜数行ずつ追記）
    new_dates = sorted(set(jpx) - set(records))
    if not new_dates:
        print("新規データなし。")
    for date in new_dates:
        try:
            ss = parse_shortsell_pdf(jpx[date])
        except Exception as e:
            print(f"  skip {date}: {e}", file=sys.stderr)
            continue
        nk = nikkei.get(date, {"nk_close": None, "nk_change": None})
        records[date] = {
            "date": date,
            "nk_close": nk["nk_close"],
            "nk_change": nk["nk_change"],
            "prime_volume": None,          # プライム出来高は v2 で東証日報から追加予定
            **ss,
        }
        print(f"  + {date}  合計{ss['sr_total']}%  日経{nk['nk_close']}")

    # backfill: 過去にStooq失敗でnk_closeが欠けている日を、今回取得できた値で埋め直す
    filled = 0
    for date, rec in records.items():
        if rec.get("nk_close") is None and date in nikkei:
            rec["nk_close"]  = nikkei[date]["nk_close"]
            rec["nk_change"] = nikkei[date]["nk_change"]
            filled += 1
    if filled:
        print(f"  backfill: nk_close を {filled}日分 補完")

    # マスタ(data/)は常に全履歴を保存。public/ は表示用（既定は全履歴、KEEP_ALL=Falseで直近WINDOW日）
    all_sorted = [records[d] for d in sorted(records)]
    master_payload = json.dumps(all_sorted, ensure_ascii=False, indent=1)
    public_rows = all_sorted if KEEP_ALL else all_sorted[-WINDOW:]
    public_payload = json.dumps(public_rows, ensure_ascii=False, indent=1)

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    PUBLIC_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(master_payload, "utf-8")     # 全履歴（gitに蓄積、消えない）
    PUBLIC_FILE.write_text(public_payload, "utf-8")    # FTPアップロード対象（表示用）
    print(f"蓄積: 全{len(all_sorted)}営業日 / 表示{len(public_rows)}営業日 / 最新 {all_sorted[-1]['date'] if all_sorted else '—'}")


if __name__ == "__main__":
    main()
