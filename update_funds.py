"""
Builds funds.csv for the screener from a list of tickers.

Input:  tickers.csv  - needs a "Symbol" (or "Ticker") column. A Fidelity ETF
                       screener export works as-is. Optional columns:
                       Category, Class (eq/bond/hy/reit/gold), PE_10y_Avg.
Output: history.csv  - date,ticker,close for every trading day, kept for up to
                       10 years. Saved baskets use it to chart performance
                       from the day they were saved.
        funds.csv    - one row per fund with price, 52-week range, 200-day
                       average, RSI, 5-yr return, volatility, max drawdown,
                       expense ratio, yield and P/E.

Data comes from Yahoo Finance via the free `yfinance` library. It's unofficial
and meant for personal use; for a public or commercial site, swap in a
licensed data provider (see fetch_history / fetch_info).

Run:  pip install yfinance pandas
      python update_funds.py
"""
import csv
import datetime as dt
import math
import os
import sys
import time

import pandas as pd
import yfinance as yf

TICKER_FILE = "tickers.csv"
OUT_FILE = "funds.csv"
HIST_FILE = "history.csv"
HIST_KEEP_DAYS = 3650
BATCH = 50  # tickers per price download


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
        if sym and sym.replace(".", "").replace("-", "").isalnum() and len(sym) <= 10:
            d["symbol"] = sym
            out.append(d)
    return out


def fetch_history(symbols):
    """Five years of daily prices. Returns {symbol: DataFrame[Close, Adj Close]}."""
    result = {}
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i:i + BATCH]
        df = yf.download(chunk, period="5y", interval="1d", auto_adjust=False,
                         group_by="ticker", progress=False, threads=True)
        for s in chunk:
            try:
                sub = df[s] if len(chunk) > 1 else df
                sub = sub[["Close", "Adj Close"]].dropna()
                if len(sub) > 30:
                    result[s] = sub
            except KeyError:
                pass
        time.sleep(1)
    return result


def fetch_info(symbol):
    """Name, category, fees, yield, P/E. Fields vary by fund; all optional."""
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception:
        return {}
    er = info.get("netExpenseRatio")  # already a percent, e.g. 0.08
    y = info.get("yield") or info.get("dividendYield")
    if y is not None and y < 1:  # fraction -> percent
        y *= 100
    return {
        "name": info.get("longName") or info.get("shortName"),
        "category": info.get("category"),
        "expense_ratio": er,
        "yield": y,
        "pe": info.get("trailingPE"),
    }


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
    }


def update_history(hist):
    """Merge the latest closes into history.csv. The first run seeds one year of
    history; later runs add the last few days, so a missed run fills itself in."""
    rows = {}
    if os.path.exists(HIST_FILE):
        with open(HIST_FILE, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[(r["date"], r["ticker"])] = r["close"]
        recent = 10
    else:
        recent = 260
    for s, h in hist.items():
        for d, c in h["Close"].iloc[-recent:].items():
            rows[(d.date().isoformat(), s)] = f"{float(c):.4f}"
    cutoff = (dt.date.today() - dt.timedelta(days=HIST_KEEP_DAYS)).isoformat()
    with open(HIST_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "ticker", "close"])
        for (d, s), c in sorted(rows.items()):
            if d >= cutoff:
                w.writerow([d, s, c])
    print(f"history.csv now holds {len(rows)} prices.")


def fmt(v, d=2):
    return "" if v is None or (isinstance(v, float) and math.isnan(v)) else round(v, d)


def main():
    rows = read_tickers(TICKER_FILE)
    symbols = [r["symbol"] for r in rows]
    print(f"Fetching {len(symbols)} tickers…")
    hist = fetch_history(symbols)
    # label data with the last trading day, not the run date
    today = max(h.index[-1] for h in hist.values()).date().isoformat() if hist else dt.date.today().isoformat()
    cols = ["ticker", "name", "category", "class", "expense_ratio", "price", "low_52w", "high_52w",
            "ma_200", "return_5y", "volatility", "max_drawdown", "pe", "pe_10y_avg", "yield", "rsi", "as_of"]
    written, skipped = 0, []
    with open(OUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            s = r["symbol"]
            if s not in hist:
                skipped.append(s)
                continue
            m = metrics(hist[s])
            info = fetch_info(s)
            w.writerow([
                s,
                info.get("name") or r.get("name") or s,
                r.get("category") or info.get("category") or "",
                r.get("class", ""),
                fmt(info.get("expense_ratio")),
                fmt(m["price"]), fmt(m["low_52w"]), fmt(m["high_52w"]), fmt(m["ma_200"]),
                fmt(m["return_5y"], 1), fmt(m["volatility"], 1), fmt(m["max_drawdown"], 1),
                fmt(info.get("pe"), 1), r.get("pe_10y_avg", ""),
                fmt(info.get("yield")), fmt(m["rsi"], 0), today,
            ])
            written += 1
            time.sleep(0.3)  # be gentle with the data source
    print(f"Wrote {written} funds to {OUT_FILE}.")
    update_history(hist)
    if skipped:
        print("No price data for:", ", ".join(skipped))


if __name__ == "__main__":
    main()
