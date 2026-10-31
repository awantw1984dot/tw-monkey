#!/usr/bin/env python3
"""
投信的猴子 — 資料回補腳本 v1.0
================================================================
抓「全市場」每日資料，存成精簡 CSV，可中斷、可續傳。

  上市法人  證交所 T86        → data/twse_insti/YYYYMMDD.csv
  上市行情  證交所 MI_INDEX   → data/twse_quote/YYYYMMDD.csv
  上櫃法人  櫃買 dailyTrade   → data/tpex_insti/YYYYMMDD.csv
  上櫃行情  櫃買 dailyQuotes  → data/tpex_quote/YYYYMMDD.csv
  產業分類  一次性            → data/industry.csv
  每日狀態                    → data/status.csv

只保留 4 位數代號的普通股（排除 ETF、權證、TDR）。
股數單位一律為「股」，金額單位為「元」。

用法：
  python3 tw_backfill.py --test        # 先試抓最近 3 個交易日（約 1 分鐘）
  python3 tw_backfill.py               # 回補一年（約 1 小時，可隨時 Ctrl+C）
  python3 tw_backfill.py --days 30     # 只補最近 30 天
  python3 tw_backfill.py --retry       # 重抓之前失敗的日期

建議用 caffeinate 防止 Mac 睡眠：
  caffeinate -i python3 tw_backfill.py

需要：pip3 install requests
================================================================
"""
import argparse
import csv
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta

import requests

# ------------------------------------------------------------
# 設定
# ------------------------------------------------------------
DATA = "data"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
DELAY = (3.0, 5.0)          # 每個請求之間隨機等待秒數
RETRY_WAIT = [30, 90]       # 失敗後重試等待秒數
MAX_CONSEC_FAIL = 3         # 連續幾個請求失敗就判定被封鎖、停止
STOCK_RE = re.compile(r"^[1-9]\d{3}$")   # 4 位數普通股，排除 0050 等 ETF

SOURCES = ["twse_quote", "twse_insti", "tpex_insti", "tpex_quote", "twse_sbl", "tpex_sbl"]

# 證交所產業別代碼 → 名稱（上市、上櫃共用）
INDUSTRY_NAMES = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙工業",
    "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業", "14": "建材營造",
    "15": "航運業", "16": "觀光餐旅", "17": "金融保險", "18": "貿易百貨",
    "19": "綜合", "20": "其他", "21": "化學工業", "22": "生技醫療",
    "23": "油電燃氣", "24": "半導體業", "25": "電腦及週邊設備業",
    "26": "光電業", "27": "通信網路業", "28": "電子零組件業",
    "29": "電子通路業", "30": "資訊服務業", "31": "其他電子業",
    "32": "文化創意業", "33": "農業科技業", "34": "電子商務",
    "35": "綠能環保", "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
    "80": "管理股票", "91": "存託憑證",
}


class Blocked(Exception):
    """疑似被網站封鎖或網站異常，需停止"""


# ------------------------------------------------------------
# 小工具
# ------------------------------------------------------------
def num(s):
    """'1,234' / '-0.31 ' / '--' / '' → float 或 None"""
    if s is None:
        return None
    s = re.sub(r"<[^>]+>", "", str(s)).replace(",", "").strip()
    if s in ("", "--", "---", "-", "X", "除權息", "除權", "除息"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def write_csv(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    os.replace(tmp, path)          # 寫完才改名，中斷不會留下半個檔


_last_req = [0.0]


def get_json(url):
    """帶節流與重試的 GET，回傳 JSON；網站異常時丟 Blocked"""
    for attempt in range(len(RETRY_WAIT) + 1):
        wait = random.uniform(*DELAY) - (time.time() - _last_req[0])
        if wait > 0:
            time.sleep(wait)
        _last_req[0] = time.time()
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.json()
            err = f"HTTP {r.status_code}"
        except ValueError:
            err = "回應不是 JSON（可能被擋）"
        except requests.RequestException as e:
            err = f"{type(e).__name__}"
        if attempt < len(RETRY_WAIT):
            print(f"    ⚠ {err}，{RETRY_WAIT[attempt]} 秒後重試…", flush=True)
            time.sleep(RETRY_WAIT[attempt])
    raise Blocked(err)


# ------------------------------------------------------------
# 各來源：抓取 + 解析
# 回傳 ("ok", header, rows) 或 ("nodata", None, None)
# ------------------------------------------------------------
def twse_quote(d):
    j = get_json("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
                 f"?date={d:%Y%m%d}&type=ALLBUT0999&response=json")
    if j.get("stat") != "OK":
        return "nodata", None, None
    tbl = next((t for t in j.get("tables", [])
                if isinstance(t, dict) and "證券代號" in (t.get("fields") or [])), None)
    if not tbl or not tbl.get("data"):
        return "nodata", None, None
    f = tbl["fields"]
    ix = {k: f.index(k) for k in ("證券代號", "證券名稱", "成交股數", "成交金額",
                                  "開盤價", "最高價", "最低價", "收盤價",
                                  "漲跌(+/-)", "漲跌價差")}
    rows = []
    for r in tbl["data"]:
        code = r[ix["證券代號"]].strip()
        if not STOCK_RE.match(code):
            continue
        chg = num(r[ix["漲跌價差"]])
        if chg is not None and "-" in re.sub(r"<[^>]+>", "", r[ix["漲跌(+/-)"]]):
            chg = -chg
        rows.append([code, r[ix["證券名稱"]].strip(),
                     num(r[ix["開盤價"]]), num(r[ix["最高價"]]), num(r[ix["最低價"]]),
                     num(r[ix["收盤價"]]), chg,
                     num(r[ix["成交股數"]]), num(r[ix["成交金額"]]), None])
    return "ok", QUOTE_HEADER, rows


def twse_insti(d):
    j = get_json("https://www.twse.com.tw/rwd/zh/fund/T86"
                 f"?date={d:%Y%m%d}&selectType=ALL&response=json")
    if j.get("stat") != "OK" or not j.get("data"):
        return "nodata", None, None
    f = [x.strip() for x in j["fields"]]

    def col(*keys):
        return next(i for i, x in enumerate(f) if all(k in x for k in keys))
    i_code = col("證券代號")
    i_name = col("證券名稱")
    i_for = col("外陸資買賣超股數")        # 不含外資自營商
    i_fdl = col("外資自營商買賣超股數")
    i_tb, i_ts, i_tn = col("投信買進股數"), col("投信賣出股數"), col("投信買賣超股數")
    i_dl = f.index("自營商買賣超股數")
    i_all = col("三大法人買賣超股數")
    rows = []
    for r in j["data"]:
        code = r[i_code].strip()
        if not STOCK_RE.match(code):
            continue
        foreign = (num(r[i_for]) or 0) + (num(r[i_fdl]) or 0)
        rows.append([code, r[i_name].strip(), foreign,
                     num(r[i_tb]), num(r[i_ts]), num(r[i_tn]),
                     num(r[i_dl]), num(r[i_all])])
    return "ok", INSTI_HEADER, rows


def tpex_insti(d):
    j = get_json("https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"
                 f"?type=Daily&sect=EW&date={d:%Y/%m/%d}&response=json")
    tbl = (j.get("tables") or [{}])[0]
    if not tbl.get("data"):
        return "nodata", None, None
    if j.get("date") and j["date"] != f"{d:%Y%m%d}":
        return "nodata", None, None      # 網站回傳了別天的資料
    if len(tbl["fields"]) < 24:
        raise Blocked(f"櫃買法人欄位數變成 {len(tbl['fields'])}，網站可能改版")
    # 欄位組：外資(不含自營)2-4｜外資自營5-7｜外資合計8-10｜投信11-13
    #        自營(自行)14-16｜自營(避險)17-19｜自營合計20-22｜三大法人23
    rows = []
    for r in tbl["data"]:
        code = r[0].strip()
        if not STOCK_RE.match(code):
            continue
        rows.append([code, r[1].strip(), num(r[10]),
                     num(r[11]), num(r[12]), num(r[13]),
                     num(r[22]), num(r[23])])
    return "ok", INSTI_HEADER, rows


def tpex_quote(d):
    j = get_json("https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
                 f"?date={d:%Y/%m/%d}&type=EW&response=json")
    tbl = (j.get("tables") or [{}])[0]
    if not tbl.get("data"):
        return "nodata", None, None
    if j.get("date") and j["date"] != f"{d:%Y%m%d}":
        return "nodata", None, None
    f = [x.strip() for x in tbl["fields"]]
    ix = {k: f.index(k) for k in ("代號", "名稱", "收盤", "漲跌", "開盤", "最高",
                                  "最低", "成交股數", "成交金額(元)", "發行股數")}
    rows = []
    for r in tbl["data"]:
        code = r[ix["代號"]].strip()
        if not STOCK_RE.match(code):
            continue
        rows.append([code, r[ix["名稱"]].strip(),
                     num(r[ix["開盤"]]), num(r[ix["最高"]]), num(r[ix["最低"]]),
                     num(r[ix["收盤"]]), num(r[ix["漲跌"]]),
                     num(r[ix["成交股數"]]), num(r[ix["成交金額(元)"]]),
                     num(r[ix["發行股數"]])])
    return "ok", QUOTE_HEADER, rows


def _sbl_rows(tbl):
    """信用額度總量管制餘額表：第 8–13 欄是借券賣出（前日餘額、當日賣出、當日還券、調整、當日餘額）"""
    rows = []
    for r in tbl.get("data") or []:
        code = str(r[0]).strip()
        if not STOCK_RE.match(code) or len(r) < 13:
            continue
        rows.append([code, str(r[1]).strip(), num(r[8]), num(r[9]), num(r[10]), num(r[12])])
    return rows


def twse_sbl(d):
    j = get_json("https://www.twse.com.tw/rwd/zh/marginTrading/TWT93U"
                 f"?date={d:%Y%m%d}&response=json")
    if j.get("stat") != "OK":
        return "nodata", None, None
    tbl = next((t for t in (j.get("tables") or [j]) if isinstance(t, dict) and t.get("data")), None)
    if not tbl:
        return "nodata", None, None
    if len(tbl.get("fields") or []) < 13:
        raise Blocked("證交所借券欄位改版")
    rows = _sbl_rows(tbl)
    return ("ok", SBL_HEADER, rows) if rows else ("nodata", None, None)


def tpex_sbl(d):
    j = get_json("https://www.tpex.org.tw/www/zh-tw/margin/sbl"
                 f"?date={d:%Y/%m/%d}&response=json")
    tbl = (j.get("tables") or [{}])[0]
    if not tbl.get("data"):
        return "nodata", None, None
    if j.get("date") and j["date"] != f"{d:%Y%m%d}":
        return "nodata", None, None
    if len(tbl.get("fields") or []) < 13:
        raise Blocked("櫃買借券欄位改版")
    rows = _sbl_rows(tbl)
    return ("ok", SBL_HEADER, rows) if rows else ("nodata", None, None)


QUOTE_HEADER = ["code", "name", "open", "high", "low", "close", "change",
                "volume_shares", "value_twd", "issued_shares"]
INSTI_HEADER = ["code", "name", "foreign_net", "trust_buy", "trust_sell",
                "trust_net", "dealer_net", "total_net"]
SBL_HEADER = ["code", "name", "sbl_prev", "sbl_sell", "sbl_return", "sbl_bal"]
FETCHERS = {"twse_sbl": twse_sbl, "tpex_sbl": tpex_sbl,
            "twse_quote": twse_quote, "twse_insti": twse_insti,
            "tpex_insti": tpex_insti, "tpex_quote": tpex_quote}


# ------------------------------------------------------------
# 產業分類（一次性，失敗不影響回補）
# ------------------------------------------------------------
def fetch_industry():
    path = os.path.join(DATA, "industry.csv")
    rows, issued = {}, {}
    print("▸ 產業分類")
    # 上市：證交所 OpenAPI（探路已確認可用）
    try:
        for x in get_json("https://openapi.twse.com.tw/v1/opendata/t187ap03_L"):
            code = x.get("公司代號", "").strip()
            if STOCK_RE.match(code):
                ic = x.get("產業別", "").strip()
                rows[code] = ["上市", code, x.get("公司簡稱", "").strip(),
                              INDUSTRY_NAMES.get(ic, ic),
                              num(x.get("已發行普通股數或TDR原股發行股數"))]
        print(f"    上市 {len(rows)} 檔")
    except Exception as e:
        print(f"    ✗ 上市產業別失敗：{e}")
    # 上櫃：先試櫃買 OpenAPI，失敗再試 ISIN 公開頁
    n0 = len(rows)
    try:
        for x in get_json("https://www.tpex.org.tw/openapi/v1/mopsfe_t187ap03_O"):
            code = (x.get("SecuritiesCompanyCode") or x.get("公司代號") or "").strip()
            ic = (x.get("SecuritiesIndustryCode") or x.get("產業別") or "").strip()
            name = (x.get("CompanyAbbreviation") or x.get("公司簡稱") or "").strip()
            if STOCK_RE.match(code):
                rows[code] = ["上櫃", code, name, INDUSTRY_NAMES.get(ic, ic), None]
    except Exception as e:
        print(f"    上櫃 OpenAPI 失敗（{e}），改試 ISIN 頁…")
    if len(rows) == n0:
        try:
            time.sleep(random.uniform(*DELAY))
            r = requests.get("https://isin.twse.com.tw/isin/C_public.jsp?strMode=4",
                             headers=HEADERS, timeout=60)
            html = r.content.decode("cp950", errors="replace")
            for tr in re.findall(r"<tr>(.*?)</tr>", html, re.S):
                td = [re.sub(r"<[^>]+>", "", c).strip()
                      for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
                if len(td) >= 5 and td[3] == "上櫃":
                    parts = td[0].replace("　", " ").split(None, 1)
                    if len(parts) == 2 and STOCK_RE.match(parts[0]):
                        rows[parts[0]] = ["上櫃", parts[0], parts[1], td[4], None]
        except Exception as e:
            print(f"    ✗ ISIN 頁也失敗：{e}")
    print(f"    上櫃 {len(rows) - n0} 檔")
    if rows:
        write_csv(path, ["market", "code", "name", "industry", "issued_shares"],
                  sorted(rows.values(), key=lambda x: x[1]))
    return len(rows) - n0


# ------------------------------------------------------------
# 狀態檔（續傳用）
# ------------------------------------------------------------
STATUS = os.path.join(DATA, "status.csv")


def load_status():
    st = {}
    if os.path.exists(STATUS):
        with open(STATUS, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                st[(r["date"], r["source"])] = r["status"]
    return st


def save_status(date_s, source, status, nrows):
    new = not os.path.exists(STATUS)
    with open(STATUS, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "source", "status", "rows", "fetched_at"])
        w.writerow([date_s, source, status, nrows,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S")])


# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------
def candidate_dates(days):
    """由新到舊的平日；下午 5 點前不含今天"""
    d = datetime.now()
    if d.hour < 17:
        d -= timedelta(days=1)
    end = d - timedelta(days=days)
    out = []
    while d > end:
        if d.weekday() < 5:
            out.append(d.replace(hour=0, minute=0, second=0, microsecond=0))
        d -= timedelta(days=1)
    return out


def run(days, test=False, retry=False, only=None):
    global SOURCES
    if only:
        SOURCES = [x for x in SOURCES if x in only]
    os.makedirs(DATA, exist_ok=True)
    st = load_status()
    dates = candidate_dates(days)
    target_trading = 3 if test else None
    done_trading = 0
    t0 = time.time()

    print(f"範圍：{dates[-1]:%Y-%m-%d} ～ {dates[0]:%Y-%m-%d}，"
          f"共 {len(dates)} 個平日{'（測試模式：只抓 3 個交易日）' if test else ''}\n")

    if not os.path.exists(os.path.join(DATA, "industry.csv")):
        fetch_industry()
        print()

    consec_fail = 0
    for n, d in enumerate(dates, 1):
        ds = f"{d:%Y%m%d}"
        # 已知休市
        if st.get((ds, SOURCES[0])) == "nodata" and not retry:
            continue
        need = [s for s in SOURCES
                if not (st.get((ds, s)) in ("ok", "nodata")
                        and (st.get((ds, s)) == "nodata"
                             or os.path.exists(os.path.join(DATA, s, ds + ".csv"))))]
        if not need:
            if st.get((ds, "twse_quote")) == "ok":
                done_trading += 1
                if target_trading and done_trading >= target_trading:
                    break
            continue

        line = f"[{n:>3}/{len(dates)}] {d:%Y-%m-%d} "
        print(line, end="", flush=True)
        for s in SOURCES:
            if s not in need:
                print(f"{s}:已有 ", end="", flush=True)
                continue
            try:
                status, header, rows = FETCHERS[s](d)
                consec_fail = 0
            except Blocked as e:
                consec_fail += 1
                save_status(ds, s, "error", 0)
                print(f"\n    ✗ {s} 失敗：{e}")
                if consec_fail >= MAX_CONSEC_FAIL:
                    print("\n連續失敗，疑似被網站暫時封鎖。"
                          "\n請過 30 分鐘後再執行同一個指令，會從中斷處繼續。")
                    summary()
                    sys.exit(1)
                continue
            except Exception as e:
                save_status(ds, s, "error", 0)
                print(f"\n    ✗ {s} 解析錯誤：{type(e).__name__}: {e}")
                continue
            if status == "ok":
                write_csv(os.path.join(DATA, s, ds + ".csv"), header, rows)
                save_status(ds, s, "ok", len(rows))
                print(f"{s}:{len(rows)} ", end="", flush=True)
            else:
                save_status(ds, s, "nodata", 0)
                print(f"{s}:無資料 ", end="", flush=True)
                if s == SOURCES[0]:       # 第一個來源沒資料 = 休市，其他來源跳過
                    print("（休市）", end="")
                    break
        print(flush=True)

        st = load_status()
        if st.get((ds, "twse_quote")) == "ok":
            done_trading += 1
            if target_trading and done_trading >= target_trading:
                break
        if n % 20 == 0:
            el = time.time() - t0
            print(f"    … 已花 {el/60:.0f} 分鐘", flush=True)

    summary()


def summary():
    st = load_status()
    trading = sorted({d for (d, s), v in st.items() if s == SOURCES[0] and v == "ok"})
    print("\n" + "=" * 60)
    print("回補摘要")
    print("=" * 60)
    if not trading:
        print("  尚無資料")
        return
    print(f"  交易日：{len(trading)} 天（{trading[0]} ～ {trading[-1]}）")
    for s in SOURCES:
        ok = sum(1 for d in trading if st.get((d, s)) == "ok")
        miss = [d for d in trading if st.get((d, s)) != "ok"]
        flag = "✓" if not miss else "⚠"
        print(f"  {flag} {s:<11} {ok}/{len(trading)}"
              + (f"  缺：{', '.join(miss[:8])}{' …' if len(miss) > 8 else ''}" if miss else ""))
    ind = os.path.join(DATA, "industry.csv")
    if os.path.exists(ind):
        with open(ind, encoding="utf-8-sig") as f:
            r = list(csv.DictReader(f))
        print(f"  產業分類：上市 {sum(x['market']=='上市' for x in r)}、"
              f"上櫃 {sum(x['market']=='上櫃' for x in r)} 檔")
    else:
        print("  ⚠ 產業分類尚未取得")
    errs = sum(1 for v in st.values() if v == "error")
    if errs:
        print(f"\n  有失敗紀錄，可執行：python3 tw_backfill.py --retry")


def sanity_check():
    """測試模式結束後：抽查概念族群的投信買賣超，確認欄位對得上"""
    groups = {}
    if os.path.exists("concept_groups.csv"):
        with open("concept_groups.csv", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                groups.setdefault(r["group"], []).append(r["code"])
    latest = sorted(os.listdir(os.path.join(DATA, "twse_insti")))[-1].replace(".csv", "")
    flows = {}
    for s in ("twse_insti", "tpex_insti"):
        p = os.path.join(DATA, s, latest + ".csv")
        if os.path.exists(p):
            with open(p, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    flows[r["code"]] = r
    print(f"\n抽查 {latest} 概念族群法人買賣超（張）")
    for g, codes in groups.items():
        print(f"  【{g}】")
        for c in codes:
            r = flows.get(c)
            if not r:
                print(f"    {c} 查無資料")
                continue
            def z(k):
                v = r.get(k)
                return f"{float(v)/1000:>8,.0f}" if v not in (None, "") else "       -"
            print(f"    {c} {r['name'][:6]:<6} 外資{z('foreign_net')}  "
                  f"投信{z('trust_net')}  自營{z('dealer_net')}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=366, help="回補天數（日曆天）")
    ap.add_argument("--test", action="store_true", help="只抓最近 3 個交易日")
    ap.add_argument("--retry", action="store_true", help="重抓失敗日期")
    ap.add_argument("--only", help="只抓指定來源，逗號分隔，例如 twse_sbl,tpex_sbl")
    a = ap.parse_args()
    try:
        run(a.days, test=a.test, retry=a.retry, only=a.only.split(",") if a.only else None)
        if a.test:
            sanity_check()
            print("\n測試完成。請把整段輸出貼給 Claude 確認，沒問題再跑完整回補。")
    except KeyboardInterrupt:
        print("\n\n已中斷。下次執行同一個指令會從中斷處繼續。")
        summary()
