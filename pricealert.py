"""Shared price-alert file format and trigger logic (used by alerts.py and onchain-alert.py).

One alert per line:   SYMBOL  >=|<=  PRICE  [note]
Prefix with * to make it repeatable; otherwise it fires once and is retired.
Everything after # is a comment.
"""
import json
import os
import sys
import time


def load(path, only=None):
    out = []
    for lineno, raw in enumerate(open(path, encoding="utf-8"), 1):
        line = raw.split("#")[0].strip()
        if not line:
            continue
        repeat = line.startswith("*")
        parts = line.lstrip("*").split(None, 3)
        if len(parts) < 3 or parts[1] not in (">=", "<="):
            print(f"  line {lineno}: bad format, skipped: {raw.strip()}", file=sys.stderr)
            continue
        sym = parts[0].upper()
        if only and sym not in only:
            continue
        try:
            px = float(parts[2])
        except ValueError:
            print(f"  line {lineno}: price is not a number, skipped: {raw.strip()}", file=sys.stderr)
            continue
        out.append({"key": f"{sym}{parts[1]}{px:g}", "symbol": sym, "op": parts[1], "price": px,
                    "note": parts[3] if len(parts) > 3 else "", "repeat": repeat})
    return out


class Tracker:
    """Fires on *crossings* only. A level already satisfied at start-up does not fire. After firing,
    price must move `band` away from the level before it re-arms; repeatable alerts also respect
    `cooldown` seconds. One-shot alerts are persisted to `state_path` so restarts don't re-fire them."""

    def __init__(self, state_path, band, cooldown):
        self.state_path, self.band, self.cooldown = state_path, band, cooldown
        self.fired = {}
        if state_path and os.path.exists(state_path):
            try:
                self.fired = json.load(open(state_path))
            except Exception:
                pass
        self.armed, self.last = {}, {}

    def reset(self):
        self.fired = {}
        if self.state_path and os.path.exists(self.state_path):
            os.remove(self.state_path)

    def live(self, alerts):
        return [a for a in alerts if a["key"] not in self.fired or a["repeat"]]

    def update(self, a, px):
        """Return True if alert `a` should fire at price px."""
        k = a["key"]
        if k in self.fired and not a["repeat"]:
            return False
        hit = px >= a["price"] if a["op"] == ">=" else px <= a["price"]
        if k not in self.armed:
            self.armed[k] = not hit
            return False
        if not self.armed[k]:
            back = px < a["price"] * (1 - self.band) if a["op"] == ">=" else px > a["price"] * (1 + self.band)
            if back:
                self.armed[k] = True
            return False
        if not hit or time.time() - self.last.get(k, 0) < self.cooldown:
            return False
        self.armed[k], self.last[k] = False, time.time()
        if not a["repeat"]:
            self.fired[k] = time.strftime("%Y-%m-%dT%H:%M:%S")
            if self.state_path:
                json.dump(self.fired, open(self.state_path, "w"), ensure_ascii=False, indent=1)
        return True


def arrow(a):
    return "📈 above" if a["op"] == ">=" else "📉 below"
