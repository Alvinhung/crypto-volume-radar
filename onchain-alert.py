#!/usr/bin/env python3
"""On-chain price alerts for DEX pools via GeckoTerminal (for tokens not listed on Binance).

One process watches every pool in config `onchain.pools`:

    {"name": "TOKEN", "network": "solana", "pool": "<pool address>", "every_s": 120}

Levels live in onchain-alerts.txt using the same format as alerts.txt, with the pool's
`name` as the symbol:

    TOKEN  <=  0.1234   first entry
    *TOKEN <=  0.1000   stop

Requests are serialized and spaced by `min_request_gap_s` so all pools together stay well
inside GeckoTerminal's free limit (~30 req/min, enforced over short windows).

  python3 onchain-alert.py                 run forever
  python3 onchain-alert.py --once          price every pool once and exit
  python3 onchain-alert.py --only TOKEN    watch a single pool
"""
import argparse
import os
import sys
import time

import pricealert
import vrcommon as vc

GECKO = "https://api.geckoterminal.com/api/v2"


def pool_price(network, pool):
    a = vc.get_json(f"{GECKO}/networks/{network}/pools/{pool}", tries=3, timeout=20,
                    rate_limit_backoff=15)["data"]["attributes"]
    return float(a["base_token_price_usd"]), a


def main():
    C = vc.section("onchain")
    ap = argparse.ArgumentParser(description="On-chain (GeckoTerminal) price alerts")
    ap.add_argument("--file", default=os.path.join(vc.HERE, C.get("file", "onchain-alerts.txt")))
    ap.add_argument("--state", default=os.path.join(vc.HERE, "onchain-alerts.state"))
    ap.add_argument("--only", nargs="*", help="pool names to watch (default: all in config)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    A = ap.parse_args()
    if A.no_push:
        os.environ["VR_NO_PUSH"] = "1"

    pools = [p for p in C.get("pools", []) if p.get("enabled", True)
             and (not A.only or p["name"].upper() in {x.upper() for x in A.only})]
    if not pools:
        sys.exit("no pools configured (config: onchain.pools)")
    gap = C.get("min_request_gap_s", 2.5)
    default_every = C.get("every_s", 120)
    tr = pricealert.Tracker(A.state, C.get("rearm_band", 0.01), C.get("cooldown_s", 3600))

    if A.once:
        for p in pools:
            try:
                px, attr = pool_price(p.get("network", "solana"), p["pool"])
                print(f"  {p['name']:<10} {p.get('network','solana'):<10} ${px:.6g}  liquidity ${float(attr.get('reserve_in_usd') or 0):,.0f}")
            except Exception as e:
                print(f"  {p['name']:<10} failed: {e}")
            time.sleep(gap)
        return

    names = {p["name"].upper() for p in pools}
    nxt = {p["name"]: time.time() + i * gap for i, p in enumerate(pools)}
    fails = {p["name"]: 0 for p in pools}
    alerts, mtime = [], 0
    print(f"on-chain alerts: {len(pools)} pools, levels from {A.file}")
    while True:
        try:
            m = os.path.getmtime(A.file)
            if m != mtime:
                mtime, alerts = m, pricealert.load(A.file, names)
                print(f"[{time.strftime('%H:%M:%S')}] loaded {len(alerts)} levels")
        except FileNotFoundError:
            alerts = []
        p = min(pools, key=lambda x: nxt[x["name"]])
        time.sleep(max(0, nxt[p["name"]] - time.time()))
        name, every = p["name"], p.get("every_s", default_every)
        nxt[name] = time.time() + every
        try:
            px, attr = pool_price(p.get("network", "solana"), p["pool"])
            if fails[name] >= 3:
                vc.telegram(f"✅ {name} on-chain feed recovered")
            fails[name] = 0
        except Exception as e:
            fails[name] += 1
            print(f"  ({name} price failed x{fails[name]}: {e})", file=sys.stderr)
            if fails[name] == 3:
                vc.telegram(f"⚠️ {name} on-chain price failing repeatedly, check manually")
            continue
        for a in alerts:
            if a["symbol"] != name.upper() or not tr.update(a, px):
                continue
            msg = f"{pricealert.arrow(a)} {name} {a['price']:g}  now {px:.6g}"
            if attr.get("reserve_in_usd"):
                msg += f"\n  pool liquidity ${float(attr['reserve_in_usd']):,.0f}"
            if a["note"]:
                msg += f"\n  {a['note']}"
            print(f"[{time.strftime('%H:%M:%S')}] {msg}")
            vc.telegram(msg)
        vc.heartbeat("onchain-alert", max(x.get("every_s", default_every) for x in pools))
        time.sleep(gap)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
