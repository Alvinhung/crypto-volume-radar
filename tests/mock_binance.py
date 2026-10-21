"""Tiny offline stand-in for the Binance USDT-M endpoints the radar and alerts use.

  python3 tests/mock_binance.py 8799 &
  VR_FAPI_BASE=http://127.0.0.1:8799 python3 radar.py --once --no-push

ETHUSDT's last closed bar is a synthetic high-volume breakout so the example rules fire.
"""
import json
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STEP = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def klines(symbol, interval, limit, end=None, start=None):
    step = STEP.get(interval, 300_000)
    now = min(int(end), int(time.time() * 1000)) if end else int(time.time() * 1000)
    last_open = (now // step) * step - step          # last fully closed bar
    if start is not None:                            # honour startTime like the real API (paging)
        first = max(int(start), last_open - (limit - 1) * step)
        if first > last_open:
            return []
        limit = min(limit, (last_open - first) // step + 1)
    out, p = [], 100.0
    for k in range(limit):
        t = last_open - (limit - 1 - k) * step
        o = p
        c = o * (1 + (0.001 if k % 2 else -0.001))
        qv = 1_000_000.0
        if symbol == "ETHUSDT" and k >= limit - 3:   # breakout on the last 3 bars
            c, qv = o * 1.04, 12_000_000.0
        h, l = max(o, c) * 1.002, min(o, c) * 0.998
        out.append([t, f"{o}", f"{h}", f"{l}", f"{c}", "0", t + step - 1, f"{qv}", 100, "0", "0", "0"])
        p = c
    return out


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        syms = ["BTCUSDT", "ETHUSDT"]
        if u.path == "/fapi/v1/exchangeInfo":
            body = {"symbols": [{"symbol": s, "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"} for s in syms]}
        elif u.path == "/fapi/v1/ticker/24hr":
            body = [{"symbol": s, "quoteVolume": "900000000", "lastPrice": "100", "priceChangePercent": "1.0"} for s in syms]
        elif u.path == "/fapi/v1/ticker/price":
            body = [{"symbol": s, "price": "100"} for s in syms]
        elif u.path == "/fapi/v1/klines":
            body = klines(q["symbol"], q["interval"], int(q.get("limit", 100)), q.get("endTime"), q.get("startTime"))
        elif u.path == "/futures/data/openInterestHist":
            body = [{"sumOpenInterest": str(1000 + i), "sumOpenInterestValue": str(1e8 + i)} for i in range(int(q.get("limit", 4)))]
        elif u.path in ("/futures/data/topLongShortPositionRatio", "/futures/data/topLongShortAccountRatio"):
            body = [{"longShortRatio": "1.5"} for _ in range(int(q.get("limit", 20)))]
        elif u.path == "/fapi/v1/premiumIndex":
            body = {"lastFundingRate": "0.0001"}
        elif u.path == "/fapi/v1/fundingInfo":
            body = []
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8799), H).serve_forever()
