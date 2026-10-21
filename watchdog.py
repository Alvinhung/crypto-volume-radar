#!/usr/bin/env python3
"""Heartbeat watchdog / dead-man's switch.

Every service writes heartbeats/<name>.json each loop (vrcommon.heartbeat). This script
checks them and pushes a Telegram alert when one is older than `stale_factor` × its
expected interval, and a recovery note when it comes back. Optionally it pings an external
dead-man's-switch URL (e.g. Healthchecks.io) every run, so you also hear about it when the
whole machine is down.

Run it from cron / a systemd timer every few minutes:
  python3 watchdog.py               check once
  python3 watchdog.py --loop 300    check every 300 s
"""
import argparse
import glob
import json
import os
import time
import urllib.request

import vrcommon as vc

STATE = os.path.join(vc.HERE, "watchdog.state")


def check(factor, ignore):
    try:
        st = json.load(open(STATE))
    except Exception:
        st = {}
    msgs, now = [], time.time()
    for path in sorted(glob.glob(os.path.join(vc.heartbeat_dir(), "*.json"))):
        try:
            hb = json.load(open(path))
        except Exception:
            continue
        name = hb.get("name") or os.path.basename(path)[:-5]
        if name in ignore:
            continue
        age, limit = now - hb.get("ts", 0), factor * max(hb.get("interval_s", 60), 30)
        stale = age > limit
        print(f"  {'✗' if stale else '✓'} {name:<16} last beat {age/60:6.1f} min ago (limit {limit/60:.0f} min)")
        if stale and not st.get(name):
            msgs.append(f"🛑 {name} silent for {age/60:.0f} min (expected every {hb.get('interval_s', 0)/60:.0f} min)")
        elif not stale and st.get(name):
            msgs.append(f"✅ {name} is back")
        st[name] = stale
    json.dump(st, open(STATE, "w"))
    return msgs


def main():
    C = vc.section("heartbeat")
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="seconds between checks (0 = once)")
    ap.add_argument("--no-push", action="store_true")
    A = ap.parse_args()
    if A.no_push:
        os.environ["VR_NO_PUSH"] = "1"
    ping = os.getenv("VR_DEADMAN_URL") or C.get("deadman_url")
    while True:
        msgs = check(C.get("stale_factor", 3), set(C.get("ignore", [])))
        if msgs:
            vc.telegram("\n".join(msgs))
        if ping:
            try:
                urllib.request.urlopen(ping, timeout=10)
            except Exception as e:
                print("  (dead-man ping failed:", e, ")")
        if not A.loop:
            return
        time.sleep(A.loop)


if __name__ == "__main__":
    main()
