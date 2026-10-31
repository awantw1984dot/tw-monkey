#!/usr/bin/env python3
"""
投信的猴子 — 主動式 ETF 每日持股 v1.0
  1. 從證交所／櫃買行情找出所有主動式 ETF（代號 00xxxA）
  2. 逐檔抓 MoneyDJ「全部持股」頁，只保留台股（4 位數代號）
  3. 存成 store/etf/YYYYMMDD.csv（以持股資料日期命名），保留最近 KEEP 份
失敗不影響主流程：抓不到就印出原因並正常結束。
"""
import csv
import glob
import os
import re
import sys
import time
from datetime import datetime, timedelta

import requests

H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "store", "etf")
KEEP = 130
ROW = re.compile(r">([^<>]{1,30}?)\((\d{4,6}[A-Z]?)\.(TW|TWO)\)<.*?>\s*([\d.,]+)\s*<.*?>\s*([\d,]+)\s*<", re.S)


def active_etfs():
    d = datetime.now()
    for back in range(8):
        t = d - timedelta(days=back)
        if t.weekday() >= 5:
            continue
        codes = {}
        try:
            j = requests.get("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
                             f"?date={t:%Y%m%d}&type=ALLBUT0999&response=json", headers=H, timeout=30).json()
            for tb in j.get("tables", []):
                if isinstance(tb, dict) and "證券代號" in (tb.get("fields") or []):
                    for r in tb["data"]:
                        if re.match(r"^00\d{3,4}A$", r[0].strip()):
                            codes[r[0].strip()] = r[1].strip()
            time.sleep(2)
            j = requests.get("https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
                             f"?date={t:%Y/%m/%d}&type=EW&response=json", headers=H, timeout=30).json()
            for r in (j.get("tables") or [{}])[0].get("data", []):
                if re.match(r"^00\d{3,4}A$", r[0].strip()):
                    codes[r[0].strip()] = r[1].strip()
        except Exception as e:
            print(f"  行情 {t:%Y%m%d} 失敗：{e}")
        if codes:
            return codes
    return {}


def fetch(code):
    for sfx in (".TW", ".TWO"):
        r = requests.get(f"https://www.moneydj.com/ETF/X/Basic/Basic0007B.xdjhtm?etfid={code}{sfx}",
                         headers=H, timeout=30)
        r.encoding = r.apparent_encoding or "utf-8"
        t = r.text
        dm = re.search(r"資料日期[：:]\s*(\d{4})/(\d{2})/(\d{2})", t)
        if dm:
            rows = [(nm.strip(), c, w, s) for nm, c, _, w, s in ROW.findall(t) if re.match(r"^[1-9]\d{3}$", c)]
            return "".join(dm.groups()), rows
        time.sleep(1.5)
    return None, []


def main():
    print("▸ 主動式 ETF 持股")
    etfs = active_etfs()
    if not etfs:
        print("  找不到主動式 ETF 清單，略過")
        return
    print(f"  {len(etfs)} 檔主動式 ETF")
    out, dates, fail = [], {}, []
    for code, name in sorted(etfs.items()):
        time.sleep(2.5)
        try:
            d, rows = fetch(code)
        except Exception as e:
            fail.append(code)
            print(f"  ✗ {code} {e}")
            continue
        if not d:
            fail.append(code)
            continue
        if not rows:
            continue                    # 海外基金，沒有台股
        dates[d] = dates.get(d, 0) + 1
        for nm, c, w, s in rows:
            out.append([d, code, name, c, nm, w.replace(",", ""), s.replace(",", "")])
    if not out:
        print("  沒有抓到任何持股，略過")
        return
    day = max(dates, key=lambda k: (dates[k], k))       # 多數 ETF 的資料日期
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, day + ".csv")
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["data_date", "etf", "etf_name", "code", "name", "weight", "shares"])
        w.writerows(out)
    for old in sorted(glob.glob(os.path.join(OUT, "*.csv")))[:-KEEP]:
        os.remove(old)
    n_etf = len({r[1] for r in out})
    print(f"  ✓ {day}：{n_etf} 檔 ETF、{len(out)} 筆台股持股" + (f"｜失敗 {','.join(fail)}" if fail else ""))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"  ✗ 主動式 ETF 持股失敗：{e}")
    sys.exit(0)
