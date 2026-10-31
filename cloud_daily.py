#!/usr/bin/env python3
"""
投信的猴子 — 雲端每日流程 v1.0
================================================================
雲端每次執行都是全新環境，所以歷史資料存在 Artifact 的 store/ 檔案裡：
  store/YYYY-MM.csv   每月一檔，所有股票的每日精簡資料（只保留最近 KEEP 個交易日）
  store/industry.csv  產業分類

流程（python3 cloud_daily.py run）：
  1. 在 fetch/ 用 tw_backfill.py 抓最近 10 天
  2. 合併進 store/，刪掉太舊的資料
  3. 展開成 work/data/，跑 monkey_report.py
  4. 把報表轉成 Artifact 頁面格式 → out/page.html
  5. 印出 RESULT 行：是否有新交易日、哪些 store 檔有變動
================================================================
"""
import csv
import glob
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "store")
FETCH = os.path.join(HERE, "fetch")
WORK = os.path.join(HERE, "work")
OUT = os.path.join(HERE, "out")
KEEP = 110                      # 保留最近幾個交易日（報表需要 60 日活躍＋60 日熱度圖）
FIELDS = ["date", "code", "name", "close", "change", "value", "trust_net",
          "foreign_net", "tact", "issued"]


def rcsv(p):
    with open(p, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def read_store():
    rows = {}
    for p in sorted(glob.glob(os.path.join(STORE, "????-??.csv"))):
        for r in rcsv(p):
            rows.setdefault(r["date"], []).append(r)
    return rows


def day_rows_from_backfill(data_dir, d):
    """把 tw_backfill 的 4 個檔合成一天的精簡資料"""
    out = {}
    for src in ("twse", "tpex"):
        qp = os.path.join(data_dir, f"{src}_quote", d + ".csv")
        if not os.path.exists(qp):
            return None
        for r in rcsv(qp):
            out[r["code"]] = {"date": d, "code": r["code"], "name": r["name"],
                              "close": r["close"], "change": r["change"],
                              "value": r["value_twd"], "trust_net": "0", "foreign_net": "0",
                              "tact": "0", "issued": r.get("issued_shares", "")}
        ip = os.path.join(data_dir, f"{src}_insti", d + ".csv")
        if not os.path.exists(ip):
            return None
        for r in rcsv(ip):
            if r["code"] in out:
                o = out[r["code"]]
                o["trust_net"] = r["trust_net"] or "0"
                o["foreign_net"] = r["foreign_net"] or "0"
                o["tact"] = "1" if fnum(r["trust_buy"]) + fnum(r["trust_sell"]) > 0 else "0"
    return list(out.values())


def write_store(rows_by_day):
    days = sorted(rows_by_day)[-KEEP:]
    months = {}
    for d in days:
        months.setdefault(f"{d[:4]}-{d[4:6]}", []).extend(rows_by_day[d])
    before = {os.path.basename(p): open(p, "rb").read()
              for p in glob.glob(os.path.join(STORE, "????-??.csv"))}
    for p in glob.glob(os.path.join(STORE, "????-??.csv")):
        os.remove(p)
    changed = []
    for m, rows in months.items():
        p = os.path.join(STORE, m + ".csv")
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(sorted(rows, key=lambda r: (r["date"], r["code"])))
        if before.get(m + ".csv") != open(p, "rb").read():
            changed.append(f"store/{m}.csv")
    removed = [f"store/{n}" for n in before if not os.path.exists(os.path.join(STORE, n))]
    return days, changed, removed


def unpack(days, rows_by_day):
    """展開成 monkey_report.py 讀得懂的 data/ 結構"""
    if os.path.exists(WORK):
        shutil.rmtree(WORK)
    q = os.path.join(WORK, "data", "twse_quote")
    i = os.path.join(WORK, "data", "twse_insti")
    os.makedirs(q)
    os.makedirs(i)
    for d in days:
        rows = rows_by_day[d]
        with open(os.path.join(q, d + ".csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["code", "name", "open", "high", "low", "close", "change",
                        "volume_shares", "value_twd", "issued_shares"])
            for r in rows:
                w.writerow([r["code"], r["name"], "", "", "", r["close"], r["change"],
                            "", r["value"], r["issued"]])
        with open(os.path.join(i, d + ".csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["code", "name", "foreign_net", "trust_buy", "trust_sell",
                        "trust_net", "dealer_net", "total_net"])
            for r in rows:
                w.writerow([r["code"], r["name"], r["foreign_net"],
                            "1" if r["tact"] == "1" else "0", "0", r["trust_net"], "", ""])
    shutil.copy(os.path.join(STORE, "industry.csv"), os.path.join(WORK, "data", "industry.csv"))
    if os.path.isdir(os.path.join(STORE, "etf")):         # 主動式 ETF 持股快照
        shutil.copytree(os.path.join(STORE, "etf"), os.path.join(WORK, "data", "etf"))
    for f in ("monkey_report.py", "concept_groups.csv"):
        shutil.copy(os.path.join(HERE, f), WORK)


def to_artifact_page(html):
    """去掉 doctype/html/head/body 外殼（Artifact 發布時會自動加上）"""
    html = re.sub(r"(?is)<!doctype html>\s*<html[^>]*>\s*<head>\s*", "", html)
    html = re.sub(r'(?i)<meta charset="utf-8">\s*', "", html)
    html = re.sub(r'(?i)<meta name="viewport"[^>]*>\s*', "", html)
    html = re.sub(r"(?i)</head>\s*<body>", "", html)
    html = re.sub(r"(?i)</body>\s*</html>\s*$", "", html)
    return html


def run():
    os.makedirs(STORE, exist_ok=True)
    old = read_store()
    old_last = max(old) if old else ""

    # 1. 抓最近 10 天
    os.makedirs(os.path.join(FETCH, "data"), exist_ok=True)
    shutil.copy(os.path.join(HERE, "tw_backfill.py"), FETCH)
    ind = os.path.join(STORE, "industry.csv")
    if os.path.exists(ind):      # 已有產業分類就不重抓（雲端連櫃買產業頁不穩）
        shutil.copy(ind, os.path.join(FETCH, "data", "industry.csv"))
    days_arg = sys.argv[2] if len(sys.argv) > 2 else "10"
    r = subprocess.run([sys.executable, "tw_backfill.py", "--days", days_arg], cwd=FETCH)
    warn = ""
    if r.returncode != 0:      # 部分日期被擋：已抓到的照用，缺的下次自動補
        warn = "部分日期下載失敗，下次執行會自動補抓"
        print("⚠ " + warn)

    # 2. 合併
    fdata = os.path.join(FETCH, "data")
    for p in glob.glob(os.path.join(fdata, "twse_quote", "*.csv")):
        d = os.path.basename(p)[:-4]
        rows = day_rows_from_backfill(fdata, d)
        if rows:
            old[d] = rows
    if os.path.exists(os.path.join(fdata, "industry.csv")):
        new_ind = rcsv(os.path.join(fdata, "industry.csv"))
        if sum(1 for x in new_ind if x["market"] == "上櫃") > 100:   # 上櫃有抓到才更新
            shutil.copy(os.path.join(fdata, "industry.csv"), os.path.join(STORE, "industry.csv"))
    if not old:
        print("RESULT " + json.dumps({"ok": False, "error": "沒有任何資料"}, ensure_ascii=False))
        sys.exit(1)
    days, changed, removed = write_store(old)
    new_last = days[-1]

    # 3. 報表
    unpack(days, old)
    r = subprocess.run([sys.executable, "monkey_report.py"], cwd=WORK,
                       capture_output=True, text=True)
    print(r.stdout[-3000:])
    if r.returncode != 0:
        print(r.stderr[-2000:])
        print("RESULT " + json.dumps({"ok": False, "error": "報表失敗"}, ensure_ascii=False))
        sys.exit(1)
    rep = sorted(glob.glob(os.path.join(WORK, "report", "*.html")))[-1]
    os.makedirs(OUT, exist_ok=True)
    with open(rep, encoding="utf-8") as f:
        page = to_artifact_page(f.read())
    with open(os.path.join(OUT, "page.html"), "w", encoding="utf-8") as f:
        f.write(page)
    shutil.copy(rep, os.path.join(OUT, "index.html"))          # 完整網頁（GitHub Pages 用）

    # 摘要（給排程任務的通知用）
    summ = re.findall(r"【(.+?)】(\d+) 個：(.*)", r.stdout)
    print("RESULT " + json.dumps({
        "ok": True, "warn": warn, "new_day": new_last != old_last, "last_day": new_last,
        "days_kept": len(days), "changed": changed, "removed": removed,
        "picks": [{"title": t, "n": int(n), "groups": g} for t, n, g in summ],
    }, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        run()
    else:
        print(__doc__)
