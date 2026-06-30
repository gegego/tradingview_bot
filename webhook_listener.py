"""TradingView webhook listener.

Receives alert POSTs from TradingView, validates a shared secret, and appends
each alert (with the arrival time) to a CSV file.

Expected JSON body (configure this in the TradingView alert "Message" box):

    {
        "secret": "CHANGE_ME_SECRET",
        "ticker": "{{ticker}}",
        "close": {{close}}
    }

At runtime TradingView substitutes its placeholders, e.g.
    {"secret": "...", "ticker": "BTCUSDT.P", "close": 10000}
"""

import csv
import hmac
import json
import logging
import os
import threading
from datetime import datetime

from flask import Flask, jsonify, request

import config
from binance_trader import trader

# --- Configuration (all values come from config.ini via config.py) ----------
WEBHOOK_SECRET = config.WEBHOOK_SECRET
WEBHOOK_PORT = config.WEBHOOK_PORT
CSV_PATH = config.CSV_PATH

CSV_HEADER = ["received_at", "pair", "close", "raw_json"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("webhook_listener")

app = Flask(__name__)
_csv_lock = threading.Lock()


def record_alert(pair, close, raw):
    """Append one alert as a row to the CSV file (thread-safe)."""
    received_at = datetime.now().isoformat(timespec="seconds")
    with _csv_lock:
        # Write the header if the file is missing or empty.
        write_header = not os.path.exists(CSV_PATH) or os.path.getsize(CSV_PATH) == 0
        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(CSV_HEADER)
            writer.writerow([received_at, pair, close, json.dumps(raw, ensure_ascii=False)])
    return received_at


@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_json(force=True, silent=True)
    if payload is None or not isinstance(payload, dict):
        logger.warning("Rejected request with invalid/non-JSON body")
        return jsonify({"status": "error", "reason": "invalid JSON body"}), 400

    # Constant-time secret comparison; missing secret -> empty string.
    supplied_secret = str(payload.get("secret", ""))
    if not hmac.compare_digest(supplied_secret, WEBHOOK_SECRET):
        logger.warning("Rejected request with invalid secret")
        return jsonify({"status": "error", "reason": "unauthorized"}), 401

    # Accept either naming convention from the alert template.
    pair = payload.get("ticker") or payload.get("pair")
    close = payload.get("close")
    if close is None:
        close = payload.get("price")

    received_at = record_alert(pair, close, payload)
    logger.info("Alert recorded: pair=%s close=%s at=%s", pair, close, received_at)

    # Optionally place a Binance order. The alert decides the side via a "side"
    # (or "action") field; quantity is fixed in the trader's configuration.
    # Recording the alert always succeeds even if trading is disabled or fails.
    response = {"status": "ok"}
    side = 'SELL'
    if side:
        trade_result = trader.place_order(pair, side)
        response["trade"] = trade_result

    return jsonify(response), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "alive"}), 200


if __name__ == "__main__":
    logger.info("Starting webhook listener on port %s, writing to %s", WEBHOOK_PORT, CSV_PATH)
    if WEBHOOK_SECRET == "CHANGE_ME_SECRET":
        logger.warning("Using the default WEBHOOK_SECRET - set WEBHOOK_SECRET before going live!")

    app.run(host="0.0.0.0", port=WEBHOOK_PORT, debug=True, use_reloader=False)
