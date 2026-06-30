"""Binance USDT-M Futures trading module.

Called by the webhook listener to place a market order in response to a
TradingView alert. Kept deliberately small: one configurable trader object and
a `place_order(symbol, side)` method.

Order side comes from the alert payload (BUY / SELL); order size is fixed in
configuration (quantity) and may be expressed either as USDT notional
(quantity_type = usdt, converted to a token size at order time) or directly as a
base-asset amount (quantity_type = token).

After the entry order fills, the trader can manage the position with three rules:
    * take_profit_pct  -> a TAKE_PROFIT_MARKET order that closes the position in profit.
    * stop_loss_pct    -> a STOP_MARKET order that closes the position at a loss.
    * max_holding_time -> a timer that force-closes the position after N seconds.

Configuration is loaded from `config.ini` via the `config` module (section
[binance]): api_key, api_secret, testnet, quantity, leverage, take_profit_pct,
stop_loss_pct, max_holding_time.

Safety:
    * Defaults to the Binance Futures **testnet**.
    * If API keys are not configured, ordering is disabled and calls return a
      skipped result instead of raising, so the webhook can still record alerts.
"""

import logging
import math
import threading
import time
import uuid

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceOrderException
from requests.exceptions import RequestException

import config

logger = logging.getLogger("binance_trader")

VALID_SIDES = {"BUY", "SELL"}

# Binance (especially the testnet) intermittently returns gateway errors that
# never reach the matching engine. Retry those transient failures a few times.
TRANSIENT_STATUS = {502, 503, 504}
MAX_RETRIES = 3
RETRY_BACKOFF = 1.0  # seconds; multiplied by the attempt number


def normalize_symbol(raw):
    """Turn a TradingView ticker into a Binance futures symbol.

    e.g. 'BTCUSDT.P' -> 'BTCUSDT', 'btcusdt' -> 'BTCUSDT'.
    """
    if not raw:
        return None
    symbol = str(raw).upper().strip()
    if symbol.endswith(".P"):  # perpetual suffix used by TradingView
        symbol = symbol[:-2]
    return symbol


def _opposite(side):
    return "SELL" if side == "BUY" else "BUY"


class BinanceTrader:
    def __init__(self):
        self.api_key = config.BINANCE_API_KEY
        self.api_secret = config.BINANCE_API_SECRET
        self.testnet = config.BINANCE_TESTNET
        self.quantity = config.TRADE_QUANTITY
        self.quantity_type = config.TRADE_QUANTITY_TYPE  # "usdt" or "token"
        self.leverage = config.TRADE_LEVERAGE  # optional
        # Position-management rules (all optional).
        self.take_profit_pct = config.TAKE_PROFIT_PCT
        self.stop_loss_pct = config.STOP_LOSS_PCT
        self.max_holding_time = config.MAX_HOLDING_TIME  # seconds
        self._client = None
        self._leverage_set = set()       # symbols we've already configured
        self._price_decimals = {}        # symbol -> price precision (decimals)
        self._tick = {}                  # symbol -> price tick size (float)
        self._step = {}                  # symbol -> quantity step size (float)
        self._qty_decimals = {}          # symbol -> quantity precision (decimals)
        self._timers = {}                # symbol -> threading.Timer

    @property
    def enabled(self):
        """True only if credentials are present, so we can place orders."""
        return bool(self.api_key and self.api_secret)

    @property
    def client(self):
        if self._client is None:
            self._client = Client(self.api_key, self.api_secret, testnet=self.testnet)
        return self._client

    def _create_order(self, **kwargs):
        """Place a futures order, retrying transient gateway/network errors.

        A 502/503/504 (or a dropped connection) is ambiguous: the request may
        have failed *before* reaching the matching engine, or it may have been
        executed and only the response was lost. To avoid placing a duplicate on
        retry, every order carries a unique ``newClientOrderId``; before each
        retry we check whether that order already exists on Binance and, if so,
        return it instead of sending again. Non-transient errors (insufficient
        balance, invalid params, ...) are re-raised immediately.
        """
        symbol = kwargs.get("symbol")
        cid = kwargs.get("newClientOrderId")
        if not cid:
            cid = "wh-" + uuid.uuid4().hex[:24]  # idempotency key (<=36 chars)
            kwargs["newClientOrderId"] = cid

        for attempt in range(MAX_RETRIES):
            try:
                return self.client.futures_create_order(**kwargs)
            except (BinanceAPIException, RequestException) as exc:
                transient = isinstance(exc, RequestException) or \
                    getattr(exc, "status_code", None) in TRANSIENT_STATUS
                if not transient or attempt == MAX_RETRIES - 1:
                    raise
                # The error may have arrived after the order executed. If it did,
                # don't send another one.
                existing = self._find_order(symbol, cid)
                if existing is not None:
                    logger.info("Order %s already executed despite gateway error; not resending", cid)
                    return existing
                wait = RETRY_BACKOFF * (attempt + 1)
                logger.warning(
                    "Transient Binance error (attempt %d/%d): %s; retrying in %.1fs",
                    attempt + 1, MAX_RETRIES, exc, wait,
                )
                time.sleep(wait)

    def _find_order(self, symbol, client_order_id):
        """Return the order with this client order id, or None if it doesn't exist."""
        try:
            return self.client.futures_get_order(symbol=symbol, origClientOrderId=client_order_id)
        except (BinanceAPIException, BinanceOrderException, RequestException):
            return None

    def _ensure_leverage(self, symbol):
        """Set leverage once per symbol if TRADE_LEVERAGE is configured."""
        if not self.leverage or symbol in self._leverage_set:
            return
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=int(self.leverage))
            self._leverage_set.add(symbol)
        except (BinanceAPIException, BinanceOrderException) as exc:
            logger.warning("Could not set leverage for %s: %s", symbol, exc)

    # --- Price helpers ------------------------------------------------------

    def _load_filters(self, symbol):
        """Cache the symbol's price tick, quantity step, and precisions."""
        if symbol in self._tick:
            return
        tick, decimals = 0.0, 2
        step, qty_decimals = 0.0, 3
        try:
            info = self.client.futures_exchange_info()
            for s in info["symbols"]:
                if s["symbol"] != symbol:
                    continue
                qty_decimals = s.get("quantityPrecision", qty_decimals)
                for f in s["filters"]:
                    if f["filterType"] == "PRICE_FILTER":
                        tick_str = f["tickSize"].rstrip("0")
                        tick = float(f["tickSize"])
                        decimals = len(tick_str.split(".")[1]) if "." in tick_str else 0
                    elif f["filterType"] == "LOT_SIZE":
                        step = float(f["stepSize"])
                break
        except (BinanceAPIException, BinanceOrderException, KeyError) as exc:
            logger.warning("Could not load filters for %s: %s", symbol, exc)
        self._tick[symbol] = tick
        self._price_decimals[symbol] = decimals
        self._step[symbol] = step
        self._qty_decimals[symbol] = qty_decimals

    def _round_price(self, symbol, price):
        """Snap a price to the symbol's tick size and decimal precision."""
        self._load_filters(symbol)
        tick = self._tick.get(symbol) or 0.0
        decimals = self._price_decimals.get(symbol, 2)
        if tick > 0:
            price = round(price / tick) * tick
        return round(price, decimals)

    def _round_qty(self, symbol, qty):
        """Floor a quantity to the symbol's lot step (never round a size up)."""
        self._load_filters(symbol)
        step = self._step.get(symbol) or 0.0
        decimals = self._qty_decimals.get(symbol, 3)
        if step > 0:
            qty = math.floor(qty / step) * step
        return round(qty, decimals)

    def _market_price(self, symbol):
        """Latest traded price for the symbol, or None if unavailable."""
        try:
            return float(self.client.futures_symbol_ticker(symbol=symbol)["price"])
        except (BinanceAPIException, BinanceOrderException, RequestException, KeyError) as exc:
            logger.warning("Could not read market price for %s: %s", symbol, exc)
            return None

    def _order_quantity(self, symbol):
        """Resolve the order size in base-asset units.

        With quantity_type 'usdt' the configured amount is USDT notional and is
        converted to a token quantity at the current price, then floored to the
        lot step. With 'token' the configured amount is used as-is.
        Returns a float, or None if it can't be determined / is too small.
        """
        if self.quantity_type == "usdt":
            price = self._market_price(symbol)
            if not price:
                return None
            qty = self._round_qty(symbol, float(self.quantity) / price)
        else:
            qty = float(self.quantity)
        return qty if qty > 0 else None

    def _entry_price(self, symbol):
        """Best-effort fill price: position entry price, falling back to mark price."""
        try:
            for p in self.client.futures_position_information(symbol=symbol):
                if p["symbol"] == symbol and float(p["entryPrice"]) > 0:
                    return float(p["entryPrice"])
        except (BinanceAPIException, BinanceOrderException, KeyError) as exc:
            logger.warning("Could not read position entry price for %s: %s", symbol, exc)
        try:
            return float(self.client.futures_symbol_ticker(symbol=symbol)["price"])
        except (BinanceAPIException, BinanceOrderException, KeyError) as exc:
            logger.warning("Could not read mark price for %s: %s", symbol, exc)
            return None

    def _position_amt(self, symbol):
        """Signed position size (positive long, negative short, 0 flat)."""
        try:
            for p in self.client.futures_position_information(symbol=symbol):
                if p["symbol"] == symbol:
                    return float(p["positionAmt"])
        except (BinanceAPIException, BinanceOrderException, KeyError) as exc:
            logger.warning("Could not read position amount for %s: %s", symbol, exc)
        return 0.0

    # --- Bracket (TP/SL) and timed exit -------------------------------------

    def _place_brackets(self, symbol, side, entry):
        """Place take-profit and/or stop-loss closing orders around `entry`."""
        result = {}
        exit_side = _opposite(side)
        long = side == "BUY"

        if self.take_profit_pct:
            tp = entry * (1 + self.take_profit_pct / 100) if long \
                else entry * (1 - self.take_profit_pct / 100)
            result["take_profit"] = self._place_close_order(
                symbol, exit_side, "TAKE_PROFIT_MARKET", self._round_price(symbol, tp))

        if self.stop_loss_pct:
            sl = entry * (1 - self.stop_loss_pct / 100) if long \
                else entry * (1 + self.stop_loss_pct / 100)
            result["stop_loss"] = self._place_close_order(
                symbol, exit_side, "STOP_MARKET", self._round_price(symbol, sl))

        return result

    def _place_close_order(self, symbol, exit_side, order_type, stop_price):
        """Place a closePosition conditional order; return its id or an error."""
        try:
            order = self._create_order(
                symbol=symbol,
                side=exit_side,
                type=order_type,
                stopPrice=stop_price,
                closePosition=True,
                timeInForce="GTE_GTC",
                workingType="MARK_PRICE",
            )
            logger.info("%s set for %s at %s (id=%s)",
                        order_type, symbol, stop_price, order.get("orderId"))
            return {"status": "ok", "stopPrice": stop_price, "orderId": order.get("orderId")}
        except (BinanceAPIException, BinanceOrderException) as exc:
            logger.error("Failed to set %s for %s: %s", order_type, symbol, exc)
            return {"status": "error", "reason": str(exc)}

    def _schedule_timeout(self, symbol):
        """Arm a timer to force-close the position after max_holding_time."""
        existing = self._timers.pop(symbol, None)
        if existing:
            existing.cancel()
        timer = threading.Timer(self.max_holding_time, self.close_position, args=(symbol,))
        timer.daemon = True
        timer.start()
        self._timers[symbol] = timer
        logger.info("Max holding time %.0fs armed for %s", self.max_holding_time, symbol)

    def close_position(self, symbol):
        """Cancel open orders and market-close any remaining position (reduceOnly)."""
        self._timers.pop(symbol, None)
        amt = self._position_amt(symbol)
        if amt == 0:
            logger.info("Holding-time reached for %s but position already closed", symbol)
            self._cancel_open_orders(symbol)
            return {"status": "flat"}
        exit_side = "SELL" if amt > 0 else "BUY"
        try:
            order = self._create_order(
                symbol=symbol,
                side=exit_side,
                type="MARKET",
                quantity=abs(amt),
                reduceOnly=True,
            )
            self._cancel_open_orders(symbol)
            logger.info("Force-closed %s (%s %s) after max holding time", symbol, exit_side, abs(amt))
            return {"status": "closed", "orderId": order.get("orderId")}
        except (BinanceAPIException, BinanceOrderException) as exc:
            logger.error("Failed to force-close %s: %s", symbol, exc)
            return {"status": "error", "reason": str(exc)}

    def _cancel_open_orders(self, symbol):
        try:
            self.client.futures_cancel_all_open_orders(symbol=symbol)
        except (BinanceAPIException, BinanceOrderException) as exc:
            logger.warning("Could not cancel open orders for %s: %s", symbol, exc)

    # --- Entry --------------------------------------------------------------

    def place_order(self, raw_symbol, side):
        """Place a market entry order plus TP/SL and a timed exit.

        Returns a result dict; never raises on trade errors.
        Result shape: {"status": "ok"|"skipped"|"error", ...}
        """
        symbol = normalize_symbol(raw_symbol)
        side = str(side or "").upper().strip()

        if not symbol:
            return {"status": "error", "reason": "missing symbol"}
        if side not in VALID_SIDES:
            return {"status": "error", "reason": f"invalid side: {side!r}"}
        if not self.enabled:
            logger.warning("Trading disabled (no API keys); skipping %s %s", side, symbol)
            return {"status": "skipped", "reason": "no API credentials configured"}

        # Don't open a new position if one for this pair already exists.
        existing = self._position_amt(symbol)
        if existing != 0:
            logger.info("Position already open on %s (amt=%s); skipping new %s order",
                        symbol, existing, side)
            return {"status": "skipped", "reason": "position already open",
                    "position_amt": existing}

        qty = self._order_quantity(symbol)
        if qty is None:
            logger.error("Could not determine order quantity for %s (price unavailable "
                         "or amount too small)", symbol)
            return {"status": "error", "reason": "could not determine order quantity"}

        try:
            self._ensure_leverage(symbol)
            order = self._create_order(
                symbol=symbol,
                side=side,
                type="MARKET",
                quantity=qty,
            )
            logger.info(
                "Order placed: %s %s qty=%s (%s %s) id=%s (%s)",
                side, symbol, qty, self.quantity, self.quantity_type,
                order.get("orderId"), "testnet" if self.testnet else "LIVE",
            )
            result = {"status": "ok", "order": order, "quantity": qty}
        except (BinanceAPIException, BinanceOrderException) as exc:
            logger.error("Binance order failed for %s %s: %s", side, symbol, exc)
            return {"status": "error", "reason": str(exc)}

        # Manage the position. Failures here don't undo the entry; report them.
        if self.take_profit_pct or self.stop_loss_pct:
            entry = self._entry_price(symbol)
            if entry:
                result["brackets"] = self._place_brackets(symbol, side, entry)
                result["entry_price"] = entry
            else:
                result["brackets"] = {"status": "error", "reason": "no entry price"}

        if self.max_holding_time:
            self._schedule_timeout(symbol)
            result["max_holding_time"] = self.max_holding_time

        return result


# Module-level singleton reused across requests.
trader = BinanceTrader()

def place_order(raw_symbol, side):
    """Convenience wrapper around the shared trader instance."""
    return trader.place_order(raw_symbol, side)
