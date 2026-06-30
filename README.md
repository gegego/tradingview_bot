# TradingView Flash-Crash Trading Bot

An end-to-end pipeline for trading flash crashes on Binance USDT-M Futures:

1. **TradingView Pine scripts** ([`tradingview/`](tradingview/)) detect a flash crash and
   fire an alert webhook.
2. A small **Flask listener** ([`webhook_listener.py`](webhook_listener.py)) receives each
   alert, validates a shared secret, records it to `alerts.csv`, and (optionally) places a
   Binance Futures order via [`binance_trader.py`](binance_trader.py).
3. A **backtester** ([`backtest.py`](backtest.py)) replays the recorded alerts against
   historical price data to evaluate take-profit / stop-loss / holding-time exit rules.

## Install

```bash
pip install -r requirements.txt          # Flask + python-binance (runtime)
pip install ccxt pandas plotly           # extra deps for the backtester
```

## Run the listener

All runtime parameters live in [`config.ini`](config.ini). Edit it, then start the server:

```bash
python webhook_listener.py
```

To use a config file in a different location, set the `CONFIG_FILE` environment variable to
its path.

### `config.ini` reference

**`[webhook]`**

| Key        | Default            | Description                          |
| ---------- | ------------------ | ------------------------------------ |
| `secret`   | `CHANGE_ME_SECRET` | Shared secret required in each alert |
| `port`     | `5000`             | Port to listen on                    |
| `csv_path` | `alerts.csv`       | Where alerts are recorded            |

**`[binance]`**

| Key                | Default   | Description                                                              |
| ------------------ | --------- | ----------------------------------------------------------------------- |
| `api_key`          | *(empty)* | API key. If empty, trading is disabled (alerts are still recorded).     |
| `api_secret`       | *(empty)* | API secret.                                                             |
| `testnet`          | `true`    | `true` -> testnet, `false` -> **live** real-money trading.              |
| `quantity_type`    | `usdt`    | `usdt` = `quantity` is USDT notional; `token` = base-asset units.       |
| `quantity`         | `0.001`   | Fixed order size, interpreted per `quantity_type`.                      |
| `leverage`         | *(blank)* | Optional leverage to set per symbol, e.g. `5`. Blank = account default. |
| `take_profit_pct`  | *(blank)* | Take-profit distance in %, e.g. `1.0`. Blank = off.                     |
| `stop_loss_pct`    | *(blank)* | Stop-loss distance in %, e.g. `0.5`. Blank = off.                       |
| `max_holding_time` | *(blank)* | Seconds to hold before force-closing. Blank = off.                     |

Blank values fall back to the built-in defaults in [`config.py`](config.py).

## Binance trading

When an alert arrives, the listener places a Binance **USDT-M Futures** market order. This
lives in [`binance_trader.py`](binance_trader.py) and is called after each alert is recorded.

- **Side:** the listener currently **always opens a SELL (short)** position — flash-crash
  entries are short by design. (The `side` field in the payload is not used; the side is
  hardcoded in [`webhook_listener.py`](webhook_listener.py).)
- **Quantity** is fixed in `config.ini`. With `quantity_type = usdt` the configured amount is
  treated as USDT notional and converted to a token size at the current price (floored to the
  symbol's lot step); with `quantity_type = token` it's used directly as a base-asset amount.
- Trading defaults to the **Binance Futures testnet**. It is only active when API credentials
  are supplied — without them, alerts are still recorded and the trade is reported as
  `skipped`.
- Orders carry a unique `newClientOrderId` and transient gateway/network errors (502/503/504)
  are retried idempotently, so a lost response never double-fills.

### Position management

When an entry order fills, the trader applies whichever of these rules are configured:

- **`take_profit_pct`** — a `TAKE_PROFIT_MARKET` order that closes the whole position once
  price moves the given % in your favour (below entry for a short).
- **`stop_loss_pct`** — a `STOP_MARKET` order that closes the position once price moves the
  given % against you.
- **`max_holding_time`** — arms a timer; after that many seconds it cancels any open TP/SL
  orders and force-closes the remaining position with a `reduceOnly` market order.
- **One position per pair** — before opening, the trader checks the live position for the
  symbol. If one already exists, the new alert is **skipped** (no pyramiding / averaging in).
  This rule is always on and needs no configuration.

TP/SL prices are snapped to each symbol's tick size automatically. All three rules are
independent — enable any combination. The timer lives in the listener process, so it only
fires while the server is running.

> ⚠️ Setting `testnet = false` in `config.ini` places **real orders with real funds** the
> moment an alert arrives. Test thoroughly on testnet first. Get testnet keys at
> <https://testnet.binancefuture.com/>.

The TradingView ticker is normalized automatically: `BTCUSDT.P` -> `BTCUSDT`.

## TradingView setup

### Pine scripts ([`tradingview/`](tradingview/))

| File                                                                          | Purpose                                                              |
| ----------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| [`flash_crash_detector_v2.pine`](tradingview/flash_crash_detector_v2.pine)    | Indicator — ROC + volume + ATR flash-crash detection with alerts.   |
| [`flash_crash_strategy_v2.pine`](tradingview/flash_crash_strategy_v2.pine)    | Strategy version of the v2 detector (for backtesting in TradingView). |
| [`flash_dump_detector.pine`](tradingview/flash_dump_detector.pine)            | Earlier dump detector.                                              |
| [`flash_dump_strategy.pine`](tradingview/flash_dump_strategy.pine)            | Strategy version of the dump detector.                             |

The detector fires on a bearish bar when either a cumulative ROC drop over K bars exceeds a
threshold **with** elevated volume, or a single violent candle (body > ATR×) occurs with
elevated volume. It can fire intrabar (earliest warning) or on bar close.

### Exposing the listener to TradingView

TradingView only delivers webhooks to **public** URLs on ports **80/443**, so for local
testing expose the port with a tunnel such as [ngrok](https://ngrok.com/):

```bash
ngrok http 5000
```

Use the resulting `https://<id>.ngrok.../webhook` URL as the alert's **Webhook URL**.

### Configuring the alert

In the alert dialog:

1. Enable **Webhook URL** and paste your public `.../webhook` URL.
2. Set the **Message** to JSON matching your secret:

   ```json
   {
     "secret": "your-secret",
     "ticker": "{{ticker}}",
     "close": {{close}}
   }
   ```

When the alert fires, TradingView substitutes the placeholders (e.g.
`"ticker": "BTCUSDT.P", "close": 10000`), the listener appends a row to `alerts.csv`, and —
if credentials are configured — a short market order is placed on Binance Futures.

## Endpoints

- `POST /webhook` — receives alerts (requires correct `secret`).
- `GET /health` — returns `{"status": "alive"}` for a quick connectivity check.

## Recorded data

`alerts.csv` columns:

| Column        | Example                                                    |
| ------------- | ---------------------------------------------------------- |
| `received_at` | `2026-06-20T14:03:11`                                      |
| `pair`        | `BTCUSDT.P`                                                |
| `close`       | `10000`                                                    |
| `raw_json`    | `{"secret": "...", "ticker": "BTCUSDT.P", "close": 10000}` |

The full raw payload is stored so nothing is lost if the alert format changes.

## Backtesting

[`backtest.py`](backtest.py) replays the entries in `alerts.csv` against historical 1-minute
OHLCV (fetched from Binance USDT-M perpetuals via `ccxt` and cached under `ohlcv_cache/`) and
simulates each trade under configurable TP / SL / max-holding rules. Tunables (TP%, SL%,
holding time, directions, fees, etc.) are constants at the top of the file.

```bash
python backtest.py
```

Outputs:

- `trade_results_long.csv` / `trade_results_short.csv` — per-trade logs.
- A console summary per direction (win rate, total/avg PnL, max drawdown, exit-reason counts).
- One interactive candlestick chart per pair under `charts/`, with entries and exits overlaid
  (set `MAKE_CHARTS = False` to skip).

The backtester mirrors the live "one position per pair" rule: an alert that arrives while a
pair's simulated position is still open is skipped. When both TP and SL are touchable in the
same bar it conservatively assumes the stop loss fills first.

## Deployment (Linux / systemd)

Example unit files are included for running behind Gunicorn with an ngrok tunnel:

- [`trading.service`](trading.service) — runs `gunicorn -w 4 -b 127.0.0.1:5000
  webhook_listener:app`.
- [`ngrok.service`](ngrok.service) — starts the ngrok tunnel after the Flask service.

The built-in Flask dev server (`python webhook_listener.py`) is fine for a single
low-frequency webhook. For heavier or always-on use, run behind a production WSGI server such
as Gunicorn (Linux) or `waitress-serve --port=5000 webhook_listener:app` (cross-platform).
