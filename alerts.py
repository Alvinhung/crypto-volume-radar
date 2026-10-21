#!/usr/bin/env python3
"""Price-level alerts for Binance USDT-M perpetuals (Bybit linear as an optional fallback).

Watches the levels in alerts.txt (see alerts.example.txt) and pushes to Telegram when price
crosses one. One request fetches the whole market, so 5 or 500 levels cost the same.
Symbols prefixed with BB: (e.g. BB:XYZUSDT) are priced from Bybit.

  python3 alerts.py                 run forever
  python3 alerts.py --once          check once, print, exit
"""
import argparse
import os
import sys
import time

import pricealert
import vrcommon as vc

try:
    import context
except Exception:
    context = None

BYBIT = "https://api.bybit.com"


def prices(use_bybit=True, tries=3):
    out = {x["symbol"]: float(x["price"]) for x in vc.get_json(vc.binance_fapi() + "/fapi/v1/ticker/price", tries=tries, timeout=15)}
    if use_bybit:
        try:
            for x in vc.get_json(BYBIT + "/v5/market/tickers?category=linear", tries=tries, timeout=15)["result"]["list"]:
                if x.get("lastPrice"):
                    out["BB:" + x["symbol"]] = float(x["lastPrice"])
        except Exception as e:
            print("  (Bybit prices unavailable, Binance only:", e, ")", file=sys.stderr)
    return out


def main():
    C = vc.section("alerts")
    ap = argparse.ArgumentParser(description="Price-level alerts")
    ap.add_argument("--file", default=os.path.join(vc.HERE, C.get("file", "alerts.txt")))
    ap.add_argument("--state", default=os.path.join(vc.HERE, "alerts.state"))
    ap.add_argument("--every", type=int, default=C.get("every_s", 30))
    ap.add_argument("--band", type=float, default=C.get("rearm_band", 0.01))
    ap.add_argument("--cooldown", type=int, default=C.get("cooldown_s", 3600))
    ap.add_argument("--reset", action="store_true", help="forget fired one-shot alerts")
    ap.add_argument("--no-context", action="store_true", help="don't attach the structure snippet")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--once", action="store_true", help="fetch prices once, show distance to each level, exit")
    A = ap.parse_args()
    if A.no_push:
        os.environ["VR_NO_PUSH"] = "1"
    use_ctx = context and not A.no_context and C.get("context", True)
    tr = pricealert.Tracker(A.state, A.band, A.cooldown)
    if A.reset:
        tr.reset()
        print("fired-alert state cleared.")

    if A.once:
        px = prices(C.get("bybit_fallback", True))
        for a in pricealert.load(A.file):
            p = px.get(a["symbol"])
            print(f"  {a['symbol']:<14}{a['op']} {a['price']:<12g} now {p if p is not None else '?'}"
                  + (f"  ({(a['price']/p-1)*100:+.2f}%)" if p else ""))
        return

    alerts, mtime, fails = [], 0, 0
    print(f"price alerts: every {A.every}s from {A.file}, context {'on' if use_ctx else 'off'}")
    while True:
        try:
            m = os.path.getmtime(A.file)
            if m != mtime:
                mtime, alerts = m, pricealert.load(A.file)
                print(f"[{time.strftime('%H:%M:%S')}] loaded {len(alerts)}, armed {len(tr.live(alerts))}")
        except FileNotFoundError:
            print(f"{A.file} not found", file=sys.stderr)
            time.sleep(A.every)
            continue
        try:
            px = prices(C.get("bybit_fallback", True))
            if fails >= 3:
                vc.telegram(f"✅ price alerts reconnected (down ~{fails * A.every}s)")
            fails = 0
        except Exception as e:
            fails += 1
            print(f"  (price fetch failed x{fails}: {e})", file=sys.stderr)
            if fails == 3:
                vc.telegram("⚠️ price alerts: repeated fetch failures, alerts may be missed")
            time.sleep(A.every)
            continue
        for a in alerts:
            p = px.get(a["symbol"])
            if p is None or not tr.update(a, p):
                continue
            msg = f"{pricealert.arrow(a)} {a['symbol']} {a['price']:g}  now {p:g}"
            if use_ctx:
                try:
                    extra = context.brief(a["symbol"])
                    if extra:
                        msg += "\n" + extra
                except Exception as e:
                    print(f"  (context failed {a['symbol']}: {e})", file=sys.stderr)
            if a["note"]:
                msg += f"\n  {a['note']}"
            print(f"[{time.strftime('%H:%M:%S')}] {msg}")
            vc.telegram(msg)
        vc.heartbeat("alerts", A.every)
        time.sleep(A.every)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
