"""Central configuration loader.

Reads all parameters from `config.ini` (next to this file, or the path in the
CONFIG_FILE environment variable) and exposes them as typed module-level values.
Both `webhook_listener.py` and `binance_trader.py` initialize from here, so the
whole bot is configured in one place.

Blank values fall back to the built-in defaults below.
"""

import configparser
import logging
import os

logger = logging.getLogger("config")

# Built-in defaults; used when a key is missing or blank in config.ini.
_DEFAULTS = {
    "webhook": {
        "secret": "CHANGE_ME_SECRET",
        "port": "5000",
        "csv_path": "alerts.csv",
    },
    "binance": {
        "api_key": "",
        "api_secret": "",
        "testnet": "true",
        "quantity": "0.001",
        "leverage": "",
        "take_profit_pct": "",
        "stop_loss_pct": "",
        "max_holding_time": "",
    },
}

CONFIG_PATH = os.environ.get("CONFIG_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config.ini"
)

_parser = configparser.ConfigParser()
_parser.read_dict(_DEFAULTS)  # seed defaults first
_read = _parser.read(CONFIG_PATH, encoding="utf-8")
if _read:
    logger.info("Loaded configuration from %s", CONFIG_PATH)
else:
    logger.warning("Config file %s not found; using built-in defaults", CONFIG_PATH)


def _get(section, key):
    return _parser.get(section, key, fallback="").strip()


def _get_bool(section, key, default):
    val = _get(section, key)
    if not val:
        return default
    return val.lower() in ("1", "true", "yes", "on")


def _get_int(section, key, default):
    val = _get(section, key)
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _get_pos_float(section, key):
    """Positive float, or None if blank/invalid/<=0 (used for optional rules)."""
    val = _get(section, key)
    if not val:
        return None
    try:
        f = float(val)
    except ValueError:
        logger.warning("Ignoring non-numeric [%s] %s=%r", section, key, val)
        return None
    return f if f > 0 else None


# --- Webhook ---------------------------------------------------------------
WEBHOOK_SECRET = _get("webhook", "secret") or "CHANGE_ME_SECRET"
WEBHOOK_PORT = _get_int("webhook", "port", 5000)
CSV_PATH = _get("webhook", "csv_path") or "alerts.csv"

# --- Binance ---------------------------------------------------------------
BINANCE_API_KEY = _get("binance", "api_key")
BINANCE_API_SECRET = _get("binance", "api_secret")
BINANCE_TESTNET = _get_bool("binance", "testnet", True)
TRADE_QUANTITY = _get("binance", "quantity") or "0.001"
# "usdt" -> `quantity` is USDT notional; "token" -> base-asset units.
TRADE_QUANTITY_TYPE = (_get("binance", "quantity_type") or "usdt").lower()
if TRADE_QUANTITY_TYPE not in ("usdt", "token"):
    logger.warning("Unknown quantity_type=%r; defaulting to 'usdt'", TRADE_QUANTITY_TYPE)
    TRADE_QUANTITY_TYPE = "usdt"
TRADE_LEVERAGE = _get("binance", "leverage") or None
TAKE_PROFIT_PCT = _get_pos_float("binance", "take_profit_pct")
STOP_LOSS_PCT = _get_pos_float("binance", "stop_loss_pct")
MAX_HOLDING_TIME = _get_pos_float("binance", "max_holding_time")
