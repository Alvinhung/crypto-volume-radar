#!/usr/bin/env python3
"""Volume/price anomaly radar for Binance USDT-M perpetual futures.

Every time a kline closes, it fetches the latest bars for every liquid USDT perpetual,
computes a set of metrics (volume ratio, % change, run-up from the recent low, upper
wick, ...) and evaluates the signal rules defined in your config file. Matches are
de-duplicated, printed, appended to a CSV log and (optionally) pushed to Telegram.

The rules themselves are NOT hard-coded: write them in config.local.json using a small
condition DSL, or as Python plugins (see plugins/example_rules.py).

  live:        python3 radar.py
  one pass:    python3 radar.py --once --no-push
  replay day:  python3 radar.py --replay BTCUSDT --date 2026-01-15
  list metrics python3 radar.py --list-metrics

Standard library only.
"""
import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import importlib
import math
import os
import statistics
import sys
import time

import vrcommon as vc

INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000}

METRICS_DOC = {
    "vr": "volume ratio: this bar's quote volume / median of the previous `base_n` bars",
    "bar_usd": "this bar's quote volume (USD)",
    "chg1": "this bar's close/open - 1",
    "chgN": "close / close `chg_n` bars ago - 1 (also exposed as chg5 when chg_n=5)",
    "abs_chgN": "abs(chgN)",
    "range": "(high - low) / low of this bar",
    "upper_wick": "upper wick as a fraction of the bar's range (0..1)",
    "run": "high / lowest low of the last `low_n` bars - 1",
    "run_to_peak": "recent peak (last `peak_n` bars) / lowest low before that peak - 1",
    "from_peak": "close / recent peak - 1 (negative = below the peak)",
    "new_high": "true if this bar's high >= highest high of the previous `high_n` bars",
    "green / red": "close > open / close < open",
    "price": "close price",
}


# ---------------------------------------------------------------- data
def get(path, params=None):
    return vc.get_json(vc.binance_fapi() + path, params, tries=4, backoff=1.0, rate_limit_backoff=5.0, timeout=15)


def klines(symbol, interval, limit=100, start=None, end=None):
    p = {"symbol": symbol, "interval": interval, "limit": limit}
    if start is not None:
        p["startTime"] = start          # startTime=0 (full history) must still be sent
    if end is not None:
        p["endTime"] = end
    return [dict(t=k[0], o=float(k[1]), h=float(k[2]), l=float(k[3]), c=float(k[4]), qv=float(k[7]))
            for k in get("/fapi/v1/klines", p)]


# ---------------------------------------------------------------- rules
class Rules:
    """Loads declarative rules + optional Python plugins from the `radar` config section."""

    def __init__(self, cfg):
        self.cfg = cfg
        w = cfg.get("windows", {})
        self.base_n = w.get("base_n", 20)
        self.chg_n = w.get("chg_n", 3)
        self.low_n = w.get("low_n", 48)
        self.peak_n = w.get("peak_n", 12)
        self.high_n = w.get("high_n", 20)
        self.min_bars = max(self.base_n, self.low_n, self.chg_n, self.high_n, self.peak_n)
        self.min_bar_usd = cfg.get("min_bar_usd", 0)
        self.rules = cfg.get("rules", [])
        self.meta = {r["code"]: r for r in self.rules}
        self.suppress = cfg.get("suppress", [])
        self.plugins = []
        for name in cfg.get("plugins", []):
            mod = importlib.import_module(name)
            self.plugins.append(mod)
            for code, meta in getattr(mod, "SIGNALS", {}).items():
                self.meta.setdefault(code, dict(meta, code=code))

    def metrics(self, bars, i):
        b = bars[i]
        base = statistics.median(x["qv"] for x in bars[i - self.base_n:i]) or 1e-9
        rng = b["h"] - b["l"] or 1e-12
        low = min(x["l"] for x in bars[i - self.low_n:i + 1])
        peak_i = max(range(i - self.peak_n, i + 1), key=lambda j: bars[j]["h"])
        peak = bars[peak_i]["h"]
        chg = b["c"] / bars[i - self.chg_n]["c"] - 1
        m = dict(
            price=b["c"], high=b["h"], bar_usd=b["qv"], qv=b["qv"],
            vr=b["qv"] / base,
            chg1=b["c"] / b["o"] - 1,
            chgN=chg, abs_chgN=abs(chg),
            range=rng / b["l"] if b["l"] else 0,
            upper_wick=(b["h"] - max(b["o"], b["c"])) / rng,
            run=b["h"] / low - 1,
            peak=peak,
            run_to_peak=peak / min(x["l"] for x in bars[i - self.low_n:peak_i + 1]) - 1,
            from_peak=b["c"] / peak - 1,
            new_high=b["h"] >= max(x["h"] for x in bars[i - self.high_n:i]),
            green=b["c"] > b["o"], red=b["c"] < b["o"],
        )
        m[f"chg{self.chg_n}"], m[f"abs_chg{self.chg_n}"] = chg, abs(chg)
        return m

    def detect(self, bars, i):
        """Signals fired by closed bar i -> [(code, metrics)]"""
        if i < self.min_bars:
            return []
        m = self.metrics(bars, i)
        if m["bar_usd"] < self.min_bar_usd:
            return []
        out = [r["code"] for r in self.rules if r.get("enabled", True) and vc.all_true(r.get("when", []), m)]
        for p in self.plugins:
            for code in p.detect(m, bars, i, self.cfg) or []:
                if code not in out:
                    out.append(code)
        for keep, drop in self.suppress:
            if keep in out and drop in out:
                out.remove(drop)
        return [(s, m) for s in out]

    def info(self, code):
        r = self.meta.get(code, {})
        return r.get("icon", "•"), r.get("title", code), r.get("desc", "")


class Cooldown:
    """Same symbol + same signal is not repeated within N bars, unless the rule sets
    refire_on_new_high and the new signal comes with a higher high."""

    def __init__(self, bars, rules):
        self.bars, self.rules, self.last, self.high = bars, rules, {}, {}

    def ok(self, key, idx, sig=None, high=None):
        recent = key in self.last and idx - self.last[key] < self.bars
        refire = self.rules.meta.get(sig, {}).get("refire_on_new_high", False)
        if recent and not (refire and high and high > self.high.get(key, 0)):
            return False
        self.last[key], self.high[key] = idx, high or 0
        return True


# ---------------------------------------------------------------- optional risk/reward helper
def full_history_4h(symbol, end_ms):
    out, cur = [], 0
    while True:
        chunk = klines(symbol, "4h", 1500, cur, end_ms)
        if not chunk:
            break
        out += chunk
        cur = chunk[-1]["t"] + 14_400_000
        if len(chunk) < 1500 or cur >= end_ms:
            break
    return out


def atr(bars, n=14):
    tr = [max(b["h"] - b["l"], abs(b["h"] - a["c"]), abs(b["l"] - a["c"])) for a, b in zip(bars, bars[1:])]
    return sum(tr[-n:]) / min(n, len(tr)) if tr else 0


def rr_long(symbol, entry, asof_ms, recent, P):
    """Stop / target / R:R for a long at `entry`. Every parameter comes from radar.rr in config.

    stop   = the further of (recent swing low * (1 - stop_buffer)) and (entry - stop_atr_mult * ATR(1h))
    target = the nearest of: entry + target_atr_cap * ATR, the N-hour high, and the first
             full-history volume-profile wall between min_target and max_target above entry.
    """
    h1 = klines(symbol, "1h", P.get("hi_lookback_h", 72) + 2, end=asof_ms)
    a = atr(h1, P.get("atr_len", 14))
    base_low = min(b["l"] for b in recent[-P.get("stop_lookback_bars", 12):]) * (1 - P.get("stop_buffer", 0.0))
    atr_stop = entry - P.get("stop_atr_mult", 2.0) * a
    stop = min(base_low, atr_stop)
    stop_why = "swing low" if base_low <= atr_stop else f"{P.get('stop_atr_mult', 2.0):g} ATR"
    lo_lim, hi_lim = entry * (1 + P.get("min_target", 0.02)), entry * (1 + P.get("max_target", 0.5))
    cands = []
    if P.get("target_atr_cap"):
        cands.append((entry + P["target_atr_cap"] * a, f"{P['target_atr_cap']:g} ATR cap"))
    hi = max((b["h"] for b in h1[-P.get("hi_lookback_h", 72):]), default=0)
    if lo_lim <= hi <= hi_lim:
        cands.append((hi, f"{P.get('hi_lookback_h', 72)}h high"))
    if P.get("volume_wall", True):
        hist = full_history_4h(symbol, asof_ms)
        w = math.log(1 + P.get("bucket", 0.02))
        vol, total = {}, 0.0
        for b in hist:
            if b["l"] <= 0 or b["qv"] <= 0:
                continue
            l, h = math.log(b["l"]), math.log(max(b["h"], b["l"]))
            for k in range(8):
                x = l + (h - l) * (k + 0.5) / 8
                vol[int(x // w)] = vol.get(int(x // w), 0) + b["qv"] / 8
            total += b["qv"]
        for k in sorted(vol):
            edge = math.exp(k * w)
            if edge < lo_lim or edge > hi_lim:
                continue
            share = vol[k] / total
            if share >= P.get("wall_share", 0.03) and vol[k] >= vol.get(k - 1, 0) and vol[k] >= vol.get(k + 1, 0):
                cands.append((edge, f"volume wall {share*100:.1f}%"))
                break
    if cands:
        (target, tgt_why), open_ended = min(cands), False
    else:
        target, tgt_why, open_ended = hi_lim, "no resistance found", True
    risk, reward = entry - stop, target - entry
    return dict(rr=risk > 0 and reward / risk or 0, stop=stop, target=target, risk=risk / entry,
                reward=reward / entry, stop_why=stop_why, tgt_why=tgt_why, open_ended=open_ended, atr=a / entry)


def rr_text(r):
    ge = "≥" if r["open_ended"] else ""
    return (f"R:R {ge}{r['rr']:.1f} (sizing aid only, not a recommendation)\n"
            f"  stop   {fmt_price(r['stop'])} (−{r['risk']*100:.1f}%, {r['stop_why']}; 1h ATR {r['atr']*100:.1f}%)\n"
            f"  target {fmt_price(r['target'])} (+{r['reward']*100:.1f}%, {r['tgt_why']})")


# ---------------------------------------------------------------- output
def fmt_price(p):
    return f"{p:.6g}"


def line(rules, symbol, sig, m, t, tz):
    icon, name, _ = rules.info(sig)
    ts = dt.datetime.fromtimestamp(t / 1000, tz).strftime("%m-%d %H:%M")
    return (f"{ts} {icon} {name:<8} {symbol:<14} px {fmt_price(m['price']):<10} "
            f"vol× {m['vr']:5.1f}  bar {m['chg1']*100:+6.1f}%  {rules.chg_n}bars {m['chgN']*100:+6.1f}%  "
            f"run {m['run']*100:+6.1f}%  wick {m['upper_wick']*100:3.0f}%  ${m['qv']/1e6:.2f}M")


# ---------------------------------------------------------------- replay
def replay(symbol, date, interval, tz, rules, C):
    step = INTERVAL_MS[interval]
    day0 = int(dt.datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=tz).timestamp() * 1000)
    start, end = day0 - (rules.min_bars + 20) * step, day0 + 86_400_000
    bars, cur = [], start
    while cur < end:
        chunk = klines(symbol, interval, 1500, cur, end)
        if not chunk:
            break
        bars += chunk
        cur = chunk[-1]["t"] + step
    bars = [b for b in bars if b["t"] + step <= time.time() * 1000]
    if not bars:
        print("no data")
        return []
    cd = Cooldown(C.get("cooldown_bars", 12), rules)
    hi = max(range(len(bars)), key=lambda j: bars[j]["h"])
    print(f"\nReplay {symbol} {date} ({interval})  {len(bars)} bars; day high {fmt_price(bars[hi]['h'])} @ "
          f"{dt.datetime.fromtimestamp(bars[hi]['t']/1000, tz):%H:%M}\n")
    rows = []
    for i in range(len(bars)):
        if bars[i]["t"] < day0:
            continue
        for sig, m in rules.detect(bars, i):
            if not cd.ok(sig, i, sig, m["high"]):
                continue
            fut = bars[i + 1:i + 31]
            up = max((x["h"] for x in fut), default=m["price"]) / m["price"] - 1
            dn = min((x["l"] for x in fut), default=m["price"]) / m["price"] - 1
            print(line(rules, symbol, sig, m, bars[i]["t"], tz) + f"  | next 30: max {up*100:+.1f}% min {dn*100:+.1f}%")
            rows.append((sig, up, dn))
    if not rows:
        print("(no signals that day)")
    return rows


# ---------------------------------------------------------------- live
def symbols(min_24h_usd):
    info = get("/fapi/v1/exchangeInfo")
    live = {s["symbol"] for s in info["symbols"]
            if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"}
    return sorted(t["symbol"] for t in get("/fapi/v1/ticker/24hr")
                  if t["symbol"] in live and float(t["quoteVolume"]) >= min_24h_usd)


def market_caps(pages=8):
    """CoinGecko circulating market caps {TICKER: cap}; the largest wins on ticker clashes."""
    caps = {}
    for page in range(1, pages + 1):
        rows = vc.get_json("https://api.coingecko.com/api/v3/coins/markets",
                           {"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": page},
                           headers={"User-Agent": "Mozilla/5.0"}, tries=4, rate_limit_backoff=15, timeout=20)
        for c in rows:
            sym, cap = c["symbol"].upper(), c.get("market_cap") or 0
            if cap > caps.get(sym, 0):
                caps[sym] = cap
        time.sleep(3)
    return caps


def base_asset(symbol):
    """XYZUSDT -> XYZ; 1000XYZUSDT -> XYZ; 1000000XYZUSDT -> XYZ (strip quantity prefixes)"""
    b = symbol[:-4] if symbol.endswith("USDT") else symbol
    for pre in ("1000000", "1000", "1M"):
        if b.startswith(pre) and len(b) > len(pre):
            return b[len(pre):]
    return b


def oi_change(symbol):
    """Open-interest change over the last ~15 min, in contracts (not notional)."""
    try:
        h = get("/futures/data/openInterestHist", {"symbol": symbol, "period": "5m", "limit": 4})
        return float(h[-1]["sumOpenInterest"]) / float(h[0]["sumOpenInterest"]) - 1
    except Exception:
        return None


def monitor(C, rules, tz, log_path, once=False, max_symbols=0):
    interval = C.get("interval", "3m")
    step = INTERVAL_MS[interval]
    min_24h, workers = C.get("min_24h_usd", 5e6), C.get("workers", 8)
    HL, RR = C.get("highlight", {}), C.get("rr", {})
    rr_codes = set(RR.get("signals", [])) if RR.get("enabled") else set()
    cd, seen = Cooldown(C.get("cooldown_bars", 12), rules), set()
    syms, refreshed, caps, caps_at = [], 0, {}, 0
    new_log = not os.path.exists(log_path)
    logf = open(log_path, "a", newline="")
    log = csv.writer(logf)
    if new_log:
        log.writerow(["time", "symbol", "signal", "price", "vol_ratio", "chg1", "chgN", "run", "upper_wick", "bar_usd", "oi_15m"])
    print(f"radar: {interval} klines, 24h volume ≥ ${min_24h/1e6:.0f}M, {len(rules.rules)} rules"
          f" + {len(rules.plugins)} plugins, config {vc.load_config()['_path']}, log {log_path}")
    while True:
        if time.time() - refreshed > 3600:
            # Keep the old list if the refresh fails (e.g. network blip) and retry in 5 min.
            try:
                syms, refreshed = symbols(min_24h), time.time()
                if max_symbols:
                    syms = syms[:max_symbols]
                print(f"[{dt.datetime.now(tz):%H:%M}] watching {len(syms)} contracts")
            except Exception as e:
                refreshed = time.time() - 3600 + 300
                print(f"[{dt.datetime.now(tz):%H:%M}] symbol refresh failed, keeping {len(syms)}: {e}")
                if once:
                    raise
        if HL.get("max_market_cap") and time.time() - caps_at > 3600:
            try:
                caps, caps_at = market_caps(), time.time()
            except Exception as e:
                caps_at = time.time() - 3600 + 600
                print(f"[{dt.datetime.now(tz):%H:%M}] market cap refresh failed: {e}")
        if not once:
            now = time.time() * 1000
            time.sleep(max(0, (step - now % step) / 1000 + 3))   # 3 s after the next close

        def scan(s):
            try:
                now_ms = time.time() * 1000
                bars = [b for b in klines(s, interval, rules.min_bars + 40) if b["t"] + step <= now_ms]
                return s, bars, rules.detect(bars, len(bars) - 1)
            except Exception as e:
                if once:
                    print(f"  ({s}: {e})", file=sys.stderr)
                return s, None, []

        hits, scanned = 0, 0
        with cf.ThreadPoolExecutor(workers) as ex:
            for s, bars, sigs in ex.map(scan, syms):
                scanned += bars is not None
                for sig, m in sigs:
                    t = bars[-1]["t"]
                    if (s, sig, t) in seen or not cd.ok((s, sig), t // step, sig, m["high"]):
                        continue
                    seen.add((s, sig, t))
                    oi = oi_change(s) if C.get("oi_on_hit", True) else None
                    txt = line(rules, s, sig, m, t, tz) + (f"  OI 15m {oi*100:+.1f}%" if oi is not None else "")
                    cap = caps.get(base_asset(s), 0)
                    flag = sig in HL.get("signals", []) and 0 < cap <= HL.get("max_market_cap", 0)
                    if cap:
                        txt += f"  mcap ${cap/1e6:,.0f}M"
                    if flag:
                        txt = HL.get("prefix", "★") + " " + txt
                    if sig in rr_codes:
                        try:
                            txt += "\n" + rr_text(rr_long(s, m["price"], t + step, bars, RR))
                        except Exception as e:
                            txt += f"\nR:R failed: {e}"
                    print(txt)
                    icon, title, desc = rules.info(sig)
                    head = f"{icon} {title} {s}"
                    if flag:
                        head = f"{HL.get('prefix', '★')} {HL.get('title', 'small cap')} (${cap/1e6:,.0f}M) {head}"
                    vc.telegram(f"{head}\n{desc}\n\n{txt}".replace("\n\n\n", "\n\n"))
                    log.writerow([dt.datetime.fromtimestamp(t / 1000, tz).isoformat(), s, sig, m["price"],
                                  round(m["vr"], 2), round(m["chg1"], 4), round(m["chgN"], 4), round(m["run"], 4),
                                  round(m["upper_wick"], 3), round(m["qv"]), None if oi is None else round(oi, 4)])
                    hits += 1
        logf.flush()
        vc.heartbeat("radar", step / 1000, f"{scanned}/{len(syms)} scanned, {hits} hits")
        if once:
            print(f"[{dt.datetime.now(tz):%H:%M}] one pass done: {scanned}/{len(syms)} symbols scanned, {hits} signals")
            return hits
        if not hits:
            print(f"[{dt.datetime.now(tz):%H:%M}] quiet", end="\r")


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="Binance USDT-M volume/price anomaly radar")
    ap.add_argument("--config", help="config file (default: $VR_CONFIG, config.local.json, config.example.json)")
    ap.add_argument("--interval", choices=INTERVAL_MS, help="override radar.interval")
    ap.add_argument("--replay", metavar="SYMBOL", help="replay a single symbol for --date")
    ap.add_argument("--date", help="replay date YYYY-MM-DD (in the configured timezone)")
    ap.add_argument("--once", action="store_true", help="scan the latest closed bar once and exit")
    ap.add_argument("--no-push", action="store_true", help="never send Telegram messages")
    ap.add_argument("--max-symbols", type=int, default=0, help="limit the universe (testing)")
    ap.add_argument("--log", help="CSV log path (default radar.log_csv or signals.csv)")
    ap.add_argument("--list-metrics", action="store_true", help="print the metrics rules can use")
    a = ap.parse_args()
    if a.list_metrics:
        for k, v in METRICS_DOC.items():
            print(f"  {k:<12} {v}")
        return
    if a.no_push:
        os.environ["VR_NO_PUSH"] = "1"
    vc.load_config(a.config)
    C = dict(vc.section("radar"))
    if a.interval:
        C["interval"] = a.interval
    rules = Rules(C)
    tz = dt.timezone(dt.timedelta(hours=vc.load_config().get("tz_hours", 0)))
    if a.replay:
        if not a.date:
            ap.error("--replay requires --date")
        replay(a.replay.upper(), a.date, C.get("interval", "3m"), tz, rules, C)
        return
    log = a.log or C.get("log_csv", "signals.csv")
    log = log if os.path.isabs(log) else os.path.join(vc.HERE, log)
    try:
        monitor(C, rules, tz, log, once=a.once, max_symbols=a.max_symbols)
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
