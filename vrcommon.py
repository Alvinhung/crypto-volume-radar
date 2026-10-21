"""Shared helpers: config loading, HTTP with retry/backoff, Telegram notifier,
heartbeat files and a tiny condition DSL used by the pluggable signal rules.

Standard library only.
"""
import json
import operator
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- config
_CONFIG = None


def config_path():
    """VR_CONFIG env var > config.local.json > config.example.json"""
    p = os.getenv("VR_CONFIG")
    if p:
        return p
    local = os.path.join(HERE, "config.local.json")
    return local if os.path.exists(local) else os.path.join(HERE, "config.example.json")


def load_config(path=None, reload=False):
    global _CONFIG
    if _CONFIG is None or reload or path:
        p = path or config_path()
        with open(p, encoding="utf-8") as f:
            _CONFIG = json.load(f)
        _CONFIG["_path"] = p
    return _CONFIG


def section(name, path=None):
    return load_config(path).get(name, {})


def binance_fapi():
    return os.getenv("VR_FAPI_BASE") or load_config().get("binance", {}).get("fapi_base", "https://fapi.binance.com")


# ---------------------------------------------------------------- HTTP
def get_json(url, params=None, headers=None, tries=4, timeout=20, backoff=2.0, rate_limit_backoff=15.0):
    """GET a JSON document. Retries on network errors; backs off longer on 418/429."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    h = {"User-Agent": "volume-radar", "Accept": "application/json"}
    h.update(headers or {})
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if i == tries - 1:
                raise
            if e.code in (418, 429):
                time.sleep(rate_limit_backoff * (i + 1))
            elif 400 <= e.code < 500:
                raise
            else:
                time.sleep(backoff * (i + 1))
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(backoff * (i + 1))
    raise RuntimeError("request failed: " + url)


# ---------------------------------------------------------------- notifier
def push_disabled():
    return os.getenv("VR_NO_PUSH", "") not in ("", "0", "false")


def telegram(text, tries=4):
    """Send to every chat in TG_CHAT_ID (comma separated). Returns True if all succeeded.

    Without TG_BOT_TOKEN (or with VR_NO_PUSH=1) the message is only printed by the caller.
    """
    tok, chats = os.getenv("TG_BOT_TOKEN"), os.getenv("TG_CHAT_ID", "")
    if push_disabled():
        return False
    if not tok:
        print("  (TG_BOT_TOKEN not set; console only)", file=sys.stderr)
        return False
    ok = True
    for chat in [c.strip() for c in chats.split(",") if c.strip()]:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        for i in range(tries):
            try:
                urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, timeout=15)
                break
            except Exception as e:
                if i == tries - 1:
                    print(f"  (Telegram push failed after {tries} tries: {e})", file=sys.stderr)
                    ok = False
                else:
                    time.sleep(3 * (i + 1))
    return ok


# ---------------------------------------------------------------- heartbeat
def heartbeat_dir():
    d = os.getenv("VR_HEARTBEAT_DIR") or load_config().get("heartbeat", {}).get("dir") or os.path.join(HERE, "heartbeats")
    d = d if os.path.isabs(d) else os.path.join(HERE, d)
    os.makedirs(d, exist_ok=True)
    return d


def heartbeat(name, interval_s, note=""):
    """Write <dir>/<name>.json each loop. watchdog.py alerts when it goes stale."""
    try:
        path = os.path.join(heartbeat_dir(), name + ".json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"name": name, "ts": time.time(), "interval_s": interval_s, "pid": os.getpid(), "note": note}, f)
        os.replace(tmp, path)
    except Exception as e:
        print(f"  (heartbeat write failed: {e})", file=sys.stderr)


# ---------------------------------------------------------------- condition DSL
OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt,
       "==": operator.eq, "!=": operator.ne}


def check(cond, m):
    """cond = [metric, op, value]; value may be a number, bool, or the name of another metric
    prefixed with '$' (e.g. ["close", "<", "$ma"]). Missing / None metrics evaluate to False."""
    key, op, val = cond
    left = m.get(key)
    right = m.get(val[1:]) if isinstance(val, str) and val.startswith("$") else val
    if left is None or right is None:
        return False
    if op not in OPS:
        raise ValueError(f"unknown operator {op!r} in {cond}")
    try:
        return OPS[op](left, right)
    except TypeError:
        return False


def all_true(conds, m):
    """A rule matches when every condition holds. An optional {"any": [...]} entry is an OR group."""
    for c in conds:
        if isinstance(c, dict) and "any" in c:
            if not any(all_true(sub if isinstance(sub[0], (list, dict)) else [sub], m) for sub in c["any"]):
                return False
        elif not check(c, m):
            return False
    return True

