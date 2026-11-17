#!/usr/bin/env python3
"""
Stage 2 - Volume Surge Intraday Strategy (PDF workflow)
=======================================================
PDF ka flow: Scan -> Shortlist (3-4) -> Validate chart -> Risk manage -> Execute/Skip -> Journal

Is script me:
  1. SCAN       : upstox_volume_scanner.scan()  (volume >= 3x avg, liquidity, spread filters)
  2. SHORTLIST  : top 3-4 liquid names
  3. VALIDATE   : 5-min chart par
        - Trend      : price VWAP aur EMA20 ke sahi side par
        - Key levels : Opening Range (pehle 15 min) high/low, previous day high/low
        - Trigger    : OR break + hold (chasing nahi), trigger candle me volume expansion,
                       rejection wick nahi
        - Invalidation: last 3 candles ka low/high -> stop-loss
        - Room       : agla key level (PDH/PDL) kam se kam 1.5R door ho
  4. RISK       : qty = max rupee risk / (entry - stop), capital cap, min reward:risk
  5. JOURNAL    : journal.csv me planned trade log (result/lesson aap baad me bharo)

!! Ye script ORDER NAHI lagata. Sirf signal + plan deta hai. Pehle paper trade karo.
!! Educational use only. Not investment advice.

Run:
  python volume_surge_strategy.py --capital 100000 --risk 500
  python volume_surge_strategy.py --loop 60            # har 60 sec scan (market hours me)
  python volume_surge_strategy.py --ignore-time        # time window ignore (testing)
"""
import argparse
import math
import time as _time
from datetime import datetime
from pathlib import Path

import pandas as pd
from urllib.parse import quote

from upstox_volume_scanner import IST, Upstox, load_universe, resolve, scan

JOURNAL = Path("journal.csv")
JCOLS = ["date", "time", "symbol", "scan_condition", "setup", "side", "entry", "sl",
         "target", "qty", "risk_planned", "result_and_lesson"]


# ---------------------------------------------------------------- data
def intraday_5m(api, key):
    """1-min intraday candles -> 5-min resample (sirf poori ho chuki candles)."""
    d = api.get(f"/historical-candle/intraday/{quote(key, safe='')}/1minute")
    rows = d.get("data", {}).get("candles", [])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "v", "oi"])
    df["ts"] = pd.to_datetime(df["ts"]).dt.tz_convert(IST)
    df = df.set_index("ts").sort_index()
    o = df.resample("5min").agg({"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"}).dropna()
    now = pd.Timestamp.now(tz=IST)
    return o[o.index + pd.Timedelta(minutes=5) <= now]


# ---------------------------------------------------------------- validation
def evaluate(df5, row, side, cfg):
    """Return (plan_dict, None) agar setup valid, warna (None, reason)."""
    s = 1 if side == "long" else -1
    if len(df5) < 4:
        return None, "opening range (pehle 15 min) poori nahi hui"

    orng = df5.iloc[:3]
    orh, orl = orng.h.max(), orng.l.min()
    tp = (df5.h + df5.l + df5.c) / 3
    vwap = (tp * df5.v).cumsum() / df5.v.cumsum()
    ema = df5.c.ewm(span=20, adjust=False).mean()
    last, recent = df5.iloc[-1], df5.iloc[-3:]
    trig = orh if s == 1 else orl

    # 1) Trend & structure
    if s * (last.c - vwap.iloc[-1]) <= 0 or s * (last.c - ema.iloc[-1]) <= 0:
        return None, "trend: price VWAP/EMA20 ke sahi side par nahi"

    # 2) Key level break + hold (confirmed break, chasing nahi)
    if not (s * (recent.c - trig) > 0).any() or s * (last.c - trig) <= 0:
        return None, "Opening Range break hold nahi kar raha"
    ext = s * (last.c - trig) / trig * 100
    if ext > cfg.max_ext_pct:
        return None, f"move already {ext:.2f}% extended (chasing)"

    # 3) Volume context: trigger candle me volume expansion, rejection wick nahi
    base = df5.v.iloc[-11:-1].mean()
    if base > 0 and last.v < cfg.candle_vol_mult * base:
        return None, "trigger candle par volume expansion nahi"
    rng = last.h - last.l
    if rng <= 0 or s * (last.c - last.o) <= 0:
        return None, "trigger candle trade direction me close nahi hui"
    wick = (last.h - max(last.o, last.c)) if s == 1 else (min(last.o, last.c) - last.l)
    if wick / rng > cfg.max_wick:
        return None, "rejection wick (move fail ho raha ho sakta hai)"

    # 4) Invalidation -> stop-loss
    entry = float(row.ltp)
    stop = recent.l.min() * (1 - cfg.buffer) if s == 1 else recent.h.max() * (1 + cfg.buffer)
    risk = s * (entry - stop)
    if risk <= 0:
        return None, "price stop ke paar - setup invalid"
    risk_pct = risk / entry * 100
    if risk_pct > cfg.max_stop_pct:
        return None, f"stop bahut door ({risk_pct:.2f}%)"
    if risk_pct < cfg.min_stop_pct:
        return None, f"stop bahut tight ({risk_pct:.2f}%) - noise me hit hoga"

    # 5) Room to run: agla key level kam se kam min_room_r * risk door ho
    level = row.pdh if s == 1 else row.pdl
    if s * (level - entry) > 0.001 * entry and s * (level - entry) < cfg.min_room_r * risk:
        return None, f"agla level ({'PDH' if s == 1 else 'PDL'} {level:.2f}) bahut paas"

    # 6) Position sizing
    qty = min(math.floor(cfg.risk / risk), math.floor(cfg.capital * cfg.leverage / entry))
    if qty < 1:
        return None, "position size < 1 share (risk/capital ke hisaab se)"

    target = entry + s * cfg.rr * risk
    return dict(side=side.upper(), entry=round(entry, 2), stop=round(stop, 2), target=round(target, 2),
                qty=qty, risk_rs=round(qty * risk, 0), risk_pct=round(risk_pct, 2),
                trigger=f"OR{'H' if s == 1 else 'L'} {trig:.2f}", rr=cfg.rr), None


# ---------------------------------------------------------------- journal
def journal_today():
    if not JOURNAL.exists():
        return pd.DataFrame(columns=JCOLS)
    df = pd.read_csv(JOURNAL)
    return df[df["date"] == str(datetime.now().date())]


def log_trade(sym, plan, vol_x, cfg):
    now = datetime.now()
    row = dict(date=now.date(), time=now.strftime("%H:%M:%S"), symbol=sym,
               scan_condition=f"vol>={cfg.mult}x avg{cfg.lookback}d, 5m",
               setup=plan["trigger"], side=plan["side"], entry=plan["entry"], sl=plan["stop"],
               target=plan["target"], qty=plan["qty"], risk_planned=plan["risk_rs"], result_and_lesson="")
    pd.DataFrame([row], columns=JCOLS).to_csv(JOURNAL, mode="a", header=not JOURNAL.exists(), index=False)


# ---------------------------------------------------------------- main flow
def run_once(api, cfg, sym_keys):
    now = datetime.now()
    t = now.strftime("%H:%M")
    if not cfg.ignore_time and not (cfg.start <= t <= cfg.end):
        print(f"[{t}] entry window {cfg.start}-{cfg.end} ke bahar. (--ignore-time se test kar sakte ho)")
        return

    done = journal_today()
    if len(done) >= cfg.max_trades:
        print(f"Daily max {cfg.max_trades} trades plan ho chuke - aaj bas. (No-trade bhi valid decision hai)")
        return

    # SCAN + SHORTLIST
    cand = scan(api, sym_keys, cfg.mult, cfg.lookback, cfg.min_value_cr, cfg.min_price, cfg.max_spread)
    print(f"\n[{now:%H:%M:%S}] SCAN: {len(cand)} stocks volume>={cfg.mult}x filter pass")
    if cand.empty:
        return
    short = cand.head(cfg.shortlist)
    print("SHORTLIST:", ", ".join(f"{r.symbol}({r.vol_x:.1f}x)" for r in short.itertuples()))

    # VALIDATE + RISK
    already = set(zip(done["symbol"], done["side"])) if len(done) else set()
    for r in short.itertuples():
        df5 = intraday_5m(api, r.key)
        if df5.empty:
            print(f"  SKIP {r.symbol}: intraday data nahi mila")
            continue
        taken = False
        reasons = []
        for side in cfg.sides:
            plan, why = evaluate(df5, r, side, cfg)
            if plan is None:
                reasons.append(f"{side}: {why}")
                continue
            if (r.symbol, plan["side"]) in already:
                reasons.append(f"{side}: aaj already log ho chuka")
                continue
            print(f"  ✅ {r.symbol} {plan['side']}  entry~{plan['entry']}  SL {plan['stop']}  "
                  f"TGT {plan['target']}  qty {plan['qty']}  risk ₹{plan['risk_rs']:.0f} "
                  f"({plan['risk_pct']}%)  trigger {plan['trigger']}  R:R 1:{plan['rr']}")
            log_trade(r.symbol, plan, r.vol_x, cfg)
            already.add((r.symbol, plan["side"]))
            taken = True
            if len(journal_today()) >= cfg.max_trades:
                return
            break
        if not taken:
            print(f"  ⏭  SKIP {r.symbol}: " + " | ".join(reasons))


def parse():
    ap = argparse.ArgumentParser(description="Volume surge intraday strategy (signals only)")
    ap.add_argument("--universe")
    ap.add_argument("--sides", default="both", choices=["long", "short", "both"])
    # scan
    ap.add_argument("--mult", type=float, default=3.0)
    ap.add_argument("--lookback", type=int, default=20)
    ap.add_argument("--min-value-cr", type=float, default=10.0)
    ap.add_argument("--min-price", type=float, default=50.0)
    ap.add_argument("--max-spread", type=float, default=0.15)
    ap.add_argument("--shortlist", type=int, default=4, help="3-4 names (PDF)")
    # validation
    ap.add_argument("--candle-vol-mult", type=float, default=1.2, help="trigger candle vol vs last 10 candles avg")
    ap.add_argument("--max-wick", type=float, default=0.4, help="max wick/range ratio")
    ap.add_argument("--max-ext-pct", type=float, default=1.0, help="OR break se max extension %%")
    ap.add_argument("--buffer", type=float, default=0.0005, help="stop buffer (fraction)")
    ap.add_argument("--min-stop-pct", type=float, default=0.15)
    ap.add_argument("--max-stop-pct", type=float, default=1.5)
    ap.add_argument("--rr", type=float, default=2.0, help="target reward:risk")
    ap.add_argument("--min-room-r", type=float, default=1.5, help="next level kam se kam itne R door")
    # risk
    ap.add_argument("--capital", type=float, default=100000)
    ap.add_argument("--risk", type=float, default=500, help="max rupee risk per trade")
    ap.add_argument("--leverage", type=float, default=1.0, help="conservative default 1x")
    ap.add_argument("--max-trades", type=int, default=3, help="daily max trade plans")
    # timing
    ap.add_argument("--start", default="09:30")
    ap.add_argument("--end", default="14:30")
    ap.add_argument("--ignore-time", action="store_true")
    ap.add_argument("--loop", type=int, default=0, help="har N second me repeat (0 = ek baar)")
    a = ap.parse_args()
    a.sides = ["long", "short"] if a.sides == "both" else [a.sides]
    return a


def main():
    cfg = parse()
    api = Upstox()
    sym_keys = resolve(load_universe(cfg.universe))
    while True:
        try:
            run_once(api, cfg, sym_keys)
        except Exception as e:  # network/API error pe loop band na ho
            print("[error]", e)
        if not cfg.loop:
            break
        _time.sleep(cfg.loop)


if __name__ == "__main__":
    main()
