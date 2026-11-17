#!/usr/bin/env python3
"""
Stage 1 - Upstox Volume Surge Scanner
=====================================
Chartink ke "Volume more than 3x average volume" scan ka Upstox API version.

Logic:
  vol_x = aaj ka ab tak ka volume / pichhle N (default 20) trading days ka average daily volume
  Filter: vol_x >= 3, liquidity (avg traded value), price, bid-ask spread

Setup:
  pip install requests pandas
  export UPSTOX_ACCESS_TOKEN="your_token"     (Windows: set UPSTOX_ACCESS_TOKEN=...)
  python upstox_volume_scanner.py
  python upstox_volume_scanner.py --mult 3 --universe my_symbols.txt --csv scan.csv

Token: Upstox 'Analytics Token' use karo (1 saal valid, free, read-only, static IP nahi chahiye).
  Developer Apps > Analytics tab > Generate Token. Roz login ki zarurat nahi.
Ye sirf educational tool hai, buy/sell advice nahi.
"""
import argparse
import gzip
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests

BASE = "https://api.upstox.com/v2"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
IST = "Asia/Kolkata"
CACHE = Path("cache")
CACHE.mkdir(exist_ok=True)

# Default universe: bade liquid NSE stocks. Apna list dene ke liye --universe file.txt (ek line = ek symbol)
DEFAULT_UNIVERSE = """
RELIANCE TCS HDFCBANK INFY ICICIBANK HINDUNILVR ITC SBIN BHARTIARTL KOTAKBANK LT AXISBANK
ASIANPAINT MARUTI SUNPHARMA TITAN BAJFINANCE ULTRACEMCO NESTLEIND WIPRO HCLTECH M&M NTPC
POWERGRID TMPV TMCV TATASTEEL JSWSTEEL ADANIENT ADANIPORTS ONGC COALINDIA TECHM INDUSINDBK
BAJAJFINSV DRREDDY CIPLA GRASIM HINDALCO EICHERMOT BPCL HEROMOTOCO BRITANNIA APOLLOHOSP
DIVISLAB SBILIFE HDFCLIFE TRENT BEL SHRIRAMFIN JIOFIN
""".split()


class Upstox:
    def __init__(self, token=None):
        token = token or os.getenv("UPSTOX_ACCESS_TOKEN")
        if not token:
            sys.exit("UPSTOX_ACCESS_TOKEN set karo (environment variable).")
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json", "Authorization": f"Bearer {token}"})

    def get(self, path, params=None, retries=4):
        for i in range(retries):
            r = self.s.get(BASE + path, params=params, timeout=25)
            if r.status_code == 429:  # rate limit
                time.sleep(1.5 * (i + 1))
                continue
            if r.status_code == 401:
                sys.exit("401 Unauthorized: token galat/expire/revoke hai. Developer Apps > Analytics tab se naya Analytics Token banao.")
            r.raise_for_status()
            return r.json()
        raise RuntimeError("Rate limit: baar-baar 429 aaya.")


def load_universe(path=None):
    if not path:
        return DEFAULT_UNIVERSE
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.strip().upper()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def load_instruments():
    """NSE equity: trading_symbol -> instrument_key (roz ek baar download, cache me)."""
    f = CACHE / f"NSE_EQ_{date.today():%Y%m%d}.json"
    if f.exists():
        return json.loads(f.read_text())
    r = requests.get(INSTRUMENTS_URL, timeout=90)
    r.raise_for_status()
    data = json.loads(gzip.decompress(r.content))
    m = {i["trading_symbol"]: i["instrument_key"]
         for i in data if i.get("segment") == "NSE_EQ" and i.get("instrument_type") == "EQ"}
    f.write_text(json.dumps(m))
    return m


def resolve(symbols):
    inst = load_instruments()
    found, missing = {}, []
    for s in symbols:
        (found.__setitem__(s, inst[s]) if s in inst else missing.append(s))
    if missing:
        print(f"[warn] ye symbols NSE_EQ me nahi mile (rename/delist?): {', '.join(missing)}")
    return found


def fetch_daily_stats(api, key, lookback):
    to, frm = date.today(), date.today() - timedelta(days=int(lookback * 2.2) + 5)
    d = api.get(f"/historical-candle/{quote(key, safe='')}/day/{to}/{frm}")
    rows = d.get("data", {}).get("candles", [])
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "v", "oi"])
    df["d"] = pd.to_datetime(df["ts"]).dt.date
    df = df[df["d"] < date.today()].sort_values("d").tail(lookback)  # aaj ki candle hatao
    if len(df) < max(10, lookback // 2):
        return None
    last = df.iloc[-1]
    return {
        "avg_vol": float(df["v"].mean()),
        "avg_val": float((df["c"] * df["v"]).mean()),
        "pdh": float(last["h"]), "pdl": float(last["l"]), "pdc": float(last["c"]),
    }


def get_daily_map(api, keys, lookback):
    """Daily stats cache karta hai taaki loop me API calls na barbaad hon."""
    f = CACHE / f"daily_{date.today()}_{lookback}.json"
    cache = json.loads(f.read_text()) if f.exists() else {}
    for k in keys:
        if k not in cache:
            cache[k] = fetch_daily_stats(api, k, lookback)
            time.sleep(0.12)
    f.write_text(json.dumps(cache))
    return cache


def get_quotes(api, keys):
    out = {}
    keys = list(keys)
    for i in range(0, len(keys), 100):
        d = api.get("/market-quote/quotes", {"instrument_key": ",".join(keys[i:i + 100])})
        for v in d.get("data", {}).values():
            out[v["instrument_token"]] = v
    return out


def scan(api, sym_keys, mult=3.0, lookback=20, min_value_cr=10.0, min_price=50.0, max_spread_pct=0.15):
    keys = list(sym_keys.values())
    daily = get_daily_map(api, keys, lookback)
    quotes = get_quotes(api, keys)
    rows = []
    for sym, key in sym_keys.items():
        st, q = daily.get(key), quotes.get(key)
        if not st or not q:
            continue
        ltp, vol = q.get("last_price") or 0, q.get("volume") or 0
        if not ltp or st["avg_vol"] <= 0:
            continue
        depth = q.get("depth") or {}
        bid = ((depth.get("buy") or [{}])[0]).get("price") or 0
        ask = ((depth.get("sell") or [{}])[0]).get("price") or 0
        spread = (ask - bid) / ltp * 100 if bid and ask else float("nan")
        rows.append(dict(symbol=sym, key=key, ltp=ltp, chg_pct=(ltp / st["pdc"] - 1) * 100,
                         volume=vol, avg_vol=st["avg_vol"], vol_x=vol / st["avg_vol"],
                         avg_val_cr=st["avg_val"] / 1e7, spread_pct=spread,
                         pdh=st["pdh"], pdl=st["pdl"]))
    df = pd.DataFrame(rows)
    if df.empty:
        print(f"[info] 0/{len(sym_keys)} stocks ka data mila (market band / token / data issue?)")
        return df
    top = df.sort_values('vol_x', ascending=False).head(3)
    print(f"[info] data mila: {len(df)}/{len(sym_keys)} stocks | sabse high vol_x: " +
          ", ".join(f"{r.symbol} {r.vol_x:.2f}x" for r in top.itertuples()))
    ok = ((df.vol_x >= mult) & (df.avg_val_cr >= min_value_cr) & (df.ltp >= min_price)
          & (df.spread_pct.isna() | (df.spread_pct <= max_spread_pct)))
    return df[ok].sort_values("vol_x", ascending=False).reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser(description="Upstox volume surge scanner")
    ap.add_argument("--universe", help="symbols file (ek line = ek NSE symbol)")
    ap.add_argument("--mult", type=float, default=3.0, help="volume multiple (default 3)")
    ap.add_argument("--lookback", type=int, default=20, help="average volume days (default 20)")
    ap.add_argument("--min-value-cr", type=float, default=10.0, help="min avg daily traded value in crore")
    ap.add_argument("--min-price", type=float, default=50.0)
    ap.add_argument("--max-spread", type=float, default=0.15, help="max bid-ask spread %%")
    ap.add_argument("--csv", help="results CSV me save karo")
    a = ap.parse_args()

    api = Upstox()
    sym_keys = resolve(load_universe(a.universe))
    res = scan(api, sym_keys, a.mult, a.lookback, a.min_value_cr, a.min_price, a.max_spread)
    if res.empty:
        print("Koi stock criteria pe nahi mila (market band ho ya volume surge nahi hai).")
        return
    show = res.drop(columns=["key"]).round(2)
    print(show.to_string(index=False))
    print("\nYe sirf WATCHLIST hai - entry signal nahi. Chart validate karne ke liye strategy script chalao.")
    if a.csv:
        show.to_csv(a.csv, index=False)


if __name__ == "__main__":
    main()
