#!/usr/bin/env python3
"""
MF + IPO tracker data updater (Python 3.8+, standard library only).

  python update_data.py            # live: AMFI NAV list + mfapi.in history -> data/
  python update_data.py --demo     # synthetic data so you can see the UI (clearly flagged)
  python update_data.py --max-per-bucket 15   # quick test run

Writes data/funds.json, data/ipos.json and data/data.js (what index.html loads).
Signals are rule-based heuristics computed from NAV history. They are not investment advice.
"""
import argparse, bisect, csv, datetime as dt, json, math, os, random, sys, time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

AMFI_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
MFAPI_URL = "https://api.mfapi.in/mf/{}"
RF = 0.065  # risk-free rate used for Sharpe / Sortino
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data")

# order matters: "Large & Mid Cap Fund" must be tested before "Mid Cap Fund"
BUCKETS = [("Large & Mid Cap Fund", "Large & Mid"), ("Large Cap Fund", "Large"),
           ("Mid Cap Fund", "Mid"), ("Small Cap Fund", "Small"), ("Flexi Cap Fund", "Flexi")]


def bucket_of(cat):
    for key, b in BUCKETS:
        if key in cat:
            return b
    return None


def http_get(url, tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 mf-tracker"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError("GET failed %s: %s" % (url, last))


# ---------------------------------------------------------------- AMFI universe
def parse_amfi(text):
    funds, cat = [], ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(";")
        if len(parts) >= 6 and parts[0].isdigit():
            try:
                nav = float(parts[4])
            except ValueError:
                continue
            funds.append({"code": parts[0], "name": parts[3], "nav": nav, "nav_date": parts[5], "cat": cat})
        elif "(" in line and line.endswith(")") and ";" not in line:
            cat = line[line.index("(") + 1:-1]
    return funds


def pick_universe(funds, max_per_bucket):
    out, count = [], {}
    for f in funds:
        if "Equity Scheme" not in f["cat"]:
            continue
        b = bucket_of(f["cat"])
        n = f["name"].lower()
        if not b or "direct" not in n or "growth" not in n:
            continue
        if any(w in n for w in ("idcw", "dividend", "bonus", "payout")):
            continue
        if max_per_bucket and count.get(b, 0) >= max_per_bucket:
            continue
        count[b] = count.get(b, 0) + 1
        f["bucket"] = b
        out.append(f)
    return out


def fetch_history(code):
    j = json.loads(http_get(MFAPI_URL.format(code)))
    rows = []
    for d in j.get("data", []):
        try:
            rows.append((dt.datetime.strptime(d["date"], "%d-%m-%Y").date(), float(d["nav"])))
        except Exception:  # noqa
            pass
    rows.sort()
    return j.get("meta", {}), rows


# ---------------------------------------------------------------- indicators
def sma(v, n):
    return sum(v[-n:]) / n if len(v) >= n else None


def ema_series(v, n):
    k, out = 2.0 / (n + 1), [v[0]]
    for x in v[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def rsi(v, n=14):
    if len(v) < n + 1:
        return None
    g = l = 0.0
    for i in range(1, n + 1):
        d = v[i] - v[i - 1]
        g += max(d, 0)
        l += max(-d, 0)
    ag, al = g / n, l / n
    for i in range(n + 1, len(v)):
        d = v[i] - v[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def macd(v):
    if len(v) < 35:
        return None
    e12, e26 = ema_series(v, 12), ema_series(v, 26)
    line = [a - b for a, b in zip(e12, e26)]
    sig = ema_series(line, 9)
    return {"macd": line[-1], "signal": sig[-1], "hist": line[-1] - sig[-1],
            "hist_prev": line[-2] - sig[-2]}


def ret_cal(dates, navs, days):
    target = dates[-1] - dt.timedelta(days=days)
    if dates[0] > target:
        return None
    i = bisect.bisect_right(dates, target) - 1
    return navs[-1] / navs[i] - 1


def cagr(r, years):
    return None if r is None else (1 + r) ** (1.0 / years) - 1


def daily_returns(navs):
    return [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs)) if navs[i - 1] > 0]


def risk_stats(dates, navs):
    cut = dates[-1] - dt.timedelta(days=365 * 3)
    i0 = max(0, bisect.bisect_left(dates, cut))
    w = navs[i0:]
    r = daily_returns(w)
    if len(r) < 60:
        return None
    mean = sum(r) / len(r)
    sd = math.sqrt(sum((x - mean) ** 2 for x in r) / (len(r) - 1))
    vol = sd * math.sqrt(252)
    rf_d = RF / 252
    dd = math.sqrt(sum(min(x - rf_d, 0) ** 2 for x in r) / len(r)) * math.sqrt(252)
    ann = mean * 252
    peak, mdd = w[0], 0.0
    for x in w:
        peak = max(peak, x)
        mdd = min(mdd, x / peak - 1)
    return {"vol": vol, "sharpe": (ann - RF) / vol if vol else None,
            "sortino": (ann - RF) / dd if dd else None, "maxdd": mdd}


def analyse(dates, navs):
    ind = {}
    ind["rsi"] = rsi(navs)
    m = macd(navs)
    ind["macd"] = m
    ind["sma50"], ind["sma200"] = sma(navs, 50), sma(navs, 200)
    last = dates[-1]
    yr = [n for d, n in zip(dates, navs) if d >= last - dt.timedelta(days=365)]
    ind["hi52"], ind["lo52"] = max(yr), min(yr)
    ind["from_high"] = navs[-1] / ind["hi52"] - 1
    rets = {"1m": ret_cal(dates, navs, 30), "3m": ret_cal(dates, navs, 91),
            "6m": ret_cal(dates, navs, 182), "1y": ret_cal(dates, navs, 365)}
    r3, r5 = ret_cal(dates, navs, 365 * 3), ret_cal(dates, navs, 365 * 5)
    rets["3y"], rets["5y"] = cagr(r3, 3), cagr(r5, 5)
    return ind, rets, risk_stats(dates, navs)


def tech_score(nav, ind):
    """-3..+2.75 raw, mapped to 0..100. Returns (score, reasons)."""
    raw, why = 0.0, []
    r = ind["rsi"]
    if r is not None:
        if r > 80:
            raw -= 1; why.append("RSI %.0f is very overbought" % r)
        elif r > 70:
            raw -= 0.5; why.append("RSI %.0f is overbought" % r)
        elif r >= 55:
            raw += 0.75; why.append("RSI %.0f shows healthy momentum" % r)
        elif r >= 45:
            why.append("RSI %.0f is neutral" % r)
        elif r >= 30:
            raw -= 0.25; why.append("RSI %.0f is weak" % r)
        else:
            raw += 0.5; why.append("RSI %.0f is oversold (possible bounce)" % r)
    m = ind["macd"]
    if m:
        if m["hist"] > 0 and m["hist"] > m["hist_prev"]:
            raw += 1; why.append("MACD above signal and strengthening")
        elif m["hist"] > 0:
            raw += 0.5; why.append("MACD above signal but fading")
        elif m["hist"] > m["hist_prev"]:
            raw -= 0.25; why.append("MACD below signal but improving")
        else:
            raw -= 1; why.append("MACD below signal and weakening")
    s50, s200 = ind["sma50"], ind["sma200"]
    if s50 and s200:
        if nav > s50 > s200:
            raw += 1; why.append("NAV above 50-DMA and 200-DMA (uptrend)")
        elif nav > s200:
            raw += 0.5; why.append("NAV above 200-DMA, below 50-DMA (pullback)")
        elif nav > s50:
            why.append("NAV above 50-DMA but under 200-DMA (recovery)")
        elif s50 < s200:
            raw -= 1; why.append("NAV below both averages (downtrend)")
        else:
            raw -= 0.5; why.append("NAV below 50-DMA and 200-DMA")
    return max(0, min(100, (raw + 3) / 5.75 * 100)), why


def pct_rank(values, key, higher=True):
    """percentile (0..100) of each fund's value within the list."""
    vals = sorted(v for v in values.values() if v is not None)
    out = {}
    for k, v in values.items():
        if v is None or len(vals) < 2:
            out[k] = None
            continue
        below = bisect.bisect_left(vals, v)
        p = below / (len(vals) - 1) * 100
        out[k] = p if higher else 100 - p
    return out


def call_for(total):
    if total >= 70: return "Strong Buy"
    if total >= 58: return "Buy"
    if total >= 45: return "Hold"
    if total >= 32: return "Reduce"
    return "Avoid"


def build_funds(raw_funds):
    """raw_funds: list of dicts with code,name,house,category,bucket,dates,navs"""
    rows = []
    for f in raw_funds:
        dates, navs = f["dates"], f["navs"]
        if len(navs) < 60:
            continue
        ind, rets, risk = analyse(dates, navs)
        step = max(1, len(navs[-260:]) // 120)
        spark = [round(x, 4) for x in navs[-260:][::step]]
        if spark[-1] != round(navs[-1], 4):
            spark.append(round(navs[-1], 4))
        rows.append({"code": f["code"], "name": f["name"], "house": f.get("house", ""),
                     "category": f["category"], "bucket": f["bucket"],
                     "nav": navs[-1], "nav_date": dates[-1].isoformat(),
                     "history_years": round((dates[-1] - dates[0]).days / 365.25, 1),
                     "returns": rets, "ind": ind, "risk": risk, "spark": spark})
    # quality score = percentile within the same cap bucket
    for b in {r["bucket"] for r in rows}:
        grp = [r for r in rows if r["bucket"] == b]
        key = lambda r: r["code"]
        p3 = pct_rank({key(r): r["returns"]["3y"] for r in grp}, key)
        ps = pct_rank({key(r): (r["risk"] or {}).get("sharpe") for r in grp}, key)
        pd = pct_rank({key(r): (r["risk"] or {}).get("maxdd") for r in grp}, key)  # less negative = better
        p1 = pct_rank({key(r): r["returns"]["1y"] for r in grp}, key)
        for r in grp:
            k = key(r)
            parts = [(p3[k], .30), (ps[k], .30), (pd[k], .20), (p1[k], .20)]
            if p3[k] is None or ps[k] is None:
                r["quality"] = None
            else:
                w = sum(x for v, x in parts if v is not None)
                r["quality"] = sum(v * x for v, x in parts if v is not None) / w
    for r in rows:
        t, why = tech_score(r["nav"], r["ind"])
        r["tech"] = t
        r["reasons"] = why
        if r["quality"] is None:
            r["total"], r["call"] = None, "Insufficient history"
            r["reasons"].insert(0, "Less than 3 years of NAV history, so no peer-based quality score")
        else:
            r["total"] = 0.6 * r["quality"] + 0.4 * t
            r["call"] = call_for(r["total"])
            q = r["quality"]
            r["reasons"].insert(0, "Ranks in the %s %.0f%% of %s funds on 3Y return, Sharpe, drawdown and 1Y return" %
                                ("top" if q >= 50 else "bottom", (100 - q) if q >= 50 else q, r["bucket"]))
    rows.sort(key=lambda r: (r["total"] is None, -(r["total"] or 0)))
    return rows


# ---------------------------------------------------------------- live + demo sources
AMFI_URLS = ["https://portal.amfiindia.com/spages/NAVAll.txt", AMFI_URL]
MFAPI_LIST = "https://api.mfapi.in/mf"
NAME_HINTS = ("large cap", "largecap", "bluechip", "blue chip", "mid cap", "midcap", "small cap", "smallcap",
              "flexi cap", "flexicap", "large & mid", "large and mid", "large & midcap")


def get_universe(max_per_bucket):
    for url in AMFI_URLS:
        print("Downloading AMFI NAV list: %s" % url)
        try:
            text = http_get(url)
        except Exception as e:  # noqa
            print("  AMFI download failed: %s" % e)
            continue
        schemes = parse_amfi(text)
        uni = pick_universe(schemes, max_per_bucket)
        print("  got %d characters, %d schemes parsed, %d fit our filters" % (len(text), len(schemes), len(uni)))
        if uni:
            return uni
        print("  start of response: %r" % text[:200])
    print("AMFI gave no usable list. Falling back to the mfapi.in scheme list ...")
    items = json.loads(http_get(MFAPI_LIST))
    print("  mfapi.in returned %d schemes" % len(items))
    out = []
    for it in items:
        n = str(it.get("schemeName", "")).lower()
        if "direct" not in n or "growth" not in n or any(w in n for w in ("idcw", "dividend", "bonus", "payout")):
            continue
        if any(h in n for h in NAME_HINTS):
            out.append({"code": str(it["schemeCode"]), "name": it["schemeName"], "cat": "", "bucket": None})
    if max_per_bucket:
        out = out[:max_per_bucket * 5]
    print("  %d candidate schemes by name; category is confirmed from each fund's own record" % len(out))
    return out


def load_live(max_per_bucket, workers):
    universe = get_universe(max_per_bucket)
    print("Universe: %d direct-growth equity funds" % len(universe))
    raw = []

    def one(f):
        try:
            meta, rows = fetch_history(f["code"])
        except Exception as e:  # noqa
            print("  skip %s: %s" % (f["code"], e))
            return None
        if not rows:
            return None
        cat = meta.get("scheme_category") or f["cat"]
        bucket = f["bucket"] or (bucket_of(cat) if "Equity Scheme" in cat else None)
        if not bucket:
            return None
        return {"code": f["code"], "name": f["name"], "house": meta.get("fund_house", ""),
                "category": cat, "bucket": bucket,
                "dates": [d for d, _ in rows], "navs": [n for _, n in rows]}

    with ThreadPoolExecutor(workers) as ex:
        for i, res in enumerate(ex.map(one, universe), 1):
            if res:
                raw.append(res)
            if i % 25 == 0:
                print("  %d/%d" % (i, len(universe)))
    return raw


def load_demo():
    rnd = random.Random(7)
    spec = {"Large": (0.12, 0.13), "Mid": (0.17, 0.17), "Small": (0.20, 0.21), "Flexi": (0.14, 0.14),
            "Large & Mid": (0.15, 0.15)}
    raw, code = [], 900000
    today = dt.date.today()
    for b, (mu, sg) in spec.items():
        for i in range(1, 7):
            code += 1
            n, nav = 1600, 100.0
            dates, navs = [], []
            d = today - dt.timedelta(days=int(n * 7 / 5))
            while len(navs) < n:
                d += dt.timedelta(days=1)
                if d.weekday() >= 5:
                    continue
                z = rnd.gauss(0, 1)
                nav *= math.exp((mu + rnd.uniform(-.04, .04) - .5 * sg * sg) / 252 + sg / math.sqrt(252) * z)
                dates.append(d); navs.append(nav)
            dates[-1] = today if today.weekday() < 5 else dates[-1]
            raw.append({"code": str(code), "name": "DEMO %s Fund %d - Direct Growth" % (b, i),
                        "house": "Demo AMC", "category": "Equity Scheme - %s Fund" % b, "bucket": b,
                        "dates": dates, "navs": navs})
    return raw


# ---------------------------------------------------------------- IPOs
IPO_NUM = ["price_low", "price_high", "lot_size", "issue_size_cr", "fresh_pct", "ofs_pct", "gmp",
           "sub_qib", "sub_hni", "sub_retail", "sub_total", "rev_growth_pct", "pat_growth_pct",
           "roe_pct", "roce_pct", "debt_equity", "pe_issue", "pe_peer", "promoter_post_pct"]
IPO_TXT = ["name", "board", "open_date", "close_date", "listing_date", "lead_manager", "business", "risks"]
CORE = ["price_high", "gmp", "sub_total", "rev_growth_pct", "pat_growth_pct", "roce_pct", "debt_equity",
        "pe_issue", "pe_peer", "ofs_pct"]


def fnum(x):
    try:
        return float(str(x).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def score_ipo(r):
    raw, why = 0.0, []
    g = r.get("gmp")
    if g is not None and r.get("price_high"):
        p = g / r["price_high"] * 100
        r["gmp_pct"] = round(p, 1)
        if p >= 25: raw += 2; why.append("GMP %.0f%% of issue price: strong listing expectation" % p)
        elif p >= 10: raw += 1; why.append("GMP %.0f%%: decent listing gain expected" % p)
        elif p >= 3: raw += .5; why.append("GMP %.0f%%: modest listing gain" % p)
        elif p >= 0: why.append("GMP near zero: flat listing expected")
        else: raw -= 1.5; why.append("Negative GMP: market expects a listing loss")
    s = r.get("sub_total")
    if s is not None:
        if s >= 50: raw += 1.5; why.append("Subscribed %.0fx overall" % s)
        elif s >= 10: raw += 1; why.append("Subscribed %.0fx overall" % s)
        elif s >= 2: raw += .5; why.append("Subscribed %.1fx overall" % s)
        elif s < 1: raw -= 1; why.append("Under-subscribed (%.1fx)" % s)
    if (r.get("sub_qib") or 0) >= 20:
        raw += .5; why.append("QIB demand %.0fx: institutions are bidding" % r["sub_qib"])
    pi, pp = r.get("pe_issue"), r.get("pe_peer")
    if pi is not None and pi <= 0:
        raw -= 1; why.append("Loss-making at issue: P/E not meaningful")
    elif pi and pp:
        x = pi / pp
        if x <= .8: raw += 1.5; why.append("P/E %.0f vs peers %.0f: priced at a discount" % (pi, pp))
        elif x <= 1.0: raw += .75; why.append("P/E %.0f vs peers %.0f: fairly priced" % (pi, pp))
        elif x <= 1.2: why.append("P/E %.0f vs peers %.0f: slight premium" % (pi, pp))
        else: raw -= 1; why.append("P/E %.0f vs peers %.0f: expensive" % (pi, pp))
    rg, pg = r.get("rev_growth_pct"), r.get("pat_growth_pct")
    if rg is not None and pg is not None:
        if rg >= 25 and pg >= 25: raw += 1.5; why.append("Revenue +%.0f%% and profit +%.0f%%" % (rg, pg))
        elif rg >= 15 and pg >= 15: raw += .75; why.append("Revenue +%.0f%% and profit +%.0f%%" % (rg, pg))
        elif rg < 0 or pg < 0: raw -= 1; why.append("Revenue or profit is shrinking")
    ro = r.get("roce_pct")
    if ro is not None:
        if ro >= 20: raw += 1; why.append("ROCE %.0f%%: high-return business" % ro)
        elif ro >= 12: raw += .5; why.append("ROCE %.0f%%: adequate" % ro)
        elif ro < 8: raw -= .5; why.append("ROCE %.0f%%: weak" % ro)
    de = r.get("debt_equity")
    if de is not None:
        if de > 2: raw -= 1; why.append("Debt/equity %.1f: heavily leveraged" % de)
        elif de < .5: raw += .5; why.append("Debt/equity %.1f: low leverage" % de)
    if (r.get("ofs_pct") or 0) > 70:
        raw -= .75; why.append("Mostly offer-for-sale: little money goes into the company")
    elif (r.get("fresh_pct") or 0) >= 50:
        raw += .5; why.append("Majority fresh issue: funds go to the business")
    have = sum(1 for k in CORE if r.get(k) is not None)
    r["completeness"] = round(have / len(CORE) * 100)
    r["score"] = round(max(0, min(100, (raw + 6) / 14 * 100)))
    if r["completeness"] < 50:
        r["verdict"] = "Insufficient data"
    elif r["score"] >= 70:
        r["verdict"] = "Apply"
    elif r["score"] >= 55:
        r["verdict"] = "Apply for listing gain" if (r.get("gmp_pct") or 0) > 0 else "Neutral"
    elif r["score"] >= 40:
        r["verdict"] = "Neutral"
    else:
        r["verdict"] = "Avoid"
    r["reasons"] = why
    return r


def ipo_status(r, today):
    def d(x):
        try: return dt.date.fromisoformat(x)
        except Exception: return None  # noqa
    o, c, l = d(r.get("open_date")), d(r.get("close_date")), d(r.get("listing_date"))
    if l and today >= l: return "Listed"
    if c and today > c: return "Closed"
    if o and c and o <= today <= c: return "Open"
    if o and today < o: return "Upcoming"
    return "Unknown"


def load_ipos(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            if not (row.get("name") or "").strip():
                continue
            r = {k: (row.get(k) or "").strip() for k in IPO_TXT}
            r.update({k: fnum(row.get(k)) for k in IPO_NUM})
            out.append(r)
    return out


def demo_ipos():
    t = dt.date.today()
    f = lambda n: (t + dt.timedelta(days=n)).isoformat()
    return [
        {"name": "DEMO Alpha Foods Ltd", "board": "Mainboard", "open_date": f(-1), "close_date": f(2), "listing_date": f(7),
         "price_low": 270, "price_high": 285, "lot_size": 52, "issue_size_cr": 900, "fresh_pct": 60, "ofs_pct": 40,
         "gmp": 70, "sub_qib": 18, "sub_hni": 25, "sub_retail": 6, "sub_total": 14, "rev_growth_pct": 27,
         "pat_growth_pct": 34, "roe_pct": 19, "roce_pct": 24, "debt_equity": .3, "pe_issue": 24, "pe_peer": 31,
         "promoter_post_pct": 61, "lead_manager": "Demo Capital", "business": "Packaged foods (demo row)", "risks": "Demo data"},
        {"name": "DEMO Beta Infra Ltd", "board": "Mainboard", "open_date": f(3), "close_date": f(5), "listing_date": f(10),
         "price_low": 140, "price_high": 150, "lot_size": 100, "issue_size_cr": 2400, "fresh_pct": 15, "ofs_pct": 85,
         "gmp": -4, "sub_qib": None, "sub_hni": None, "sub_retail": None, "sub_total": None, "rev_growth_pct": 6,
         "pat_growth_pct": -3, "roe_pct": 8, "roce_pct": 7, "debt_equity": 2.6, "pe_issue": 38, "pe_peer": 26,
         "promoter_post_pct": 70, "lead_manager": "Demo Securities", "business": "Roads and EPC (demo row)", "risks": "Demo data"},
    ]


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="synthetic data, clearly flagged in the UI")
    ap.add_argument("--max-per-bucket", type=int, default=0, help="0 = every fund")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--ipo-csv", default=os.path.join(OUT, "ipo_input.csv"))
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)

    raw = load_demo() if a.demo else load_live(a.max_per_bucket, a.workers)
    funds = build_funds(raw)
    if not funds:
        sys.exit("No funds produced. Nothing written.")
    today = dt.date.today()
    ipos = demo_ipos() if a.demo else load_ipos(a.ipo_csv)
    for r in ipos:
        score_ipo(r)
        r["status"] = ipo_status(r, today)
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).isoformat(timespec="minutes")
    meta = {"generated_at": now, "demo": a.demo,
            "source": "Synthetic demo data" if a.demo else "AMFI NAVAll.txt + mfapi.in (NAV history)"}
    data = {"meta": meta, "funds": funds, "ipos": ipos}
    with open(os.path.join(OUT, "funds.json"), "w") as fh: json.dump({"meta": meta, "funds": funds}, fh)
    with open(os.path.join(OUT, "ipos.json"), "w") as fh: json.dump({"meta": meta, "ipos": ipos}, fh)
    with open(os.path.join(OUT, "data.js"), "w") as fh:
        fh.write("window.__TRACKER__ = " + json.dumps(data) + ";\n")
    tpl = os.path.join(OUT, "ipo_input.csv")
    if not os.path.exists(tpl):
        with open(tpl, "w", newline="") as fh:
            csv.writer(fh).writerow(IPO_TXT[:5] + IPO_NUM + IPO_TXT[5:])
    print("Wrote %d funds, %d IPOs -> %s" % (len(funds), len(ipos), OUT))


if __name__ == "__main__":
    main()
