# crypto-volume-radar

A small, dependency-free toolkit for watching crypto markets and pushing alerts to Telegram:

- **Volume/price radar** (`radar.py`): scans every liquid Binance USDT-M perpetual each time a
  kline closes and fires *your* signal rules (volume ratio, % move, run-up, wick, ...).
- **Price-level alerts** (`alerts.py` + `context.py`): pushes when price crosses levels you set,
  with an optional market-structure snippet (OI, funding, top-trader ratio, overhead volume).
- **On-chain DEX pool alerts** (`onchain-alert.py`): the same level alerts for tokens that only
  trade on DEXes, priced through GeckoTerminal. One process watches all your pools.

Pure Python 3.9+ standard library. No database, no pip install.

> **Bring your own watchlist, rules and thresholds.** This repo ships a framework and deliberately
> generic example settings. The example thresholds are placeholders, not tuned values and not
> trading advice. Decide what to watch, write your own rules, and validate them with your own
> data and backtests before you rely on them. Nothing here is investment advice.

## Quick start

```bash
git clone https://github.com/Alvinhung/crypto-volume-radar.git
cd crypto-volume-radar
cp config.example.json config.local.json      # your private settings (gitignored)
cp alerts.example.txt alerts.txt              # your price levels (gitignored)
cp .env.example .env && chmod 600 .env        # Telegram bot token + chat id(s)

python3 radar.py --once --no-push             # one scan, console only
./run.sh radar.py                             # run forever, loads .env
./run.sh alerts.py
./run.sh onchain-alert.py
```

Without `TG_BOT_TOKEN` everything still runs and just prints to the console. `--no-push` (or
`VR_NO_PUSH=1`) forces console-only mode.

Binance blocks some regions (for example US-hosted servers return HTTP 451). Run it from a
region Binance serves.

## Configuration

Config is loaded from `$VR_CONFIG`, else `config.local.json`, else `config.example.json`.
Keep your real settings in `config.local.json`; it is in `.gitignore`.

### Radar rules

Rules are data, not code. Each rule is a list of conditions that must all hold on the bar
that just closed:

```json
{"code": "BREAKOUT", "icon": "⬆️", "title": "Breakout on volume", "desc": "...",
 "when": [["vr", ">=", 5], ["chgN", ">=", 0.08], ["green", "==", true]]}
```

- Operators: `>= <= > < == !=`. A value starting with `$` refers to another metric (`["price", ">", "$peak"]`).
- `{"any": [cond, cond]}` is an OR group.
- `suppress: [["A", "B"]]` drops signal B when A fires on the same bar.
- `refire_on_new_high: true` lets a rule fire again inside the cooldown if it prints a higher high.
- `windows` sets the look-back lengths (`base_n` for the volume median, `chg_n`, `low_n`, `peak_n`, `high_n`).
- `python3 radar.py --list-metrics` shows every metric a rule can use.

Need more than the DSL? Write a Python plugin (see `plugins/example_rules.py`) and list it in
`radar.plugins`.

Other radar options: `interval`, `min_24h_usd` (universe filter), `min_bar_usd`, `cooldown_bars`,
`highlight` (tag signals on coins under a market cap, from CoinGecko), `rr` (optional stop/target
helper, for position sizing only).

Replay a past day to see what your rules would have fired:

```bash
python3 radar.py --replay BTCUSDT --date 2026-01-15
```

Every live signal is appended to `signals.csv`, so you can do your own analysis later.

### Price-level alerts

`alerts.txt`, one per line: `SYMBOL >=|<= PRICE [note]`. Prefix `*` for a repeatable alert.
`BB:` symbols are priced from Bybit. The file is re-read when it changes, so you don't need to
restart. Alerts fire on crossings only, re-arm after price moves `rearm_band` away from the level,
and one-shot alerts are remembered across restarts (`alerts.state`).

### On-chain pool alerts

List the pools in `onchain.pools` (`name`, `network` as GeckoTerminal names it, the **pool**
address rather than the token address, and `every_s`). Put the levels in `onchain-alerts.txt`, using the
pool `name` as the symbol. Requests are serialized and spaced (`min_request_gap_s`) so many
pools stay within GeckoTerminal's free rate limit.

## Running 24/7

**Linux (systemd):** `deploy/systemd/crypto-volume-radar@.service` is a template unit (one instance per script):

```bash
sudo cp -r . /opt/crypto-volume-radar
sudo cp deploy/systemd/crypto-volume-radar@.service deploy/systemd/crypto-volume-radar-watchdog.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-volume-radar@radar crypto-volume-radar@alerts crypto-volume-radar@onchain-alert
sudo systemctl enable --now crypto-volume-radar-watchdog.timer
```

**macOS (launchd):** see `deploy/launchd/com.example.crypto-volume-radar.radar.plist`. launchd restarts
crashed processes but cannot stop a laptop from sleeping, so for 24/7 use a machine that never sleeps.

### Heartbeats: know when it stops

When a monitor stops, the chat just goes quiet, and that looks the same as a quiet market. So every
loop writes `heartbeats/<name>.json`. `watchdog.py` (run by the timer above or by cron) pushes
an alert when a heartbeat is older than `stale_factor` × its expected interval, and another when
it recovers. To also catch the whole machine going down, set `VR_DEADMAN_URL` (or
`heartbeat.deadman_url`) to an external dead-man's-switch such as Healthchecks.io. The watchdog
pings it on every run, and the external service alerts you when the pings stop.

## Tests

```bash
python3 -m unittest discover tests     # offline: uses a local mock of the Binance API
```

## Requirements

Python 3.9+. Standard library only (`urllib`, `json`, `csv`, `statistics`, ...).

## 中文說明

這是一套只用 Python 標準庫的加密貨幣監控框架，包含三個模組：幣安 USDT 永續的量價異動雷達、價位警報，以及鏈上 DEX 池子警報。觸發後推播到 Telegram。

**監控清單、訊號規則和門檻都要自己定。** 範例設定裡的數字只是示範用的佔位值，沒有經過調校，也不構成投資建議。請用自己的資料和回測驗證之後再使用。私人設定放在 `config.local.json`、`alerts.txt`、`onchain-alerts.txt`，這三個檔案都在 `.gitignore` 裡，不會進版控。

## License

MIT © Alvinhung
