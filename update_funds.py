"""
Builds funds.csv and history.csv for the screener from a list of tickers.

Input:  tickers.csv  - needs a "Symbol" column with London Stock Exchange
                       tickers (e.g. VUSA, SMGB). ".L" is added automatically
                       for Yahoo; a symbol that already has a suffix (e.g.
                       "SPY" won't, "SXR8.DE" will) is used as-is.
                       Optional columns: Category, Class (eq/bond/hy/reit/gold),
                       PE_10y_Avg.
Output: funds.csv    - one row per fund: price, 52-week range, 200-day
                       average, RSI, 5-yr return, volatility, max drawdown,
                       fees, yield, P/E. All prices in pounds (GBP).
        history.csv  - date,ticker,close_gbp for every trading day, kept for up
                       to 10 years. Saved baskets use it to chart performance.
        risk.json    - weekly total returns (in pounds, dividends included) for
                       every fund over up to 10 years. The site uses it to
                       measure each basket's own volatility and worst drop.
                       A fund younger than the window borrows the average of
                       similar funds before its launch (its "from" date says
                       where its real data starts).
        info_cache.json - fund details and each fund's quote currency.

London ETFs are quoted in pence (GBp), pounds (GBP) or dollars (USD) depending
on the share class. The script looks up each fund's quote currency from Yahoo
and converts everything to pounds, so the site always compares like with like.

Data comes from Yahoo Finance via the free `yfinance` library. It's unofficial
and meant for personal use; for a public or commercial site, swap in a
licensed data provider.

Run:  pip install yfinance pandas xlrd
      python update_funds.py
"""
import csv
import datetime as dt
import io
import json
import math
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

import pandas as pd
import yfinance as yf

TICKER_FILE = "tickers.csv"
OUT_FILE = "funds.csv"
HIST_FILE = "history.csv"
HIST_HEADER = ["date", "ticker", "close_gbp"]
HIST_KEEP_DAYS = 3650
RISK_FILE = "risk.json"
PRICE_PERIOD = "10y"             # long enough to include the 2020 crash and 2022 rate shock
MIN_FUNDS_FOR_START = 10         # the risk window starts once this many funds have data
INFO_CACHE = "info_cache.json"
EXCHANGE_SUFFIX = ".L"           # London Stock Exchange on Yahoo
BASE = "GBP"                     # every price in the output is in pounds
INFO_MAX_AGE_DAYS = 7            # refresh fund details about once a week
CALL_TIMEOUT = 15                # seconds to wait for any single Yahoo lookup
CURRENCY_BUDGET = 240            # seconds to spend finding quote currencies
INFO_BUDGET = 240                # seconds to spend refreshing fund details
BATCH = 50                       # tickers per price download
FX = {"USD": "GBP=X", "EUR": "EURGBP=X"}   # Yahoo: pounds per 1 USD / 1 EUR
# Today's bond yields, used for the site's bond base case (a bond's best 1-year guide is its yield):
# Bank of England daily gilt par yields and Bank Rate; US Treasury yields from Yahoo.
BOE_SERIES = {"g5": "IUDSNPY", "g10": "IUDMNPY", "g20": "IUDLNPY", "bank": "IUDBEDR"}
US_YIELDS = {"us5": "^FVX", "us10": "^TNX", "us30": "^TYX"}
BOE_URL = ("https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?csv.x=yes"
           "&Datefrom={start}&Dateto=now&SeriesCodes={codes}&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N")
# Market conditions that move a one-year outlook (all free; each one is optional and falls back to
# the last good value). The site uses them as inputs, never as a headline-driven forecast.
BOE_INFLATION = {"infl5": "IUDSIZC", "infl10": "IUDMIZC"}        # market-implied inflation (RPI), zero coupon
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={code}"   # no key needed for this endpoint
FRED_SPREADS = {"ig": "BAMLC0A0CM", "hy_us": "BAMLH0A0HYM2", "hy_eu": "BAMLHE00EHYIOAS"}  # ICE BofA OAS, %
VOL_INDEXES = {"vix": "^VIX", "move": "^MOVE"}                  # expected share / bond turbulence
PE_PROXIES = {"US": "SPY", "Europe": "VGK", "UK": "EWU", "Japan": "EWJ", "Asia Pacific": "EPP",
              "Emerging": "EEM", "World": "ACWI"}                 # broad index funds: market P/E by region
GPR_PAGE = "https://www.matteoiacoviello.com/gpr.htm"            # Caldara-Iacoviello geopolitical risk index
MACRO_KEEP_DAYS = 60
VOL_HISTORY = {}                    # daily VIX / MOVE closes from this run, for the weekly history
# weekly history of the inputs the bond-choice rules use, so the site can test the rules over ten years
BOE_HISTORY = {"g5": "IUDSNPY", "g10": "IUDMNPY", "bank": "IUDBEDR", "infl10": "IUDMIZC"}
FRED_HISTORY = {"baa": "BAA10Y"}    # Moody's Baa company bonds over 10-year Treasuries: decades of history


def yahoo(sym):
    return sym if "." in sym else sym + EXCHANGE_SUFFIX


def read_tickers(path):
    """Return a list of dicts with at least 'symbol'; skips disclaimer lines."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    head_i = next(
        (i for i, r in enumerate(rows) if any(c.strip().lower() in ("symbol", "ticker") for c in r)),
        None,
    )
    if head_i is None:
        sys.exit(f"{path} needs a Symbol or Ticker column.")
    heads = [h.strip().lower() for h in rows[head_i]]
    out = []
    for r in rows[head_i + 1:]:
        d = {heads[i]: (r[i].strip() if i < len(r) else "") for i in range(len(heads))}
        sym = (d.get("symbol") or d.get("ticker") or "").upper()
        if sym and sym.replace(".", "").replace("-", "").isalnum() and len(sym) <= 12:
            d["symbol"] = sym
            out.append(d)
    return out


def download(yahoo_symbols, period=PRICE_PERIOD):
    """Daily Close and Adj Close. Returns {yahoo_symbol: DataFrame}."""
    result = {}
    for i in range(0, len(yahoo_symbols), BATCH):
        chunk = yahoo_symbols[i:i + BATCH]
        print(f"  prices {i + 1}-{i + len(chunk)} of {len(yahoo_symbols)}", flush=True)
        try:
            df = yf.download(chunk, period=period, interval="1d", auto_adjust=False,
                             group_by="ticker", progress=False, threads=True, timeout=30)
        except Exception as e:
            print(f"  download failed: {e}", flush=True)
            continue
        for s in chunk:
            try:
                sub = df[s] if isinstance(df.columns, pd.MultiIndex) else df
                sub = sub[["Close", "Adj Close"]].dropna()
                if len(sub) > 30:
                    result[s] = sub
            except KeyError:
                pass
        time.sleep(1)
    return result


def with_timeout(fn):
    """Run a Yahoo lookup, giving up after CALL_TIMEOUT (yfinance can hang)."""
    box = {}

    def work():
        try:
            box["v"] = fn()
        except Exception:
            pass

    t = threading.Thread(target=work, daemon=True)  # daemon: never blocks exit
    t.start()
    t.join(CALL_TIMEOUT)
    return box.get("v")


def load_cache():
    try:
        with open(INFO_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def find_currencies(symbols, cache):
    """Quote currency per fund (GBp, GBP, USD...). Cached forever: it doesn't change."""
    missing = [s for s in symbols if not cache.get(s, {}).get("currency")]
    print(f"Quote currencies: {len(symbols) - len(missing)} cached, {len(missing)} to look up", flush=True)
    start = time.time()
    for s in missing:
        if time.time() - start > CURRENCY_BUDGET:
            print("  time budget reached; the rest will be looked up next run", flush=True)
            break
        cur = with_timeout(lambda s=s: yf.Ticker(yahoo(s)).fast_info["currency"])
        if cur:
            cache.setdefault(s, {})["currency"] = cur
        time.sleep(0.3)


def refresh_info(symbols, cache):
    """Fund details, refreshed about weekly within a time budget."""
    today = dt.date.today()
    stale = [s for s in symbols
             if (today - dt.date.fromisoformat(cache.get(s, {}).get("fetched", "2000-01-01"))).days >= INFO_MAX_AGE_DAYS]
    print(f"Fund details: {len(symbols) - len(stale)} cached, {len(stale)} to fetch", flush=True)
    start = time.time()
    for n, s in enumerate(stale):
        if time.time() - start > INFO_BUDGET:
            print(f"  time budget reached; {len(stale) - n} left for the next run", flush=True)
            break
        raw = with_timeout(lambda s=s: yf.Ticker(yahoo(s)).info or {})
        if raw:
            entry = cache.setdefault(s, {})
            entry.update(parse_info(raw))
            if raw.get("currency"):
                entry["currency"] = raw["currency"]
            entry["fetched"] = today.isoformat()
        time.sleep(0.5)


def parse_info(info):
    """Name, fees, yield, P/E. Fields vary by fund; all optional."""
    er = info.get("netExpenseRatio")  # already a percent, e.g. 0.07
    y = info.get("yield") or info.get("dividendYield")
    if y is not None and y < 1:  # fraction -> percent
        y *= 100
    return {
        "name": info.get("longName") or info.get("shortName"),
        "expense_ratio": er,
        "yield": y,
        "pe": info.get("trailingPE"),
    }


def fix_unit_glitches(s):
    """Yahoo sometimes reports an LSE day in pounds instead of pence (or the
    reverse), a 100x jump. Put those days back in line with their neighbours."""
    med = s.rolling(15, center=True, min_periods=3).median()
    ratio = s / med
    s = s.copy()
    s[ratio > 30] /= 100
    s[ratio < 1 / 30] *= 100
    return s


SPLITS = {}          # symbol -> [{"date", "ratio"}] found this run (ratio = new price / old price)


def fix_splits(s, sym=None):
    """A share split or consolidation shows up as a permanent jump in the price (GILS fell from 100.5 to
    4.99 overnight in July 2026: a 20-for-1 split Yahoo didn't adjust). ETFs don't move 45% in a day, so
    a jump that big which STAYS is rescaled away: earlier prices are multiplied by the ratio."""
    s = s.copy()
    v = s.to_numpy(dtype=float, copy=True)
    for i in range(1, len(v)):
        a, b = v[i - 1], v[i]
        if not (a > 0 and b > 0):
            continue
        r = b / a
        if 1 / 1.8 < r < 1.8:
            continue
        before = pd.Series(v[max(0, i - 5):i]).median()
        after = pd.Series(v[i:i + 5]).median()
        if before > 0 and abs((after / before) / r - 1) < 0.1:      # the new level holds: a split, not a spike
            v[:i] = v[:i] * r
            if sym:
                SPLITS.setdefault(sym, [])
                d = s.index[i].date().isoformat()
                if not any(x["date"] == d for x in SPLITS[sym]):
                    SPLITS[sym].append({"date": d, "ratio": round(float(r), 6)})
    return pd.Series(v, index=s.index)


def fix_spikes(s):
    """One-day bad prints that snap straight back (on 24 Oct 2025 Yahoo priced several London funds 34%
    too high for a single day). A price more than 20% away from the days either side of it is replaced."""
    med = s.rolling(5, center=True, min_periods=3).median()
    ratio = s / med
    bad = (ratio > 1.25) | (ratio < 0.8)
    bad.iloc[-2:] = False       # the latest days can't be checked against what comes next yet
    if bad.any():
        s = s.mask(bad).interpolate(limit_direction="both")
    return s


def clean_prices(h, sym=None):
    h = h.apply(fix_unit_glitches)
    h = h.apply(lambda c: fix_splits(c, sym if c.name == "Close" else None))
    h = h.apply(fix_spikes)
    if len(h) > 60:
        h = h.iloc[3:]          # a fund's first few trading days are often mispriced; similar funds stand in
    return h


def to_pounds(h, currency, fx, sym=None):
    """Convert a Close/Adj Close frame from its quote currency to pounds."""
    h = clean_prices(h, sym)
    if currency in ("GBp", "GBX"):
        return h / 100
    if currency == "GBP":
        return h
    if currency in fx:
        rate = fx[currency].reindex(h.index).ffill().bfill()
        return h.mul(rate, axis=0)
    return None


def rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss
    return float(100 - 100 / (1 + rs.iloc[-1]))


def metrics(h):
    h = h[h.index >= h.index[-1] - pd.DateOffset(years=5)]   # fund stats stay 5-year
    close, adj = h["Close"], h["Adj Close"]
    last_year = close.iloc[-252:]
    years = (adj.index[-1] - adj.index[0]).days / 365.25
    rets = adj.pct_change().dropna()
    peak = adj.cummax()
    return {
        "price": float(close.iloc[-1]),
        "low_52w": float(last_year.min()),
        "high_52w": float(last_year.max()),
        "ma_200": float(close.iloc[-200:].mean()) if len(close) >= 200 else None,
        "rsi": rsi(close),
        # total return incl. dividends, annualized; only if we have ~5 years
        "return_5y": (float(adj.iloc[-1] / adj.iloc[0]) ** (1 / years) - 1) * 100 if years > 4.5 else None,
        "volatility": float(rets.iloc[-756:].std() * math.sqrt(252) * 100),
        "max_drawdown": float(((adj / peak) - 1).min() * 100),
        "dist_yield": trailing_yield(close, adj),
        # total return (incl. dividends, in pounds) had you bought 3, 6 or 12 months ago
        **{f"return_{k}": back_return(adj, months) for k, months in (("3m", 3), ("6m", 6), ("1y", 12))},
    }


def trailing_yield(close, adj):
    """Dividends paid over the last 12 months as % of today's price, worked out from the gap between
    total return (Adj Close) and price return (Close). Accumulating funds pay nothing, so show ~0."""
    start = close.index[-1] - pd.DateOffset(months=12)
    if close.index[0] > start:
        return None
    c0, a0 = close.asof(start), adj.asof(start)
    if not c0 or not a0:
        return None
    y = ((adj.iloc[-1] / a0) / (close.iloc[-1] / c0) - 1) * 100
    return float(max(y, 0.0)) if -0.5 < y < 15 else None   # outside this range it's a data glitch


def market_yields(cache):
    """Gilt yields and Bank Rate (Bank of England) plus US Treasury yields (Yahoo), in %.
    Falls back to the last good values in info_cache.json if a source is unavailable."""
    out = {}
    try:
        start = (dt.date.today() - dt.timedelta(days=45)).strftime("%d/%b/%Y")
        url = BOE_URL.format(start=start, codes=",".join(BOE_SERIES.values()))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (fund-data-bot)"})
        with urllib.request.urlopen(req, timeout=CALL_TIMEOUT) as r:
            rows = list(csv.reader(r.read().decode("utf-8", "replace").splitlines()))
        head = [h.strip().upper() for h in rows[0]]
        for key, code in BOE_SERIES.items():
            if code in head:
                i = head.index(code)
                vals = [(row[0], row[i]) for row in rows[1:] if len(row) > i and row[i].strip() not in ("", "n/a")]
                if vals:
                    out[key] = float(vals[-1][1])
                    d = dt.datetime.strptime(vals[-1][0].strip(), "%d %b %Y").date().isoformat()
                    if key != "bank":                       # gilt yields date the curve; Bank Rate lags
                        out["uk_as_of"] = max(out.get("uk_as_of", ""), d)
    except Exception as e:
        print(f"  Bank of England yields unavailable: {e}", flush=True)
    try:
        raw = download(list(US_YIELDS.values()), period="3mo")
        for key, sym in US_YIELDS.items():
            if sym in raw:
                v = float(raw[sym]["Close"].iloc[-1])
                out[key] = v / 10 if v > 25 else v      # some feeds quote yield x10
    except Exception as e:
        print(f"  US Treasury yields unavailable: {e}", flush=True)
    good = {k: v for k, v in out.items() if isinstance(v, str) or 0 <= v < 20}
    old = cache.get("_yields", {})
    merged = {**old, **good}
    if merged:
        if good:
            merged["fetched"] = dt.date.today().isoformat()
        cache["_yields"] = merged
    print(f"Bond yields: {', '.join(f'{k} {v}' for k, v in merged.items())}", flush=True)
    return merged


def http_text(url, timeout=CALL_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (fund-data-bot)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def pct_rank(series, value):
    s = [x for x in series if x == x]
    return round(100 * sum(1 for x in s if x <= value) / len(s)) if s else None


def market_macro(cache, yields):
    """Fear gauges, credit spreads, implied inflation, market valuations and geopolitical risk.
    Every part is optional: a source that fails keeps its last good value from info_cache.json."""
    old = cache.get("_macro", {})
    out = {}
    today = dt.date.today().isoformat()
    # 1. VIX (shares) and MOVE (bonds): today vs their own 10-year history
    try:
        raw = download(list(VOL_INDEXES.values()), period="10y")
        for key, sym in VOL_INDEXES.items():
            if sym in raw:
                c = raw[sym]["Close"].dropna()
                VOL_HISTORY[key] = c
                now = float(c.iloc[-5:].mean())                       # a 1-week average, so one spike day doesn't rule
                out[key] = {"now": round(now, 1), "median": round(float(c.median()), 1),
                            "pct": pct_rank(c.tolist(), now), "as_of": c.index[-1].date().isoformat()}
    except Exception as e:
        print(f"  volatility indexes unavailable: {e}", flush=True)
    # 2. credit spreads (FRED, ICE BofA option-adjusted spreads)
    spreads = {}
    for key, code in FRED_SPREADS.items():
        try:
            rows = list(csv.reader(http_text(FRED_URL.format(code=code)).decode("utf-8", "replace").splitlines()))
            vals = [(r[0], float(r[1])) for r in rows[1:] if len(r) > 1 and r[1] not in ("", ".")]
            if vals:
                hist = [v for _, v in vals[-260 * 10:]]
                spreads[key] = {"now": vals[-1][1], "median": round(float(pd.Series(hist).median()), 2),
                                "pct": pct_rank(hist, vals[-1][1]), "as_of": vals[-1][0]}
        except Exception as e:
            print(f"  credit spread {code} unavailable: {e}", flush=True)
    if spreads:
        out["spreads"] = spreads
    # 3. market-implied inflation (Bank of England)
    try:
        start = (dt.date.today() - dt.timedelta(days=60)).strftime("%d/%b/%Y")
        rows = list(csv.reader(http_text(BOE_URL.format(start=start, codes=",".join(BOE_INFLATION.values())))
                               .decode("utf-8", "replace").splitlines()))
        head = [h.strip().upper() for h in rows[0]]
        infl = {}
        for key, code in BOE_INFLATION.items():
            if code in head:
                i = head.index(code)
                vals = [(r[0], r[i]) for r in rows[1:] if len(r) > i and r[i].strip() not in ("", "n/a")]
                if vals and 0 < float(vals[-1][1]) < 10:
                    infl[key] = float(vals[-1][1])
                    infl["as_of"] = dt.datetime.strptime(vals[-1][0].strip(), "%d %b %Y").date().isoformat()
        if infl:
            out["inflation"] = infl
    except Exception as e:
        print(f"  implied inflation unavailable: {e}", flush=True)
    # 4. market valuations: trailing P/E of broad regional index funds
    pe = {}
    for region, sym in PE_PROXIES.items():
        info = with_timeout(lambda sym=sym: yf.Ticker(sym).info or {})
        v = (info or {}).get("trailingPE")
        if isinstance(v, (int, float)) and 5 < v < 60:
            pe[region] = round(float(v), 1)
        time.sleep(0.3)
    if pe:
        out["pe"] = {**old.get("pe", {}), **pe, "as_of": today}
    # 5. geopolitical risk (daily index; the file name changes, so find it on the page)
    try:
        import re
        page = http_text(GPR_PAGE).decode("utf-8", "replace")
        links = re.findall(r'href="([^"]*data_gpr_daily_recent[^"]*\.xls)"', page)
        if links:
            url = urllib.parse.urljoin(GPR_PAGE, links[0])
            df = pd.read_excel(io.BytesIO(http_text(url, timeout=60)))
            cols = {c.upper(): c for c in df.columns}
            dcol = cols.get("DATE") or cols.get("DAY")
            g = df[cols["GPRD"]].astype(float)
            d = pd.to_datetime(df[dcol].astype(str), errors="coerce")
            ok = d.notna() & g.notna()
            g, d = g[ok], d[ok]
            now = float(g.iloc[-30:].mean())                         # 30-day average: daily counts are noisy
            roll = g.rolling(30).mean().dropna()
            out["gpr"] = {"now": round(now, 1), "median": round(float(roll.median()), 1),
                          "pct": pct_rank(roll.tolist(), now), "as_of": d.iloc[-1].date().isoformat()}
    except Exception as e:
        print(f"  geopolitical risk index unavailable: {e}", flush=True)
    merged = {**old, **out}
    if out:
        merged["fetched"] = today
    cache["_macro"] = merged
    # a small daily snapshot, so the site can say what changed since last week
    snap = {k: v for k, v in (yields or {}).items() if isinstance(v, (int, float))}
    for k in ("vix", "move", "gpr"):
        if k in merged:
            snap[k] = merged[k]["now"]
    for k, v in merged.get("spreads", {}).items():
        snap["sp_" + k] = v["now"]
    for k, v in merged.get("inflation", {}).items():
        if isinstance(v, (int, float)):
            snap[k] = v
    for k, v in merged.get("pe", {}).items():
        if isinstance(v, (int, float)):
            snap["pe_" + k] = v
    hist = cache.get("_macro_hist", {})
    hist[today] = snap
    cutoff = (dt.date.today() - dt.timedelta(days=MACRO_KEEP_DAYS)).isoformat()
    cache["_macro_hist"] = {d: v for d, v in sorted(hist.items()) if d >= cutoff}
    week_ago = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    older = [d for d in cache["_macro_hist"] if d <= week_ago]
    prev = {"date": older[-1], **cache["_macro_hist"][older[-1]]} if older else None
    print("Market conditions: " + ", ".join(f"{k} {v}" for k, v in snap.items()), flush=True)
    return merged, prev


def macro_weekly(dates):
    """Weekly (Friday) values of gilt yields, Bank Rate, implied inflation and a long-history credit spread,
    aligned with risk.json's weeks. Missing weeks are carried forward; anything unavailable is left out."""
    out = {}
    idx = pd.to_datetime(dates)
    try:
        start = (dt.date.fromisoformat(dates[0]) - dt.timedelta(days=30)).strftime("%d/%b/%Y")
        rows = list(csv.reader(http_text(BOE_URL.format(start=start, codes=",".join(BOE_HISTORY.values())), timeout=60)
                               .decode("utf-8", "replace").splitlines()))
        head = [h.strip().upper() for h in rows[0]]
        when = pd.to_datetime([r[0].strip() for r in rows[1:]], format="%d %b %Y", errors="coerce")
        for key, code in BOE_HISTORY.items():
            if code in head:
                i = head.index(code)
                v = pd.Series([float(r[i]) if len(r) > i and r[i].strip() not in ("", "n/a") else float("nan") for r in rows[1:]], index=when)
                out[key] = v[v.index.notna()].sort_index()
    except Exception as e:
        print(f"  Bank of England history unavailable: {e}", flush=True)
    for key, code in FRED_HISTORY.items():
        try:
            rows = list(csv.reader(http_text(FRED_URL.format(code=code), timeout=60).decode("utf-8", "replace").splitlines()))
            vals = [(r[0], float(r[1])) for r in rows[1:] if len(r) > 1 and r[1] not in ("", ".")]
            out[key] = pd.Series([v for _, v in vals], index=pd.to_datetime([d for d, _ in vals])).sort_index()
        except Exception as e:
            print(f"  {code} history unavailable: {e}", flush=True)
    for key in ("vix",):
        if key in VOL_HISTORY:
            out[key] = VOL_HISTORY[key]
    res = {}
    for key, ser in out.items():
        wk = ser.dropna().resample("W-FRI").last().ffill().reindex(idx, method="ffill")
        if wk.notna().sum() > 52:
            res[key] = [None if v != v else round(float(v), 3) for v in wk]
    print(f"Weekly market history: {', '.join(f'{k} {sum(x is not None for x in v)} weeks' for k, v in res.items()) or 'none'}", flush=True)
    return res


def back_return(adj, months):
    """% change from the last close on or before `months` ago to the latest close."""
    start = adj.index[-1] - pd.DateOffset(months=months)
    if adj.index[0] > start:
        return None  # fund too new
    then = adj.asof(start)
    return (float(adj.iloc[-1]) / float(then) - 1) * 100 if then and then > 0 else None


def update_history(hist):
    """Merge the latest closes (in pounds) into history.csv. The first run seeds
    one year; later runs add the last few days, so a missed run fills itself in.
    An older file in a different format (e.g. US dollar prices) is replaced."""
    rows, fresh = {}, True
    if os.path.exists(HIST_FILE):
        with open(HIST_FILE, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            if next(reader, None) == HIST_HEADER:
                fresh = False
                for r in reader:
                    if len(r) == 3:
                        rows[(r[0], r[1])] = r[2]
            else:
                print("history.csv is in an old format; starting it again in pounds.")
    recent = 260 if fresh else 10
    for s, h in hist.items():
        for d, c in h["Close"].iloc[-recent:].items():
            rows[(d.date().isoformat(), s)] = f"{float(c):.4f}"
    # rows already in the file are refreshed from today's cleaned download, so a split or bad print
    # corrected this run is corrected in the saved history too
    closes = {s: {d.date().isoformat(): float(c) for d, c in h["Close"].items()} for s, h in hist.items()}
    fixed = 0
    for (d, s), old in list(rows.items()):
        new = closes.get(s, {}).get(d)
        if new is not None and abs(float(old) / new - 1) > 0.001:
            rows[(d, s)] = f"{new:.4f}"
            fixed += 1
    if fixed:
        print(f"history.csv: corrected {fixed} earlier prices (splits or bad prints)")
    cutoff = (dt.date.today() - dt.timedelta(days=HIST_KEEP_DAYS)).isoformat()
    with open(HIST_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HIST_HEADER)
        for (d, s), c in sorted(rows.items()):
            if d >= cutoff:
                w.writerow([d, s, c])
    print(f"history.csv now holds {len(rows)} prices.")


def weekly_returns(adj):
    wk = adj.resample("W-FRI").last().dropna()
    return wk.pct_change().dropna().clip(-0.6, 0.6)   # guard against stray data glitches


def build_risk(hist, rows, as_of, stand_ins=None):
    """Weekly total returns per fund for risk.json. Gaps before a fund launched are
    filled first from its US equivalent (tickers.csv US_equivalent column), then
    with the average of its category, else its asset class, else all funds, so
    every basket can be measured over the same window."""
    stand_ins = stand_ins or {}
    meta = {r["symbol"]: ((r.get("category") or "").strip(), (r.get("class") or "eq").strip()) for r in rows}
    weekly = {}
    for s, h in hist.items():
        r = weekly_returns(h["Adj Close"])
        if len(r) >= 8:
            weekly[s] = r
    if not weekly:
        return None
    rets = pd.DataFrame(weekly).sort_index()
    counts = rets.notna().sum(axis=1)
    rets = rets[counts.cumsum() > 0]
    start_ok = counts[counts >= min(MIN_FUNDS_FOR_START, len(weekly))]
    if len(start_ok):
        rets = rets[rets.index >= start_ok.index[0]]
    # a fund's first weeks are often mispriced (FWRG showed +60% in its first full week), so for funds that
    # launched inside the window, the first two weekly returns come from its stand-in or similar funds instead
    for s in rets:
        first = rets[s].first_valid_index()
        if first is not None and first > rets.index[0]:
            rets.loc[rets.index[rets.index.get_loc(first):rets.index.get_loc(first) + 2], s] = float("nan")
    real_from = {s: rets[s].first_valid_index() for s in rets}
    groups = {}
    for s in rets:
        cat, cls = meta.get(s, ("", "eq"))
        groups.setdefault(("cat", cat), []).append(s)
        groups.setdefault(("cls", cls), []).append(s)
    out = {}
    for s in rets:
        col = rets[s].copy()
        cat, cls = meta.get(s, ("", "eq"))
        proxy = None
        if s in stand_ins and col.isna().any():
            sym, series = stand_ins[s]
            filled = col.fillna(series.reindex(rets.index))
            if filled.notna().sum() > col.notna().sum():
                proxy = {"t": sym, "from": series.first_valid_index().date().isoformat()}
                col = filled
        for peers in (groups.get(("cat", cat), []), groups.get(("cls", cls), []), list(rets.columns)):
            peers = [p for p in peers if p != s]
            if peers and col.isna().any():
                col = col.fillna(rets[peers].mean(axis=1))
        col = col.fillna(0.0)
        out[s] = {"from": real_from[s].date().isoformat() if real_from[s] is not None else None,
                  "r": [int(round(v * 10000)) for v in col]}   # in 0.01% steps, to keep the file small
        if proxy:
            out[s]["proxy"] = proxy
    return {"as_of": as_of, "freq": "weekly", "unit": 0.0001, "basis": "total return in GBP",
            # weeks are labelled by their Friday; the current, unfinished week gets its last trading day
            "dates": [min(d.date().isoformat(), as_of) for d in rets.index], "funds": out}


def stand_ins_for(hist, rows, fx):
    """US-listed equivalents for funds that launched after the risk window starts.
    Their returns (converted to pounds) stand in before the London fund existed."""
    if not hist or "USD" not in fx:
        return {}
    first = min(h.index[0] for h in hist.values())
    late = {r["symbol"]: (r.get("us_equivalent") or "").strip().upper() for r in rows
            if r["symbol"] in hist and hist[r["symbol"]].index[0] > first + pd.Timedelta(days=60)}
    late = {s: u for s, u in late.items() if u and u.replace(".", "").replace("-", "").isalnum()}
    if not late:
        return {}
    print(f"Stand-ins: fetching {len(set(late.values()))} US equivalents for late-launched funds", flush=True)
    raw = download(sorted(set(late.values())))
    out = {}
    for s, u in late.items():
        if u in raw:
            conv = to_pounds(raw[u], "USD", fx)
            if conv is not None:
                out[s] = (u, weekly_returns(conv["Adj Close"]))
    return out


def fmt(v, d=2):
    return "" if v is None or (isinstance(v, float) and math.isnan(v)) else round(v, d)


def main():
    rows = read_tickers(TICKER_FILE)
    symbols = [r["symbol"] for r in rows]
    print(f"Fetching {len(symbols)} funds…", flush=True)

    raw = download([yahoo(s) for s in symbols])
    raw = {s: raw[yahoo(s)] for s in symbols if yahoo(s) in raw}
    fx_raw = download(list(FX.values()))
    fx = {cur: fx_raw[t]["Close"] for cur, t in FX.items() if t in fx_raw}

    cache = load_cache()
    find_currencies(list(raw), cache)
    refresh_info(list(raw), cache)
    with open(INFO_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1, sort_keys=True)

    hist, unknown = {}, []
    for s, h in raw.items():
        cur = cache.get(s, {}).get("currency")
        conv = to_pounds(h, cur, fx, s) if cur else None
        if conv is None:
            unknown.append(f"{s} ({cur or 'currency unknown'})")
        else:
            hist[s] = conv

    as_of = max(h.index[-1] for h in hist.values()).date().isoformat() if hist else dt.date.today().isoformat()
    cols = ["ticker", "name", "category", "class", "expense_ratio", "price", "low_52w", "high_52w",
            "ma_200", "return_3m", "return_6m", "return_1y", "return_5y", "volatility", "max_drawdown", "pe", "pe_10y_avg", "yield", "rsi",
            "currency", "quote_currency", "as_of"]
    written, skipped = 0, []
    with open(OUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            s = r["symbol"]
            if s not in hist:
                skipped.append(s)
                continue
            m, info = metrics(hist[s]), cache.get(s, {})
            w.writerow([
                s, info.get("name") or s, r.get("category") or "", r.get("class", ""),
                fmt(info.get("expense_ratio")),
                fmt(m["price"], 4), fmt(m["low_52w"], 4), fmt(m["high_52w"], 4), fmt(m["ma_200"], 4),
                fmt(m["return_3m"], 2), fmt(m["return_6m"], 2), fmt(m["return_1y"], 2),
                fmt(m["return_5y"], 1), fmt(m["volatility"], 1), fmt(m["max_drawdown"], 1),
                fmt(info.get("pe"), 1), r.get("pe_10y_avg", ""),
                fmt(info.get("yield") if info.get("yield") is not None else m["dist_yield"]), fmt(m["rsi"], 0), BASE, info.get("currency", ""), as_of,
            ])
            written += 1
    print(f"Wrote {written} funds to {OUT_FILE} (prices in {BASE}).")
    update_history(hist)
    risk = build_risk(hist, rows, as_of, stand_ins_for(hist, rows, fx))
    if risk:
        risk["yields"] = market_yields(cache)
        risk["macro"], risk["macro_prev"] = market_macro(cache, risk["yields"])
        risk["macro_weekly"] = macro_weekly(risk["dates"])
        # splits are remembered for good, so the site can adjust baskets saved before one
        known = cache.get("_splits", {})
        for sym, lst in SPLITS.items():
            for x in lst:
                if not any(k["date"] == x["date"] for k in known.get(sym, [])):
                    known.setdefault(sym, []).append(x)
                    print(f"Share split found: {sym} on {x['date']}, price ratio {x['ratio']}", flush=True)
        cache["_splits"] = known
        risk["splits"] = known
        with open(INFO_CACHE, "w", encoding="utf-8") as f:   # keep the last good yields
            json.dump(cache, f, indent=1, sort_keys=True)
        with open(RISK_FILE, "w", encoding="utf-8") as f:
            json.dump(risk, f, separators=(",", ":"))
        print(f"{RISK_FILE}: {len(risk['funds'])} funds, {len(risk['dates'])} weeks from {risk['dates'][0]}.")
    if unknown:
        print("Skipped until their currency is known:", ", ".join(unknown))
    missing = [s for s in skipped if s not in {u.split()[0] for u in unknown}]
    if missing:
        print("No price data for:", ", ".join(missing))


if __name__ == "__main__":
    main()
