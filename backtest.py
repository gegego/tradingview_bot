"""Backtester for TradingView flash-crash alert entries.

Reads predefined entry signals from ``alerts.csv`` (produced by
``webhook_lisener/webhook_listener.py``) and simulates each entry under
configurable take-profit / stop-loss / max-holding-time exit rules.

Future price data needed to evaluate the exits is fetched on demand via ``ccxt``
from Binance USDT-M futures (``binanceusdm``) and cached to disk so repeated
parameter sweeps don't refetch.

Usage:
    pip install ccxt pandas
    python backtest.py

Outputs:
    - trade_results_long.csv  / trade_results_short.csv  (per-trade log)
    - console summary report for each direction
"""

from __future__ import annotations

import os
import sys
import time
from zoneinfo import ZoneInfo

import pandas as pd

try:
    import ccxt
except ImportError:  # pragma: no cover - guidance for first run
    sys.exit("ccxt is required: pip install ccxt pandas")


# --------------------------------------------------------------------------- #
# Configuration -- tweak these to optimize / re-run the strategy.
# --------------------------------------------------------------------------- #
INPUT_CSV = "alerts.csv"
TAKE_PROFIT_PCT = 5.0          # take profit, percent of entry price
STOP_LOSS_PCT = 10.0            # stop loss, percent of entry price
MAX_HOLDING = "3h"            # pandas-parseable timedelta (e.g. "6h", "90min")
DIRECTIONS = ["long", "short"]  # which side(s) to simulate
TIMEFRAME = "1m"               # OHLCV granularity used to walk each trade
SOURCE_TZ = "Europe/Berlin"    # timezone of the naive `received_at` column
EXCHANGE_ID = "binanceusdm"    # ccxt exchange id (Binance USDT-M perpetuals)
FEE_PCT = 0.0                  # per-side fee in %, deducted from each trade PnL
CACHE_DIR = "ohlcv_cache"
FETCH_LIMIT = 1000             # bars per ccxt fetch_ohlcv page
MAKE_CHARTS = True             # write one interactive K-line HTML per pair
CHARTS_DIR = "charts"

# Exit reasons that did NOT result in a real, data-backed position. These are
# excluded from performance metrics and from the chart overlays.
NON_TRADED_REASONS = {"NO_DATA", "SKIPPED"}


# --------------------------------------------------------------------------- #
# 1. Load and normalize the entry signals.
# --------------------------------------------------------------------------- #
def load_alerts(path: str) -> pd.DataFrame:
    """Load alerts CSV, convert `received_at` (naive local) -> UTC, de-dupe."""
    if not os.path.exists(path):
        sys.exit(f"Input file not found: {path}")

    df = pd.read_csv(path)
    missing = {"received_at", "pair", "close"} - set(df.columns)
    if missing:
        sys.exit(f"Input CSV is missing required columns: {sorted(missing)}")

    # Drop exact duplicate alerts (the raw feed contains a few).
    df = df.drop_duplicates(subset=["received_at", "pair", "close"]).copy()

    # Parse the naive local timestamp, attach the source tz (DST-aware) and
    # convert to UTC so it lines up with exchange OHLCV (which is UTC).
    naive = pd.to_datetime(df["received_at"], errors="coerce")
    df["entry_time_utc"] = naive.dt.tz_localize(
        ZoneInfo(SOURCE_TZ), ambiguous="NaT", nonexistent="shift_forward"
    ).dt.tz_convert("UTC")

    # Drop rows we couldn't parse (bad timestamp or non-numeric price).
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    bad = df["entry_time_utc"].isna() | df["close"].isna()
    if bad.any():
        print(f"[warn] dropping {int(bad.sum())} alert(s) with bad time/price")
    df = df[~bad].reset_index(drop=True)

    return df[["entry_time_utc", "pair", "close"]]


# --------------------------------------------------------------------------- #
# 2. Map TradingView perp tickers -> ccxt symbols.
# --------------------------------------------------------------------------- #
def tv_to_ccxt_symbol(pair: str) -> str | None:
    """`MAGMAUSDT.P` -> `MAGMA/USDT:USDT`. Returns None if not recognized."""
    if not isinstance(pair, str):
        return None
    p = pair.strip().upper()
    if p.endswith(".P"):
        p = p[:-2]
    # Only USDT-quoted perps are handled here.
    if not p.endswith("USDT"):
        return None
    base = p[: -len("USDT")]
    if not base:
        return None
    return f"{base}/USDT:USDT"


# --------------------------------------------------------------------------- #
# 3. Fetch (and cache) OHLCV for one symbol over a time window.
# --------------------------------------------------------------------------- #
def _cache_path(base_key: str) -> str:
    safe = base_key.replace("/", "_").replace(":", "_")
    return os.path.join(CACHE_DIR, f"{safe}_{TIMEFRAME}.csv")


def fetch_ohlcv(exchange, symbol: str, since_ms: int, until_ms: int) -> pd.DataFrame:
    """Return a UTC-indexed OHLCV DataFrame covering [since_ms, until_ms].

    Results are cached per symbol+timeframe. If the cache already spans the
    requested window it is reused; otherwise we (re)fetch the full range.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(symbol)

    if os.path.exists(path):
        cached = pd.read_csv(path, parse_dates=["timestamp"])
        cached["timestamp"] = pd.to_datetime(cached["timestamp"], utc=True)
        if not cached.empty:
            have_from = cached["timestamp"].iloc[0].value // 10**6
            have_to = cached["timestamp"].iloc[-1].value // 10**6
            if have_from <= since_ms and have_to >= until_ms:
                return cached

    # Paginate forward until we pass `until_ms` (or the exchange runs dry).
    rows: list[list] = []
    cursor = since_ms
    tf_ms = exchange.parse_timeframe(TIMEFRAME) * 1000
    while cursor <= until_ms:
        batch = exchange.fetch_ohlcv(symbol, TIMEFRAME, since=cursor, limit=FETCH_LIMIT)
        if not batch:
            break
        rows.extend(batch)
        cursor = batch[-1][0] + tf_ms
        if len(batch) < FETCH_LIMIT:
            break
        time.sleep(exchange.rateLimit / 1000.0)

    if not rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp")
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.to_csv(path, index=False)
    return df


# --------------------------------------------------------------------------- #
# 4. Simulate a single trade bar-by-bar.
# --------------------------------------------------------------------------- #
def simulate_trade(ohlcv: pd.DataFrame, entry_time, entry_price: float,
                   direction: str, tp_pct: float, sl_pct: float,
                   max_hold: pd.Timedelta):
    """Walk forward from the bar after `entry_time` and resolve the exit.

    Returns (exit_time, exit_price, reason). `reason` is one of
    TP / SL / TIMEOUT / NO_DATA. On bars where both TP and SL are touchable we
    conservatively assume the stop loss fills first.
    """
    if ohlcv is None or ohlcv.empty:
        return None, None, "NO_DATA"

    window_end = entry_time + max_hold
    # Start on the first *fully future* bar to avoid lookahead on the entry bar.
    mask = (ohlcv["timestamp"] > entry_time) & (ohlcv["timestamp"] <= window_end)
    bars = ohlcv[mask]
    if bars.empty:
        return None, None, "NO_DATA"

    tp = tp_pct / 100.0
    sl = sl_pct / 100.0
    if direction == "long":
        tp_level = entry_price * (1 + tp)
        sl_level = entry_price * (1 - sl)
    else:  # short
        tp_level = entry_price * (1 - tp)
        sl_level = entry_price * (1 + sl)

    for ts, high, low in zip(bars["timestamp"], bars["high"], bars["low"]):
        if direction == "long":
            hit_sl = low <= sl_level
            hit_tp = high >= tp_level
        else:
            hit_sl = high >= sl_level
            hit_tp = low <= tp_level
        # Conservative: if both could fill in the same bar, take the stop loss.
        if hit_sl:
            return ts, sl_level, "SL"
        if hit_tp:
            return ts, tp_level, "TP"

    # Neither level reached within the holding window -> close at last bar.
    last = bars.iloc[-1]
    return last["timestamp"], float(last["close"]), "TIMEOUT"


# --------------------------------------------------------------------------- #
# 5. PnL.
# --------------------------------------------------------------------------- #
def compute_pnl(entry: float, exit_price: float, direction: str) -> float:
    """Percent PnL for the trade, net of round-trip fees."""
    if direction == "long":
        gross = (exit_price - entry) / entry * 100.0
    else:
        gross = (entry - exit_price) / entry * 100.0
    return round(gross - 2 * FEE_PCT, 4)


# --------------------------------------------------------------------------- #
# 6. Run one direction over all alerts.
# --------------------------------------------------------------------------- #
def run_backtest(alerts: pd.DataFrame, ohlcv_by_pair: dict, direction: str,
                 max_hold: pd.Timedelta) -> pd.DataFrame:
    records = []
    # Track when the open position on each pair closes. A new alert that arrives
    # while that pair's position is still open is skipped (no pyramiding).
    open_until: dict[str, pd.Timestamp] = {}
    # Process chronologically so "is a position already open?" is well-defined.
    for _, row in alerts.sort_values("entry_time_utc").iterrows():
        pair = row["pair"]
        held_until = open_until.get(pair)
        if held_until is not None and row["entry_time_utc"] < held_until:
            # A position on this pair is still open -> don't open a new one.
            records.append({
                "Entry Time": row["entry_time_utc"], "Pair": pair,
                "Direction": direction, "Entry Price": row["close"],
                "Exit Time": None, "Exit Price": None, "PnL (%)": None,
                "Exit Reason": "SKIPPED",
            })
            continue

        ohlcv = ohlcv_by_pair.get(pair)
        exit_time, exit_price, reason = simulate_trade(
            ohlcv, row["entry_time_utc"], row["close"], direction,
            TAKE_PROFIT_PCT, STOP_LOSS_PCT, max_hold,
        )
        # Only a real (data-backed) position blocks subsequent alerts.
        if exit_time is not None:
            open_until[pair] = exit_time
        pnl = (compute_pnl(row["close"], exit_price, direction)
               if exit_price is not None else None)
        records.append({
            "Entry Time": row["entry_time_utc"],
            "Pair": pair,
            "Direction": direction,
            "Entry Price": row["close"],
            "Exit Time": exit_time,
            "Exit Price": exit_price,
            "PnL (%)": pnl,
            "Exit Reason": reason,
        })
    return pd.DataFrame(records)


# --------------------------------------------------------------------------- #
# 7. Summary report.
# --------------------------------------------------------------------------- #
def _max_drawdown(pnl_series: pd.Series) -> float:
    """Max peak-to-trough drop of the cumulative-PnL equity curve (in %)."""
    if pnl_series.empty:
        return 0.0
    equity = pnl_series.cumsum()
    running_peak = equity.cummax()
    return float((equity - running_peak).min())  # most negative == worst dd


def summarize(results: pd.DataFrame, direction: str) -> None:
    print(f"\n{'=' * 52}")
    print(f" BACKTEST SUMMARY  --  {direction.upper()}")
    print(f"{'=' * 52}")

    reason_counts = results["Exit Reason"].value_counts().to_dict()

    # Metrics are computed only over real positions (data-backed, not skipped).
    traded = results[~results["Exit Reason"].isin(NON_TRADED_REASONS)].copy()
    traded = traded.sort_values("Entry Time")
    pnl = traded["PnL (%)"].dropna()

    total = len(traded)
    skipped = int((results["Exit Reason"] == "SKIPPED").sum())
    no_data = int((results["Exit Reason"] == "NO_DATA").sum())

    if total == 0:
        print(" No tradable signals (no price data fetched).")
        if no_data:
            print(f" Skipped (NO_DATA): {no_data}")
        return

    wins = int((pnl > 0).sum())
    win_rate = wins / total * 100.0
    total_pnl = float(pnl.sum())
    avg_pnl = float(pnl.mean())
    max_dd = _max_drawdown(pnl)

    print(f" Total Trades        : {total}")
    print(f" Win Rate            : {win_rate:6.2f}%  ({wins}/{total})")
    print(f" Total PnL           : {total_pnl:+7.2f}%")
    print(f" Avg Profit / Trade  : {avg_pnl:+7.2f}%")
    print(f" Max Drawdown        : {max_dd:7.2f}%")
    print(f" Exit reasons        : "
          + ", ".join(f"{k}={v}" for k, v in sorted(reason_counts.items())))
    if skipped:
        print(f" Skipped (position already open) : {skipped}")
    excluded = skipped + no_data
    if excluded:
        print(f" (excluded {excluded} non-traded signal(s) from metrics)")


# --------------------------------------------------------------------------- #
# 8. K-line (candlestick) charts -- one interactive HTML per pair.
# --------------------------------------------------------------------------- #
# Exit markers are colored by reason; both directions live on one chart and can
# be toggled via the legend.
_REASON_COLOR = {"TP": "#2ca02c", "SL": "#d62728", "TIMEOUT": "#7f7f7f"}
_DIR_LINE_COLOR = {"long": "#1f77b4", "short": "#ff7f0e"}


def plot_pair_charts(ohlcv_by_pair: dict, results_by_dir: dict) -> None:
    """Render one candlestick chart per pair with all trade operations marked.

    `results_by_dir` maps direction -> results DataFrame (from run_backtest).
    Each chart shows: the K-lines, entry markers (shared), and per-direction
    exit markers + entry->exit connector lines.
    """
    import plotly.graph_objects as go

    os.makedirs(CHARTS_DIR, exist_ok=True)

    for pair, ohlcv in ohlcv_by_pair.items():
        if ohlcv is None or ohlcv.empty:
            continue

        fig = go.Figure()
        fig.add_trace(go.Candlestick(
            x=ohlcv["timestamp"], open=ohlcv["open"], high=ohlcv["high"],
            low=ohlcv["low"], close=ohlcv["close"], name="Price",
            increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
        ))

        # Entry markers are identical across directions -> draw once.
        any_res = next(iter(results_by_dir.values()))
        entries = any_res[(any_res["Pair"] == pair)
                          & ~any_res["Exit Reason"].isin(NON_TRADED_REASONS)]
        if not entries.empty:
            fig.add_trace(go.Scatter(
                x=entries["Entry Time"], y=entries["Entry Price"],
                mode="markers", name="Entry",
                marker=dict(symbol="circle", size=8, color="white",
                            line=dict(color="black", width=1.5)),
                hovertemplate="Entry %{y}<br>%{x}<extra></extra>",
            ))

        # Per-direction exits and connector lines.
        for direction, res in results_by_dir.items():
            trades = res[(res["Pair"] == pair)
                         & ~res["Exit Reason"].isin(NON_TRADED_REASONS)
                         & res["Exit Price"].notna()]
            if trades.empty:
                continue

            # Connector line from each entry to its exit (one trace, gaps via None).
            lx, ly = [], []
            for _, t in trades.iterrows():
                lx += [t["Entry Time"], t["Exit Time"], None]
                ly += [t["Entry Price"], t["Exit Price"], None]
            fig.add_trace(go.Scatter(
                x=lx, y=ly, mode="lines", name=f"{direction} trades",
                line=dict(color=_DIR_LINE_COLOR[direction], width=1),
                opacity=0.5, hoverinfo="skip",
            ))

            # Exit markers, colored by exit reason.
            sym = "triangle-up" if direction == "long" else "triangle-down"
            fig.add_trace(go.Scatter(
                x=trades["Exit Time"], y=trades["Exit Price"], mode="markers",
                name=f"{direction} exit",
                marker=dict(
                    symbol=sym, size=10,
                    color=[_REASON_COLOR.get(r, "#7f7f7f") for r in trades["Exit Reason"]],
                    line=dict(color="black", width=0.5),
                ),
                customdata=trades[["Exit Reason", "PnL (%)"]].values,
                hovertemplate=(f"{direction} exit %{{y}}<br>%{{x}}"
                               "<br>%{customdata[0]}  PnL %{customdata[1]:.2f}%"
                               "<extra></extra>"),
            ))

        fig.update_layout(
            title=f"{pair} -- entries & exits  (TP {TAKE_PROFIT_PCT}% / "
                  f"SL {STOP_LOSS_PCT}% / {MAX_HOLDING})",
            xaxis_title="Time (UTC)", yaxis_title="Price",
            xaxis_rangeslider_visible=False, template="plotly_dark",
            hovermode="closest",
        )
        out = os.path.join(CHARTS_DIR, f"{pair.replace('/', '_')}.html")
        fig.write_html(out)
        print(f"  chart: {out}")


# --------------------------------------------------------------------------- #
# 9. Main.
# --------------------------------------------------------------------------- #
def main() -> None:
    alerts = load_alerts(INPUT_CSV)
    print(f"Loaded {len(alerts)} unique alert(s) from {INPUT_CSV}.")

    max_hold = pd.Timedelta(MAX_HOLDING)
    exchange = getattr(ccxt, EXCHANGE_ID)({"enableRateLimit": True})

    # Fetch OHLCV once per unique pair, covering all of its entries + holding.
    ohlcv_by_pair: dict[str, pd.DataFrame] = {}
    for pair, grp in alerts.groupby("pair"):
        symbol = tv_to_ccxt_symbol(pair)
        if symbol is None:
            print(f"[warn] {pair}: unrecognized symbol format, skipping")
            ohlcv_by_pair[pair] = pd.DataFrame()
            continue
        since_ms = int(grp["entry_time_utc"].min().value // 10**6)
        until_ms = int((grp["entry_time_utc"].max() + max_hold).value // 10**6)
        try:
            df = fetch_ohlcv(exchange, symbol, since_ms, until_ms)
            if df.empty:
                print(f"[warn] {pair} ({symbol}): no OHLCV returned")
            else:
                print(f"  {pair:>22} -> {symbol:<18} {len(df)} bars")
            ohlcv_by_pair[pair] = df
        except ccxt.BadSymbol:
            print(f"[warn] {pair} ({symbol}): not listed on {EXCHANGE_ID}")
            ohlcv_by_pair[pair] = pd.DataFrame()
        except Exception as exc:  # network / rate-limit / etc.
            print(f"[warn] {pair} ({symbol}): fetch failed: {exc}")
            ohlcv_by_pair[pair] = pd.DataFrame()

    # Run each requested direction and write its own results file.
    results_by_dir = {}
    for direction in DIRECTIONS:
        results = run_backtest(alerts, ohlcv_by_pair, direction, max_hold)
        results_by_dir[direction] = results
        out_path = f"trade_results_{direction}.csv"
        results.to_csv(out_path, index=False)
        print(f"\nWrote {out_path} ({len(results)} rows).")
        summarize(results, direction)

    # Per-pair K-line charts with the trade operations overlaid.
    if MAKE_CHARTS:
        print(f"\nWriting K-line charts to {CHARTS_DIR}/ ...")
        plot_pair_charts(ohlcv_by_pair, results_by_dir)


if __name__ == "__main__":
    main()
