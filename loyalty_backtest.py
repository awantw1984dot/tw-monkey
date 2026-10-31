#!/usr/bin/env python3
"""
投信不離不棄 — 回測 v1.0
讀 tw_backfill.py 格式的 data/，以事件為單位回測：
  事件 ＝ 個股從近 60 日高點回檔首次跌破門檻的那一天（同一個高點只算一次）
  上漲段投信買超 A ＝ 高點前 20 個交易日（含高點）投信淨買超金額
  高點後投信買賣 B ＝ 高點隔天到事件日的投信淨買賣金額
  留存率 ＝ (A + B) / A；只看 A ≥ 1 億的事件
  分類：不離不棄（留存率 ≥ 100% 且高點後買超天數 ≥ 50%）
        按兵不動（留存率 ≥ 90%，但買超天數 < 50%）
        撤退（留存率 < 70%）｜其他
  績效：事件日之後 10／20／40 個交易日，相對全市場（市值加權）的超額報酬
用法：python3 loyalty_backtest.py [data 資料夾]
"""
import csv
import os
import sys
from collections import defaultdict

DATA = sys.argv[1] if len(sys.argv) > 1 else "data"
LOOK, PRE, MIN_A = 60, 20, 1e8
THRESHOLDS = [0.15, 0.20, 0.25, 0.30]
HORIZONS = [10, 20, 40]


def F(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def rcsv(p):
    with open(p, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load():
    days = sorted(f[:-4] for f in os.listdir(os.path.join(DATA, "twse_quote")) if f.endswith(".csv"))
    issued = {r["code"]: F(r["issued_shares"]) for r in rcsv(os.path.join(DATA, "industry.csv"))}
    names = {r["code"]: r["name"] for r in rcsv(os.path.join(DATA, "industry.csv"))}
    px, tr, fo = defaultdict(dict), defaultdict(dict), defaultdict(dict)
    mret = {}
    for d in days:
        num = den = 0.0
        for m in ("twse", "tpex"):
            qp = os.path.join(DATA, f"{m}_quote", d + ".csv")
            if os.path.exists(qp):
                for r in rcsv(qp):
                    c, close, chg = r["code"], F(r["close"]), F(r["change"])
                    if close <= 0:
                        continue
                    px[c][d] = close
                    iss = issued.get(c) or F(r.get("issued_shares"))
                    if iss and not issued.get(c):
                        issued[c] = iss
                    prev = close - chg
                    if iss and prev > 0:
                        num += prev * iss * (chg / prev)
                        den += prev * iss
            ip = os.path.join(DATA, f"{m}_insti", d + ".csv")
            if os.path.exists(ip):
                for r in rcsv(ip):
                    tr[r["code"]][d] = F(r["trust_net"])
                    fo[r["code"]][d] = F(r["foreign_net"])
        mret[d] = num / den if den else 0.0
    return days, px, tr, fo, mret, names


def main():
    days, px, tr, fo, mret, names = load()
    di = {d: i for i, d in enumerate(days)}
    mlvl, v = {}, 1.0
    for d in days:
        v *= 1 + mret[d]
        mlvl[d] = v
    print(f"資料：{days[0]}～{days[-1]}，{len(days)} 個交易日，{len(px)} 檔")

    events = {t: [] for t in THRESHOLDS}
    for c, p in px.items():
        for thr in THRESHOLDS:
            used_peaks = set()
            for i in range(LOOK, len(days)):
                d = days[i]
                if d not in p:
                    continue
                win = [x for x in days[i - LOOK:i + 1] if x in p]
                pk = max(win, key=lambda x: p[x])
                if pk == d or pk in used_peaks:
                    continue
                dd = p[d] / p[pk] - 1
                if dd > -thr:
                    continue
                used_peaks.add(pk)
                ip = di[pk]
                pre = [x for x in days[max(0, ip - PRE + 1):ip + 1] if x in p]
                post = [x for x in days[ip + 1:i + 1] if x in p]
                A = sum(tr[c].get(x, 0) * p[x] for x in pre)
                if A < MIN_A:
                    continue
                B = sum(tr[c].get(x, 0) * p[x] for x in post)
                FB = sum(fo[c].get(x, 0) * p[x] for x in post)
                buyd = sum(1 for x in post if tr[c].get(x, 0) > 0) / max(len(post), 1)
                keep = (A + B) / A
                if keep >= 1 and buyd >= .5:
                    cat = "不離不棄"
                elif keep >= .9:
                    cat = "按兵不動"
                elif keep < .7:
                    cat = "撤退"
                else:
                    cat = "其他"
                fwd = {}
                for h in HORIZONS:
                    if i + h < len(days) and days[i + h] in p:
                        e = days[i + h]
                        fwd[h] = (p[e] / p[d] - 1) - (mlvl[e] / mlvl[d] - 1)
                events[thr].append({"code": c, "name": names.get(c, ""), "date": d, "peak": pk,
                                    "dd": dd, "A": A, "B": B, "FB": FB, "keep": keep,
                                    "buyd": buyd, "cat": cat, "fwd": fwd})

    out = []
    for thr in THRESHOLDS:
        ev = events[thr]
        print(f"\n===== 回檔門檻 {thr*100:.0f}%｜事件 {len(ev)} 筆 =====")
        print(f"{'分類':<8}" + "".join(f"{'%d日超額' % h:>12}{'勝率':>6}{'樣本':>6}" for h in HORIZONS))
        for cat in ("不離不棄", "按兵不動", "其他", "撤退", "全部"):
            sel = ev if cat == "全部" else [e for e in ev if e["cat"] == cat]
            line = f"{cat:<8}"
            for h in HORIZONS:
                xs = [e["fwd"][h] for e in sel if h in e["fwd"]]
                if xs:
                    line += f"{sum(xs)/len(xs)*100:>+11.1f}%{sum(x > 0 for x in xs)/len(xs)*100:>5.0f}%{len(xs):>6}"
                else:
                    line += f"{'—':>12}{'':>6}{0:>6}"
            print(line)
            out += [{"thr": thr, **{k: e[k] for k in ("code", "name", "date", "peak", "cat")},
                     "dd": round(e["dd"], 4), "A_億": round(e["A"] / 1e8, 2), "B_億": round(e["B"] / 1e8, 2),
                     "外資B_億": round(e["FB"] / 1e8, 2), "keep": round(e["keep"], 3),
                     "buyd": round(e["buyd"], 3),
                     **{f"fwd{h}": round(e["fwd"][h], 4) if h in e["fwd"] else "" for h in HORIZONS}}
                    for e in sel] if cat != "全部" else []
        # 半年穩定性
        mid = days[len(days) // 2]
        for half, cond in (("前半", lambda e: e["date"] < mid), ("後半", lambda e: e["date"] >= mid)):
            s = [e for e in ev if cond(e) and 20 in e["fwd"]]
            a = [e["fwd"][20] for e in s if e["cat"] == "不離不棄"]
            r = [e["fwd"][20] for e in s if e["cat"] == "撤退"]
            fa = f"{sum(a)/len(a)*100:+.1f}%（{len(a)}）" if a else "—"
            fr = f"{sum(r)/len(r)*100:+.1f}%（{len(r)}）" if r else "—"
            print(f"  {half}年 20日：不離不棄 {fa}｜撤退 {fr}")

    p = os.path.join(DATA, "loyalty_events.csv")
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)
    print(f"\n事件明細：{p}")


if __name__ == "__main__":
    main()
