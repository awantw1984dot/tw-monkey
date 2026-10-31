"""主動式 ETF 持股來源探路：從證交所／櫃買行情找出所有主動式 ETF，再抓 MoneyDJ 持股頁"""
import json, os, re, time, html
from datetime import datetime, timedelta
import requests
H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
os.makedirs("probe/out", exist_ok=True)
log = []
def L(*a):
    s = " ".join(str(x) for x in a); print(s); log.append(s)

d = datetime.now()
codes = {}
for back in range(7):
    t = d - timedelta(days=back)
    if t.weekday() >= 5: continue
    try:
        j = requests.get(f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={t:%Y%m%d}&type=ALLBUT0999&response=json", headers=H, timeout=30).json()
        for tb in j.get("tables", []):
            if "證券代號" in (tb.get("fields") or []):
                for r in tb["data"]:
                    if re.match(r"^00\d{3,4}A$", r[0].strip()): codes[r[0].strip()] = (r[1].strip(), "上市")
        j = requests.get(f"https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?date={t:%Y/%m/%d}&type=EW&response=json", headers=H, timeout=30).json()
        for r in (j.get("tables") or [{}])[0].get("data", []):
            if re.match(r"^00\d{3,4}A$", r[0].strip()): codes[r[0].strip()] = (r[1].strip(), "上櫃")
        if codes:
            L("行情日", f"{t:%Y%m%d}"); break
    except Exception as e:
        L("行情失敗", t, e)
L("主動式ETF", len(codes), json.dumps(codes, ensure_ascii=False))

results = {}
for i, (c, (nm, mk)) in enumerate(sorted(codes.items())):
    time.sleep(2.5)
    sfx = ".TW" if mk == "上市" else ".TWO"
    url = f"https://www.moneydj.com/ETF/X/Basic/Basic0007B.xdjhtm?etfid={c}{sfx}"
    try:
        r = requests.get(url, headers=H, timeout=30)
        r.encoding = r.apparent_encoding or "utf-8"
        t = r.text
        if i < 3:
            open(f"probe/out/{c}.html", "w", encoding="utf-8").write(t)
        date = re.search(r"資料日期[：:]\s*([\d/]+)", t)
        rows = re.findall(r">([^<>]{1,30}?)\((\d{4,6}[A-Z]?)\.(TW|TWO)\)<.*?>\s*([\d.,]+)\s*<.*?>\s*([\d,]+)\s*<", t, re.S)
        results[c] = {"name": nm, "status": r.status_code, "bytes": len(t), "date": date.group(1) if date else None,
                      "n_tw_rows": len(rows), "sample": rows[:3]}
        L(c, nm, r.status_code, len(t), results[c]["date"], len(rows), rows[:2])
    except Exception as e:
        results[c] = {"name": nm, "error": str(e)}; L(c, "ERR", e)
json.dump({"codes": codes, "results": results}, open("probe/out/result.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
open("probe/out/log.txt", "w", encoding="utf-8").write("\n".join(log))
