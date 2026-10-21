#!/usr/bin/env python3
"""Market-structure snippet attached to a price alert when it fires.

Called only when an alert triggers (rarely), so it can afford a few extra requests.
Every step is best-effort: a failure returns whatever was collected so far and never
blocks the alert itself. ~4 requests, 1-2 s.

Note: "overhead supply" here only looks at the last `daily_bars` daily candles, not the
full listing history.

  python3 context.py BTCUSDT ETHUSDT
"""
import collections
import math

import vrcommon as vc

_FUNDING_HOURS = {}


def _get(path):
    return vc.get_json(vc.binance_fapi() + path, tries=2, timeout=8)


def _funding_hours(symbol):
    if not _FUNDING_HOURS:
        try:
            for x in _get("/fapi/v1/fundingInfo"):
                _FUNDING_HOURS[x["symbol"]] = float(x["fundingIntervalHours"])
        except Exception:
            pass
    return _FUNDING_HOURS.get(symbol, 8.0)


def _extreme_days(daily, price):
    """(N-day high, M-day low): how many days back until a higher high / lower low. None = none in window."""
    hi_n = lo_n = None
    for i in range(len(daily) - 2, -1, -1):
        if hi_n is None and float(daily[i][2]) >= price:
            hi_n = len(daily) - 1 - i
        if lo_n is None and float(daily[i][3]) <= price:
            lo_n = len(daily) - 1 - i
        if hi_n is not None and lo_n is not None:
            break
    return hi_n, lo_n


def _overhead(daily, price, band):
    """Volume profile: how many days of average volume sit between price and price*(1+band)."""
    bins = collections.defaultdict(float)
    for c in daily:
        lo, hi, q = float(c[3]), float(c[2]), float(c[7])
        if hi <= lo:
            continue
        for i in range(8):
            p = lo + (hi - lo) * (i + 0.5) / 8
            bins[round(math.log(p / price) / math.log(1 + band))] += q / 8
    vol = sum(v for k, v in bins.items() if k in (0, 1))
    avg = sum(float(c[7]) for c in daily[-7:]) / 7
    return (vol / avg) if avg else None


def brief(symbol):
    """Multi-line string (each line indented) or "" on failure."""
    if symbol.startswith("BB:"):
        return ""
    C = vc.section("context")
    n_days, band = C.get("daily_bars", 120), C.get("overhead_band", 0.02)
    thin, thick = C.get("overhead_thin_days", 2), C.get("overhead_thick_days", 10)
    min_ext = C.get("min_extreme_days", 7)
    lines = []
    try:
        daily = _get(f"/fapi/v1/klines?symbol={symbol}&interval=1d&limit={n_days}")
        price = float(daily[-1][4])
        hi_n, lo_n = _extreme_days(daily, price)
        bits = []
        if hi_n is None:
            bits.append(f"≥{len(daily) - 1}-day high")
        elif hi_n >= min_ext:
            bits.append(f"{hi_n}-day high")
        if lo_n is None:
            bits.append(f"≥{len(daily) - 1}-day low")
        elif lo_n >= min_ext:
            bits.append(f"{lo_n}-day low")
        days = _overhead(daily, price, band)
        if days is not None:
            verdict = "thin" if days < thin else ("moderate" if days < thick else "heavy")
            bits.append(f"next +{band*100:g}% = {days:.1f} days of volume ({verdict}, {n_days}d)")
        if bits:
            lines.append("  " + "  ".join(bits))
    except Exception:
        pass
    try:
        oi = _get(f"/futures/data/openInterestHist?symbol={symbol}&period=4h&limit=20")
        q = [float(x["sumOpenInterest"]) for x in oi]
        s = f"  OI ${float(oi[-1]['sumOpenInterestValue'])/1e6:,.1f}M"
        if len(q) > 7 and q[-7]:
            s += f" (contracts 24h {(q[-1] / q[-7] - 1) * 100:+.2f}%)"
        lines.append(s)
    except Exception:
        pass
    try:
        r = float(_get(f"/fapi/v1/premiumIndex?symbol={symbol}")["lastFundingRate"])
        ann = r * (24 / _funding_hours(symbol)) * 365 * 100
        lines.append(f"  funding {ann:+.1f}%/yr ({ann/365:+.3f}%/day)")
    except Exception:
        pass
    try:
        w = _get(f"/futures/data/topLongShortPositionRatio?symbol={symbol}&period=4h&limit=20")
        wr = [float(x["longShortRatio"]) for x in w]
        s = f"  top-trader L/S (positions) {wr[-1]:.2f}"
        if len(wr) > 18 and wr[-19]:
            s += f" (3d {(wr[-1]/wr[-19]-1)*100:+.1f}%)"
        if wr[-1] <= min(wr) * 1.02:
            s += "  ◀ swing low"
        lines.append(s)
    except Exception:
        pass
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    for s in (sys.argv[1:] or ["BTCUSDT", "ETHUSDT"]):
        print(f"── {s} ──")
        print(brief(s.upper()) or "  (unavailable)")
        print()
