#!/usr/bin/env python3
"""
投信的猴子 — 族群報表 v1.1（擴散度規則依回測校正）
================================================================
讀 tw_backfill.py 抓好的 data/，輸出一份手機可看的 HTML：
  1. 族群排行表（概念族群＋官方產業）
  2. 族群 × 日期 投信熱度圖（近 60 個交易日）
  3. 各族群成分股明細
另存 data/group_daily.csv（每族群每日指標，之後回測用）

用法（和 data 資料夾放在同一處）：
  python3 monkey_report.py               # 用最新一天
  python3 monkey_report.py --date 20260331   # 看過去某一天的樣子

只用 Python 內建模組，不需要安裝套件。
================================================================
"""
import argparse
import csv
import glob
import html
import json
import os
from collections import defaultdict
from datetime import datetime

DATA = "data"
OUT_DIR = "report"
HEAT_DAYS = 60
MIN_GROUP = 5                     # 官方產業少於幾檔不列入
SKIP_INDUSTRY = {"存託憑證", "管理股票", "管理股", ""}

# 擴散度規則（2026/10/31 以一年資料回測校正）
ACTIVE_WINDOW = 60                # 投信活躍股：過去幾個交易日內投信有買賣過
MIN_BUY_VALUE = 5_000_000         # 有效買超：投信 5 日淨買超 ≥ 500 萬元
MIN_BUYERS = 3                    # 至少幾檔有效買超
DIFF_LOW, DIFF_HIGH = 0.20, 0.50  # 主動佈局區間；≥ 上限標「過熱」
MIN_GROUP_VALUE = 5e8             # 族群投信 5 日淨買超 ≥ 5 億元才貼買方標籤

# 投信不離不棄（2026/10/31 以一年資料回測，門檻 20%）
LOY_LOOK, LOY_PRE = 60, 20        # 高點回看天數、上漲段天數
LOY_DD = 0.20                     # 從高點回檔至少 20%
LOY_MIN_A = 3e8                   # 上漲段投信買超至少 3 億元


# ------------------------------------------------------------
# 讀資料
# ------------------------------------------------------------
def rcsv(path):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load(end_date=None):
    days = sorted(f[:-4] for f in os.listdir(os.path.join(DATA, "twse_quote"))
                  if f.endswith(".csv"))
    if end_date:
        days = [d for d in days if d <= end_date]
    info = {}                                     # code -> dict
    for r in rcsv(os.path.join(DATA, "industry.csv")):
        ind = r["industry"].strip()
        if ind.endswith("業") and not ind.endswith("工業"):   # 上市「金融保險」vs 上櫃「金融保險業」
            ind = ind[:-1]
        info[r["code"]] = {"market": r["market"], "name": r["name"],
                           "industry": ind,
                           "issued": fnum(r["issued_shares"])}
    q = {}                                        # (day, code) -> dict
    for d in days:
        for src in ("twse", "tpex"):
            qp = os.path.join(DATA, f"{src}_quote", d + ".csv")
            ip = os.path.join(DATA, f"{src}_insti", d + ".csv")
            if not os.path.exists(qp):
                continue
            for r in rcsv(qp):
                c = r["code"]
                close, chg = fnum(r["close"]), fnum(r["change"])
                q[(d, c)] = {"close": close, "chg": chg,
                             "value": fnum(r["value_twd"]) or 0.0,
                             "trust": 0.0, "foreign": 0.0, "tact": False}
                iss = fnum(r.get("issued_shares"))
                if iss and c in info and not info[c]["issued"]:
                    info[c]["issued"] = iss
            if os.path.exists(ip):
                for r in rcsv(ip):
                    k = (d, r["code"])
                    if k in q:
                        q[k]["trust"] = fnum(r["trust_net"]) or 0.0
                        q[k]["foreign"] = fnum(r["foreign_net"]) or 0.0
                        q[k]["tact"] = ((fnum(r.get("trust_buy")) or 0)
                                        + (fnum(r.get("trust_sell")) or 0)) > 0
    # 換算金額、報酬
    for v in q.values():
        px = v["close"] or 0.0
        v["trust_v"] = v["trust"] * px
        v["foreign_v"] = v["foreign"] * px
        prev = (v["close"] - v["chg"]) if (v["close"] and v["chg"] is not None) else None
        v["ret"] = (v["chg"] / prev) if prev else None
    return days, info, q


def build_groups(info):
    groups = {}                                   # name -> {"kind","codes"}
    if os.path.exists("concept_groups.csv"):
        for r in rcsv("concept_groups.csv"):
            groups.setdefault(r["group"], {"kind": "概念", "codes": []})["codes"].append(r["code"])
    ind = defaultdict(list)
    for c, v in info.items():
        if v["industry"] not in SKIP_INDUSTRY:
            ind[v["industry"]].append(c)
    for k, codes in ind.items():
        if len(codes) >= MIN_GROUP and k not in groups:
            groups[k] = {"kind": "產業", "codes": sorted(codes)}
    return groups


# ------------------------------------------------------------
# 計算
# ------------------------------------------------------------
def market_ret(days, q, info):
    """全市場市值加權報酬（近似大盤）"""
    out = {}
    for d in days:
        num = den = 0.0
        for c, v in info.items():
            x = q.get((d, c))
            if x and x["ret"] is not None and x["close"] and v["issued"]:
                cap = (x["close"] - x["chg"]) * v["issued"]
                num += cap * x["ret"]
                den += cap
        out[d] = num / den if den else 0.0
    return out


def is_quarter_window(d):
    dt = datetime.strptime(d, "%Y%m%d")
    return dt.month in (3, 6, 9, 12) and dt.day >= 20


def compute(days, info, q, groups):
    mret = market_ret(days, q, info)
    daily = {}                                    # (group, day) -> metrics
    for g, gv in groups.items():
        codes = gv["codes"]
        for d in days:
            rows = [(c, q[(d, c)]) for c in codes if (d, c) in q]
            if not rows:
                continue
            rets = [x["ret"] for _, x in rows if x["ret"] is not None]
            daily[(g, d)] = {
                "trust_v": sum(x["trust_v"] for _, x in rows),
                "foreign_v": sum(x["foreign_v"] for _, x in rows),
                "turnover": sum(x["value"] for _, x in rows),
                "n": len(rows),
                "n_buy": sum(1 for _, x in rows if x["trust"] > 0),
                "n_sell": sum(1 for _, x in rows if x["trust"] < 0),
                "ret": sum(rets) / len(rets) if rets else 0.0,
                "mret": mret[d],
            }

    last = days[-1]
    d5, d20 = days[-5:], days[-20:]
    summary = []
    for g, gv in groups.items():
        if (g, last) not in daily:
            continue
        codes = gv["codes"]
        S = lambda key, ds: sum(daily[(g, d)][key] for d in ds if (g, d) in daily)
        t1, t5, t20 = daily[(g, last)]["trust_v"], S("trust_v", d5), S("trust_v", d20)
        f1, f5 = daily[(g, last)]["foreign_v"], S("foreign_v", d5)
        turn5 = S("turnover", d5)
        # 連買/連賣天數
        streak = 0
        for d in reversed(days):
            tv = daily.get((g, d), {}).get("trust_v", 0)
            if streak >= 0 and tv > 0:
                streak += 1
            elif streak <= 0 and tv < 0:
                streak -= 1
            else:
                break
        # 投信活躍股（過去 60 日投信有買賣過）
        d60 = days[-ACTIVE_WINDOW:]
        active = {c for c in codes if any(q.get((d, c), {}).get("tact") for d in d60)}
        # 個股 5 日累計
        members = []
        for c in codes:
            tv5 = sum(q[(d, c)]["trust_v"] for d in d5 if (d, c) in q)
            tsh5 = sum(q[(d, c)]["trust"] for d in d5 if (d, c) in q)
            fsh5 = sum(q[(d, c)]["foreign"] for d in d5 if (d, c) in q)
            x = q.get((last, c))
            if not x:
                continue
            r5 = 1.0
            for d in d5:
                y = q.get((d, c))
                if y and y["ret"] is not None:
                    r5 *= 1 + y["ret"]
            cap = (x["close"] or 0) * (info.get(c, {}).get("issued") or 0)
            members.append({"code": c, "name": info.get(c, {}).get("name", ""),
                            "t1": x["trust"] / 1000, "t5": tsh5 / 1000,
                            "f5": fsh5 / 1000, "tv5": tv5, "r5": r5 - 1,
                            "cap": cap, "active": c in active,
                            "buyer": c in active and tv5 >= MIN_BUY_VALUE})
        n_mem = len(members)
        n_active = len(active)
        n_buyers = sum(1 for m in members if m["buyer"])
        diff5 = n_buyers / n_active if n_active else 0
        # 前兩大市值股佔正向買超比重
        pos = sum(m["tv5"] for m in members if m["tv5"] > 0)
        big2 = sorted(members, key=lambda m: -m["cap"])[:2]
        big2_share = sum(max(m["tv5"], 0) for m in big2) / pos if pos else 0
        top1_share = max((m["tv5"] for m in members), default=0) / pos if pos else 0
        for m in big2:
            m["big"] = True
        # 相對報酬
        def cum(ds, key):
            v = 1.0
            for d in ds:
                if (g, d) in daily:
                    v *= 1 + daily[(g, d)][key]
            return v - 1
        rel5 = cum(d5, "ret") - cum(d5, "mret")
        rel20 = cum(d20, "ret") - cum(d20, "mret")

        tags = []
        big_enough = t5 >= MIN_GROUP_VALUE          # 投信 5 日淨買超達門檻才判斷
        if big_enough:
            if n_buyers >= MIN_BUYERS and DIFF_LOW <= diff5 < DIFF_HIGH:
                tags.append(("主動佈局", "good"))
            elif n_buyers >= MIN_BUYERS and diff5 >= DIFF_HIGH:
                tags.append(("過熱", "warn"))
            else:
                tags.append(("買超集中", "warn"))
            if is_quarter_window(last) and top1_share >= 0.6:
                tags.append(("季底窗口", "warn"))
            if f5 < 0:
                tags.append(("投信買外資賣", "info"))
        if streak <= -3:
            tags.append((f"連賣{-streak}天", "bad"))

        summary.append({
            "group": g, "kind": gv["kind"], "n": n_mem,
            "t1": t1 / 1e8, "t5": t5 / 1e8, "t20": t20 / 1e8,
            "f1": f1 / 1e8, "f5": f5 / 1e8,
            "intensity": (t5 / turn5 * 100) if turn5 else 0,
            "streak": streak, "diff5": diff5,
            "n_active": n_active, "n_buyers": n_buyers,
            "diff1": daily[(g, last)]["n_buy"] / daily[(g, last)]["n"],
            "rel5": rel5 * 100, "rel20": rel20 * 100,
            "big2": big2_share, "tags": tags,
            "members": sorted(members, key=lambda m: -m["tv5"]),
        })
    return daily, summary


# ------------------------------------------------------------
# 輸出
# ------------------------------------------------------------
def write_group_daily(daily, groups):
    path = os.path.join(DATA, "group_daily.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["date", "group", "kind", "n", "trust_value", "foreign_value",
                    "turnover", "n_trust_buy", "n_trust_sell", "ret_ew", "ret_market"])
        for (g, d), m in sorted(daily.items(), key=lambda x: (x[0][1], x[0][0])):
            w.writerow([d, g, groups[g]["kind"], m["n"], round(m["trust_v"]),
                        round(m["foreign_v"]), round(m["turnover"]), m["n_buy"],
                        m["n_sell"], round(m["ret"], 5), round(m["mret"], 5)])
    return path


def esc(s):
    return html.escape(str(s))


def fmt(x, d=1, sign=True):
    s = f"{x:+,.{d}f}" if sign else f"{x:,.{d}f}"
    return s.replace("+0.0", "0.0").replace("-0.0", "0.0") if abs(x) < 0.05 and d == 1 else s


def cls(x):
    return "up" if x > 0 else ("dn" if x < 0 else "")


def loyalty(days, info, q, groups):
    """投信不離不棄：從近 60 日高點回檔 ≥ 20% 的股票，看投信是撤退還是留下"""
    concept = {}
    for g, gv in groups.items():
        if gv["kind"] == "概念":
            for c in gv["codes"]:
                concept[c] = g
    if len(days) < LOY_LOOK + 1:
        return []
    last = days[-1]
    win = days[-LOY_LOOK - 1:]
    out = []
    for c in info:
        x = q.get((last, c))
        if not x or not x["close"]:
            continue
        ds = [d for d in win if (d, c) in q and q[(d, c)]["close"]]
        pk = max(ds, key=lambda d: q[(d, c)]["close"])
        if pk == last:
            continue
        dd = x["close"] / q[(pk, c)]["close"] - 1
        if dd > -LOY_DD:
            continue
        ip = days.index(pk)
        pre = [d for d in days[max(0, ip - LOY_PRE + 1):ip + 1] if (d, c) in q]
        post = [d for d in days[ip + 1:] if (d, c) in q]
        A = sum(q[(d, c)]["trust_v"] for d in pre)
        if A < LOY_MIN_A:
            continue
        B = sum(q[(d, c)]["trust_v"] for d in post)
        FB = sum(q[(d, c)]["foreign_v"] for d in post)
        buyd = sum(1 for d in post if q[(d, c)]["trust"] > 0) / max(len(post), 1)
        keep = (A + B) / A
        if keep >= 1 and buyd >= .5:
            cat = "不離不棄"
        elif keep >= .9:
            cat = "按兵不動"
        elif keep < .7:
            cat = "撤退"
        else:
            cat = "其他"
        out.append({"code": c, "name": info[c]["name"], "industry": info[c]["industry"],
                    "concept": concept.get(c, ""), "peak": pk, "dd": dd, "A": A / 1e8,
                    "B": B / 1e8, "FB": FB / 1e8, "keep": keep, "buyd": buyd,
                    "ndays": len(post), "cat": cat})
    return sorted(out, key=lambda r: -r["A"])


def load_etf():
    """主動式 ETF 持股快照：{日期: {股票代號: [(ETF代號, ETF名稱, 張數, 權重)]}}"""
    snaps = {}
    for p in sorted(glob.glob(os.path.join(DATA, "etf", "*.csv"))):
        d = os.path.basename(p)[:-4]
        m = defaultdict(list)
        for r in rcsv(p):
            m[r["code"]].append((r["etf"], r["etf_name"], fnum(r["shares"]) / 1000, fnum(r["weight"])))
        snaps[d] = m
    return snaps


def attach_etf(loy, snaps):
    if not snaps:
        return
    dates = sorted(snaps)
    last = dates[-1]
    for r in loy:
        now = snaps[last].get(r["code"], [])
        r["etf_now"] = sorted(now, key=lambda x: -x[2])
        r["etf_n"] = len(now)
        r["etf_sh"] = sum(x[2] for x in now)
        base = [d for d in dates if d <= r["peak"]]
        if base:
            then = snaps[base[-1]].get(r["code"], [])
            r["etf_dn"] = len(now) - len(then)
            r["etf_dsh"] = r["etf_sh"] - sum(x[2] for x in then)
            prev = {x[0]: x[2] for x in then}
            r["etf_chg"] = {x[0]: x[2] - prev.get(x[0], 0) for x in now}
            nowk = {x[0] for x in now}
            r["etf_out"] = [(x[0], x[1], x[2]) for x in then if x[0] not in nowk]
        else:
            r["etf_since"] = dates[0]


def render(days, daily, summary, groups, loy=None):
    last = days[-1]
    ld = f"{last[:4]}/{last[4:6]}/{last[6:]}"
    concept = [s for s in summary if s["kind"] == "概念"]
    industry = sorted([s for s in summary if s["kind"] == "產業"], key=lambda s: -s["t5"])
    ordered = concept + industry

    # ---------- 排行表 ----------
    def row(s):
        tags = "".join(f'<span class="tag {k}">{esc(t)}</span>' for t, k in s["tags"])
        st = s["streak"]
        st_txt = f"連買{st}" if st > 0 else (f"連賣{-st}" if st < 0 else "—")
        return (f'<tr data-k="{esc(s["kind"])}">'
                f'<td class="g"><a href="#m-{esc(s["group"])}">{esc(s["group"])}</a>'
                f'<small>{s["n"]}檔</small></td>'
                f'<td class="num {cls(s["t1"])}" data-v="{s["t1"]:.4f}">{fmt(s["t1"], 2)}</td>'
                f'<td class="num {cls(s["t5"])}" data-v="{s["t5"]:.4f}">{fmt(s["t5"], 2)}</td>'
                f'<td class="num {cls(s["intensity"])}" data-v="{s["intensity"]:.4f}">{fmt(s["intensity"], 1)}%</td>'
                f'<td class="num {cls(st)}" data-v="{st}">{st_txt}</td>'
                f'<td class="num" data-v="{s["diff5"]:.4f}">{s["diff5"]*100:.0f}%'
                f'<br><small>{s["n_buyers"]}/{s["n_active"]}檔</small></td>'
                f'<td class="num {cls(s["f5"])}" data-v="{s["f5"]:.4f}">{fmt(s["f5"], 2)}</td>'
                f'<td class="num {cls(s["rel5"])}" data-v="{s["rel5"]:.4f}">{fmt(s["rel5"], 1)}%</td>'
                f'<td class="tags">{tags}</td></tr>')

    head = ('<tr><th>族群</th><th data-s>投信今日<br><small>億</small></th>'
            '<th data-s>投信5日<br><small>億</small></th><th data-s>5日強度<br><small>佔成交</small></th>'
            '<th data-s>連續</th><th data-s>擴散度<br><small>買超/活躍</small></th>'
            '<th data-s>外資5日<br><small>億</small></th><th data-s>5日相對<br><small>大盤</small></th>'
            '<th>標籤</th></tr>')
    table = (f'<table class="rank" id="rank"><thead>{head}</thead><tbody>'
             + "".join(row(s) for s in ordered) + '</tbody></table>')

    # ---------- 熱度圖 ----------
    hd = days[-HEAT_DAYS:]
    cells, vals = [], []
    for s in ordered:
        g = s["group"]
        avg_turn = sum(daily[(g, d)]["turnover"] for d in hd if (g, d) in daily) / max(len(hd), 1)
        rowv = []
        for d in hd:
            m = daily.get((g, d))
            v = (m["trust_v"] / avg_turn * 100) if (m and avg_turn) else 0
            rowv.append((d, v, (m["trust_v"] / 1e8) if m else 0, m["n_buy"] if m else 0,
                         m["n"] if m else 0))
            vals.append(abs(v))
        cells.append((s, rowv))
    vals.sort()
    cap = vals[int(len(vals) * 0.95)] if vals else 1
    cap = cap or 1

    def color(v):
        t = max(-1, min(1, v / cap))
        a = abs(t) ** 0.7
        var = "--buy" if t > 0 else "--sell"
        pct = round(a * 100)
        return f"color-mix(in oklab, var({var}) {pct}%, var(--mid))"

    month_marks = []
    prev_m = None
    for i, d in enumerate(hd):
        if d[4:6] != prev_m:
            month_marks.append((i, f"{int(d[4:6])}月"))
            prev_m = d[4:6]
    mm = dict(month_marks)
    hm_head = "".join(f'<span class="mk">{esc(mm.get(i, ""))}</span>' for i in range(len(hd)))
    hm_rows = []
    for s, rowv in cells:
        sep = ' sep' if (industry and s is industry[0]) else ''
        hm_rows.append(f'<div class="hn{sep}">{esc(s["group"])}</div>')
        for d, v, tv, nb, n in rowv:
            tip = f'{s["group"]}｜{d[4:6]}/{d[6:]}｜投信 {tv:+.2f} 億｜買超 {nb}/{n} 檔｜強度 {v:+.1f}'
            hm_rows.append(f'<div class="c{sep}" style="background:{color(v)}" '
                           f'data-t="{esc(tip)}" title="{esc(tip)}"></div>')
    heat = (f'<div class="hwrap"><div class="heat" style="--cols:{len(hd)}">'
            f'<div class="hn hh"></div>{hm_head}' + "".join(hm_rows) + '</div></div>')

    # ---------- 成分股明細 ----------
    det = []
    for s in ordered:
        rows = "".join(
            f'<tr><td>{"● " if m["buyer"] else ("" if m["active"] else "<small>·</small> ")}'
            f'{esc(m["code"])} {esc(m["name"])}{" <small>權值</small>" if m.get("big") else ""}</td>'
            f'<td class="num {cls(m["t1"])}">{m["t1"]:+,.0f}</td>'
            f'<td class="num {cls(m["t5"])}">{m["t5"]:+,.0f}</td>'
            f'<td class="num {cls(m["tv5"])}">{m["tv5"]/1e8:+,.2f}</td>'
            f'<td class="num {cls(m["f5"])}">{m["f5"]:+,.0f}</td>'
            f'<td class="num {cls(m["r5"])}">{m["r5"]*100:+.1f}%</td></tr>'
            for m in s["members"])
        open_ = " open" if s["kind"] == "概念" else ""
        det.append(
            f'<details id="m-{esc(s["group"])}"{open_}><summary>{esc(s["group"])}'
            f'<span class="{cls(s["t5"])}">投信5日 {fmt(s["t5"], 2)} 億</span></summary>'
            f'<table class="mem"><thead><tr><th>個股</th><th>投信今日<br><small>張</small></th>'
            f'<th>投信5日<br><small>張</small></th><th>投信5日<br><small>億</small></th>'
            f'<th>外資5日<br><small>張</small></th>'
            f'<th>5日漲跌</th></tr></thead><tbody>{rows}</tbody></table></details>')

    # ---------- 重點族群：三類分開列 ----------
    def has(s, t):
        return any(x == t for x, _ in s["tags"])
    by_t5 = sorted(summary, key=lambda s: -s["t5"])
    both = [s for s in by_t5 if has(s, "主動佈局") and has(s, "投信買外資賣")]
    act_only = [s for s in by_t5 if has(s, "主動佈局") and not has(s, "投信買外資賣")]
    pa_only = [s for s in by_t5 if has(s, "投信買外資賣") and not has(s, "主動佈局")]

    def card(s):
        why = next((t for t, _ in s["tags"] if t in ("過熱", "買超集中")), "")
        return (f'<a class="card" href="#m-{esc(s["group"])}"><div class="ct">'
                f'<b>{esc(s["group"])}</b>{f'<span class="tag warn">{why}</span>' if why else ""}</div>'
                f'<div class="cr"><span>投信 <em class="{cls(s["t5"])}">{fmt(s["t5"], 1)}</em></span>'
                f'<span>外資 <em class="{cls(s["f5"])}">{fmt(s["f5"], 1)}</em></span>'
                f'<span>擴散 {s["diff5"]*100:.0f}%<small>（{s["n_buyers"]}/{s["n_active"]}）</small></span></div></a>')

    def block(title, desc, items, kind):
        body = "".join(card(s) for s in items) or '<div class="empty">今天沒有</div>'
        return (f'<section class="pick {kind}"><h3>{title}<span>{len(items)}</span></h3>'
                f'<p>{desc}</p><div class="cards">{body}</div></section>')

    picks = (block("主動佈局 ＋ 投信買外資賣", "外資在賣、投信廣泛承接；回測表現最好的組合", both, "p1")
             + block("只有主動佈局", "投信廣泛買超，但外資沒有在賣", act_only, "p2")
             + block("只有投信買外資賣", "投信有接，但買超集中、過熱或檔數不足", pa_only, "p3"))


    # ---------- 投信不離不棄 ----------
    loy = loy or []

    def lrow(r):
        tag = f'<small class="ct2">{esc(r["concept"])}</small>' if r["concept"] else f'<small>{esc(r["industry"])}</small>'
        return (f'<tr><td>{esc(r["code"])} {esc(r["name"])} {tag}</td>'
                f'<td class="num">{r["peak"][4:6]}/{r["peak"][6:]}</td>'
                f'<td class="num dn">{r["dd"]*100:.0f}%</td>'
                f'<td class="num">{r["A"]:,.1f}</td>'
                f'<td class="num {cls(r["B"])}">{r["B"]:+,.1f}</td>'
                f'<td class="num {cls(r["FB"])}">{r["FB"]:+,.1f}</td>'
                f'<td class="num">{r["keep"]*100:.0f}%</td>'
                f'<td class="num">{r["buyd"]*100:.0f}%</td>{etf_cell(r)}</tr>')

    def etf_cell(r):
        if "etf_n" not in r:
            return '<td class="num"><small>—</small></td>'
        if not r["etf_n"]:
            return '<td class="num"><small>0 檔</small></td>'
        if "etf_dn" in r:
            dn, dsh = r["etf_dn"], r["etf_dsh"]
            chg = (f'<span class="{cls(dn)}">{"↑" if dn > 0 else ("↓" if dn < 0 else "→")}{abs(dn)}</span> '
                   f'<span class="{cls(dsh)}">{dsh:+,.0f}張</span>')
        else:
            chg = f'<small>累積中（{r["etf_since"][4:6]}/{r["etf_since"][6:]}起）</small>'
        items = "".join(
            f'<li>{esc(e)} {esc(n.replace("主動", ""))}　{sh:,.0f}張　{w:.2f}%'
            + (f'　<span class="{cls(r["etf_chg"][e])}">{r["etf_chg"][e]:+,.0f}</span>' if "etf_chg" in r and r["etf_chg"].get(e) else "")
            + '</li>' for e, n, sh, w in r["etf_now"]) + "".join(
            f'<li>{esc(e)} {esc(n.replace("主動", ""))}　<span class="dn">已出清 −{sh:,.0f}張</span></li>'
            for e, n, sh in r.get("etf_out", []))
        return (f'<td class="etfc"><details><summary><b>{r["etf_n"]} 檔</b> {chg}</summary>'
                f'<ul>{items}</ul></details></td>')

    def ltable(cat, kind, title, desc, limit=None):
        rs = [r for r in loy if r["cat"] == cat]
        if limit:
            con = [r for r in rs if r["concept"]]
            rs = rs[:limit] + [r for r in con if r not in rs[:limit]]
        head = ('<tr><th>個股</th><th>高點</th><th>回檔</th><th>上漲段<br><small>投信</small></th>'
                '<th>高點後<br><small>投信</small></th><th>高點後<br><small>外資</small></th>'
                '<th>留存率</th><th>買超<br><small>天數</small></th><th>主動 ETF<br><small>自高點變化</small></th></tr>')
        body = "".join(lrow(r) for r in rs) or '<tr><td colspan="9" class="empty">今天沒有</td></tr>'
        n = sum(1 for r in loy if r["cat"] == cat)
        return (f'<section class="pick {kind}"><h3>{title}<span>{n}</span></h3><p>{desc}</p>'
                f'<div class="scroll"><table class="mem"><thead>{head}</thead><tbody>{body}</tbody></table></div></section>')

    loyal = (ltable("不離不棄", "p1", "不離不棄", "投信沒跑，下跌時還持續加碼（留存率 ≥ 100%、買超天數 ≥ 50%）")
             + ltable("按兵不動", "p3", "按兵不動（警訊）", "投信沒賣但也很少加碼，回測表現最差，可能是被套住")
             + ltable("撤退", "p4", "撤退", "投信在下跌中賣掉三成以上（只列上漲段買最多的 10 檔＋概念族群）", limit=10))

    return TEMPLATE.format(
        loyal=loyal, loy_dd=f"{LOY_DD*100:.0f}", loy_a=f"{LOY_MIN_A/1e8:.0f}",
        date=ld, ndays=len(days), first=f"{days[0][:4]}/{days[0][4:6]}/{days[0][6:]}",
        picks=picks, min_val=f"{MIN_GROUP_VALUE/1e8:.0f}",
        table=table, heat=heat, heat_days=len(hd), details="".join(det))


TEMPLATE = """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>投信的猴子</title>
<meta name="claude:viewport-fit-sides" content="cover">
<style>
:root{{--bg:#fcfcfb;--fg:#0b0b0b;--fg2:#52514e;--line:#e4e3df;--card:#fff;
--buy:#c62828;--sell:#1f5fb4;--mid:#f0efec;--good:#006300;--warn:#8a5a00;--bad:#b3261e;--info:#1f5fb4}}
@media (prefers-color-scheme:dark){{:root:not([data-theme=light]){{--bg:#1a1a19;--fg:#f0efec;--fg2:#c3c2b7;
--line:#383835;--card:#222220;--buy:#ef5350;--sell:#5b9be8;--mid:#383835;--good:#0ca30c;--warn:#e0a63a;--bad:#ef5350;--info:#5b9be8;color-scheme:dark}}}}
:root[data-theme=dark]{{--bg:#1a1a19;--fg:#f0efec;--fg2:#c3c2b7;
--line:#383835;--card:#222220;--buy:#ef5350;--sell:#5b9be8;--mid:#383835;--good:#0ca30c;--warn:#e0a63a;--bad:#ef5350;--info:#5b9be8;color-scheme:dark}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,"PingFang TC","Noto Sans TC",sans-serif}}
main{{max-width:1100px;margin:0 auto;padding-block:16px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}}
h1{{font-size:20px;margin:4px 0 2px}} h2{{font-size:16px;margin:28px 0 8px}}
.sub{{color:var(--fg2);font-size:12px}}
.kpi{{display:flex;gap:10px;margin:14px 0;flex-wrap:wrap}}
.kpi div{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 12px}}
.kpi b{{font-size:20px;display:block}} .kpi span{{font-size:12px;color:var(--fg2)}}
.scroll{{overflow-x:auto;-webkit-overflow-scrolling:touch;border:1px solid var(--line);border-radius:10px;background:var(--card)}}
table{{border-collapse:collapse;width:100%}}
th,td{{padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}}
th{{font-size:12px;color:var(--fg2);font-weight:600;text-align:right;position:sticky;top:0;background:var(--card)}}
th:first-child{{text-align:left}} th[data-s]{{cursor:pointer}} th small,td small{{color:var(--fg2);font-weight:400}}
td.g{{position:sticky;left:0;background:var(--card);font-weight:600}} td.g a{{color:var(--fg);text-decoration:none}}
td.g small{{margin-left:4px}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.up{{color:var(--buy)}} .dn{{color:var(--sell)}}
.tag{{display:inline-block;font-size:11px;padding:1px 6px;border-radius:9px;margin-right:3px;border:1px solid currentColor}}
.tag.good{{color:var(--good)}} .tag.warn{{color:var(--warn)}} .tag.bad{{color:var(--bad)}} .tag.info{{color:var(--info)}}
.filt{{display:flex;gap:6px;margin-bottom:8px}}
.filt button{{font:inherit;font-size:13px;padding:4px 12px;border-radius:16px;border:1px solid var(--line);background:var(--card);color:var(--fg)}}
.filt button.on{{background:var(--fg);color:var(--bg)}}
.hwrap{{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--card);padding:8px}}
.heat{{display:grid;grid-template-columns:96px repeat(var(--cols),minmax(9px,1fr));gap:2px;min-width:calc(96px + var(--cols)*11px)}}
.hn{{font-size:12px;position:sticky;left:0;background:var(--card);padding-right:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;line-height:16px}}
.c{{height:16px;border-radius:2px;cursor:pointer}}
.mk{{font-size:11px;color:var(--fg2);white-space:nowrap;position:relative;z-index:1;overflow:visible}}
.sep{{margin-top:8px}}
.legend{{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--fg2);margin-top:8px;flex-wrap:wrap}}
.bar{{width:140px;height:10px;border-radius:5px;background:linear-gradient(90deg,var(--sell),var(--mid),var(--buy))}}
#tip{{position:fixed;left:50%;bottom:calc(16px + env(safe-area-inset-bottom,0px));transform:translateX(-50%);background:var(--fg);color:var(--bg);
padding:8px 14px;border-radius:10px;font-size:13px;display:none;max-width:92vw;z-index:9}}
details{{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:8px 0;overflow-x:auto}}
summary{{padding:10px 12px;font-weight:600;cursor:pointer;display:flex;justify-content:space-between;gap:12px}}
summary span{{font-weight:400;font-size:13px}}
.mem td:first-child{{text-align:left}}
.note{{font-size:12px;color:var(--fg2);line-height:1.7}}
.pick{{border:1px solid var(--line);border-left-width:4px;border-radius:10px;background:var(--card);padding:10px 12px;margin:10px 0}}
.pick.p1{{border-left-color:var(--good)}} .pick.p2{{border-left-color:var(--info)}} .pick.p3{{border-left-color:var(--warn)}}
.pick h3{{font-size:15px;margin:0;display:flex;align-items:center;gap:8px}}
.pick h3 span{{font-size:12px;font-weight:600;background:var(--mid);border-radius:9px;padding:0 8px}}
.pick p{{margin:2px 0 8px;font-size:12px;color:var(--fg2)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:8px}}
.card{{display:block;text-decoration:none;color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:8px 10px}}
.ct{{display:flex;justify-content:space-between;align-items:center;gap:6px}}
.cr{{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:var(--fg2);margin-top:4px}}
.cr em{{font-style:normal;font-variant-numeric:tabular-nums;font-weight:600}}
.empty{{font-size:13px;color:var(--fg2)}}
.pick.p4{{border-left-color:var(--bad)}}
.pick .scroll{{border:0;background:transparent}}
.ct2{{color:var(--info)}}
.etfc{{min-width:150px}} .etfc summary{{cursor:pointer;white-space:nowrap}}
.etfc ul{{margin:6px 0 0;padding-left:16px;font-size:12px;color:var(--fg2);white-space:nowrap}}
</style></head><body><main>
<h1>投信的猴子</h1>
<div class="sub">資料日 {date}｜資料範圍 {first} 起共 {ndays} 個交易日｜金額單位：億元</div>

<h2>重點族群</h2>
<p class="note">只列投信 5 日淨買超 ≥ {min_val} 億元的族群｜數字為 5 日累計（億元），點卡片看成分股</p>
{picks}

<h2>投信不離不棄</h2>
<p class="note">從近 60 日高點回檔 ≥ {loy_dd}%、且漲到高點前 20 天投信買超 ≥ {loy_a} 億元的個股｜金額為億元｜留存率＝（上漲段買超＋高點後買賣）÷ 上漲段買超</p>
{loyal}
<p class="note">回測（一年、回檔 20%，之後 20 日相對大盤）：不離不棄 平均 +3.1%、中位數 −1.8%｜撤退 平均 −0.2%、中位數 −3.5%｜按兵不動 平均 −1.5%、中位數 −5.3%。大跌後通常還會再跌，不離不棄跌得比較少；這是篩選條件，不是進場訊號。<br>主動 ETF：目前持有的主動式 ETF 檔數，以及跟高點當時相比的檔數與合計張數變化；點一下看是哪幾檔。資料每天累積，涵蓋到高點之前才會顯示變化。</p>

<h2>族群排行</h2>
<div class="filt"><button class="on" data-f="all">全部</button><button data-f="概念">概念族群</button><button data-f="產業">官方產業</button></div>
<div class="scroll">{table}</div>
<p class="note">點欄位標題可排序。強度＝投信5日買超金額 ÷ 族群5日成交金額。<br>
擴散度＝有效買超檔數 ÷ 投信活躍檔數（活躍＝近60日投信有買賣；有效買超＝5日淨買超≥500萬元）。<br>
標籤只在投信5日淨買超 ≥ 5 億元時出現：<b>主動佈局</b> 擴散度20–50%且≥3檔｜<b>過熱</b> 擴散度≥50%｜<b>買超集中</b> 擴散度&lt;20%或不到3檔｜季底窗口：季底月20日後且單一個股佔六成以上買超。<br>
回測（一年、投信買外資賣時，之後20日相對其他產業）：擴散20–35% +1.3%／勝率58%；&lt;20% −0.6%／37%；≥50% −2.9%／24%（樣本少）。近半年效果明顯變弱，標籤是過濾條件，不是進場訊號。</p>

<h2>投信熱度圖（近 {heat_days} 個交易日）</h2>
<div class="hwrap" style="padding:0;border:0">{heat}</div>
<div class="legend"><span>投信賣超</span><div class="bar"></div><span>投信買超</span><span>｜色深＝買賣超 ÷ 族群日均成交額，點格子看數字</span></div>

<h2>成分股明細</h2>
<p class="note">依投信 5 日淨買超金額排序｜● 有效買超（計入擴散度）｜<small>·</small> 近60日投信沒碰過（不計入分母）</p>
{details}
<div id="tip"></div>
</main>
<script>
(function(){{
 const tip=document.getElementById('tip');let tm;
 document.querySelectorAll('.c').forEach(c=>c.addEventListener('click',()=>{{
  tip.textContent=c.dataset.t;tip.style.display='block';clearTimeout(tm);tm=setTimeout(()=>tip.style.display='none',4000);}}));
 document.querySelectorAll('.filt button').forEach(b=>b.addEventListener('click',()=>{{
  document.querySelectorAll('.filt button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
  document.querySelectorAll('#rank tbody tr').forEach(r=>r.style.display=(b.dataset.f==='all'||r.dataset.k===b.dataset.f)?'':'none');}}));
 const tb=document.querySelector('#rank tbody');
 document.querySelectorAll('#rank th[data-s]').forEach(th=>{{let asc=false;th.addEventListener('click',()=>{{
  const i=[...th.parentNode.children].indexOf(th);asc=!asc;
  [...tb.rows].sort((a,b)=>(parseFloat(a.cells[i].dataset.v)-parseFloat(b.cells[i].dataset.v))*(asc?1:-1)).forEach(r=>tb.appendChild(r));}});}});
}})();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="以某一天為報表日，例如 20260331")
    a = ap.parse_args()
    print("讀取資料…", flush=True)
    days, info, q = load(a.date)
    if len(days) < 20:
        print(f"⚠ 只有 {len(days)} 個交易日，部分指標（20日）不完整")
    groups = build_groups(info)
    print(f"  {len(days)} 個交易日、{len(groups)} 個族群，計算中…", flush=True)
    daily, summary = compute(days, info, q, groups)
    loy = loyalty(days, info, q, groups)
    attach_etf(loy, load_etf())
    gd = write_group_daily(daily, groups)
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"投信的猴子_{days[-1]}.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(render(days, daily, summary, groups, loy))

    # 終端機摘要
    print(f"\n報表：{out}\n族群每日指標：{gd}\n")
    print(f"{'族群':<10}{'投信5日(億)':>12}{'強度':>8}{'連續':>6}{'擴散':>6}{'外資5日':>10}  標籤")
    show = [s for s in summary if s["kind"] == "概念"] + \
           sorted([s for s in summary if s["kind"] == "產業"], key=lambda s: -s["t5"])[:10]
    for s in show:
        print(f"{s['group'][:8]:<10}{s['t5']:>12.2f}{s['intensity']:>7.1f}%{s['streak']:>6}"
              f"{s['diff5']*100:>5.0f}%({s['n_buyers']}/{s['n_active']}){s['f5']:>10.2f}  "
              f"{' '.join(t for t, _ in s['tags'])}")
    has = lambda s, t: any(x == t for x, _ in s["tags"])
    by_t5 = sorted(summary, key=lambda s: -s["t5"])
    for title, sel in (
            ("主動佈局＋投信買外資賣", [s for s in by_t5 if has(s, "主動佈局") and has(s, "投信買外資賣")]),
            ("只有主動佈局", [s for s in by_t5 if has(s, "主動佈局") and not has(s, "投信買外資賣")]),
            ("只有投信買外資賣", [s for s in by_t5 if has(s, "投信買外資賣") and not has(s, "主動佈局")])):
        print(f"\n【{title}】{len(sel)} 個：" + ("、".join(s["group"] for s in sel) or "無"))
    for cat in ("不離不棄", "按兵不動"):
        sel = [r for r in loy if r["cat"] == cat]
        print(f"\n【投信{cat}】{len(sel)} 檔：" + ("、".join(r["name"] for r in sel) or "無"))
    print("\n用瀏覽器打開報表，或 AirDrop 到手機看。")


if __name__ == "__main__":
    main()
