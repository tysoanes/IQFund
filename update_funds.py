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
        info_cache.json - fund details and each fund's quote currency.

London ETFs are quoted in pence (GBp), pounds (GBP) or dollars (USD) depending
on the share class. The script looks up each fund's quote currency from Yahoo
and converts everything to pounds, so the site always compares like with like.

Data comes from Yahoo Finance via the free `yfinance` library. It's unofficial
and meant for personal use; for a public or commercial site, swap in a
licensed data provider.

Run:  pip install yfinance pandas
      python update_funds.py
"""
import csv
import datetime as dt
import json
import math
import os
import sys
import threading
import time

import pandas as pd
import yfinance as yf

TICKER_FILE = "tickers.csv"
OUT_FILE = "funds.csv"
HIST_FILE = "history.csv"
HIST_HEADER = ["date", "ticker", "close_gbp"]
HIST_KEEP_DAYS = 3650
INFO_CACHE = "info_cache.json"
EXCHANGE_SUFFIX = ".L"           # London Stock Exchange on Yahoo
BASE = "GBP"                     # every price in the output is in pounds
INFO_MAX_AGE_DAYS = 7            # refresh fund details about once a week
CALL_TIMEOUT = 15                # seconds to wait for any single Yahoo lookup
CURRENCY_BUDGET = 240            # seconds to spend finding quote currencies
INFO_BUDGET = 240                # seconds to spend refreshing fund details
BATCH = 50                       # tickers per price download
FX = {"USD": "GBP=X", "EUR": "EURGBP=X"}   # Yahoo: pounds per 1 USD / 1 EUR


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


def download(yahoo_symbols, period="5y"):
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


def to_pounds(h, currency, fx):
    """Convert a Close/Adj Close frame from its quote currency to pounds."""
    h = h.apply(fix_unit_glitches)
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
        # total return (incl. dividends, in pounds) had you bought 3, 6 or 12 months ago
        **{f"return_{k}": back_return(adj, months) for k, months in (("3m", 3), ("6m", 6), ("1y", 12))},
    }


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
    cutoff = (dt.date.today() - dt.timedelta(days=HIST_KEEP_DAYS)).isoformat()
    with open(HIST_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HIST_HEADER)
        for (d, s), c in sorted(rows.items()):
            if d >= cutoff:
                w.writerow([d, s, c])
    print(f"history.csv now holds {len(rows)} prices.")


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
        conv = to_pounds(h, cur, fx) if cur else None
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
                fmt(info.get("yield")), fmt(m["rsi"], 0), BASE, info.get("currency", ""), as_of,
            ])
            written += 1
    print(f"Wrote {written} funds to {OUT_FILE} (prices in {BASE}).")
    update_history(hist)
    if unknown:
        print("Skipped until their currency is known:", ", ".join(unknown))
    missing = [s for s in skipped if s not in {u.split()[0] for u in unknown}]
    if missing:
        print("No price data for:", ", ".join(missing))


if __name__ == "__main__":
    main()
