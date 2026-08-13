#!/usr/bin/env python3
"""Detect bullish and bearish pin bars from Binance USD-M futures candles.

By default only the newest completed candle is evaluated.  This makes the
script suitable for a scheduled run immediately after a candle closes.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Literal
import warnings

import pandas as pd

warnings.filterwarnings("ignore", message=r"urllib3 v2 only supports OpenSSL 1\.1\.1\+.*")
import requests


BINANCE_FUTURES_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"


@dataclass(frozen=True)
class PinBarConfig:
    """Default pin-bar geometry rules, expressed relative to candle body."""

    min_range_pct: float = 0.008
    min_range_atr: float = 1.0
    min_main_wick_body_ratio: float = 3.0
    max_opposite_wick_body_ratio: float = 1.5
    # A zero-sized body makes any wick ratio mathematically meaningless.
    # This small guard rejects pure doji candles while retaining pin bars.
    min_body_range_ratio: float = 0.05
    close_end_zone_ratio: float = 0.40
    context_lookback: int = 12
    atr_period: int = 14


@dataclass(frozen=True)
class PinBarSignal:
    kind: Literal["bullish_pinbar", "bearish_pinbar"]
    open_time: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    body: float
    upper_wick: float
    lower_wick: float
    range_pct: float


def fetch_futures_klines(symbol: str, interval: str, limit: int = 2) -> pd.DataFrame:
    """Fetch completed USD-M perpetual futures candles from Binance."""
    if not 1 <= limit <= 1500:
        raise ValueError("limit must be between 1 and 1500")
    response = requests.get(
        BINANCE_FUTURES_KLINES_URL,
        params={"symbol": symbol.upper(), "interval": interval, "limit": limit},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected Binance response: {payload}")
    columns = [
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_base_volume", "taker_quote_volume", "ignore",
    ]
    data = pd.DataFrame(payload, columns=columns)
    data[["open", "high", "low", "close", "volume"]] = data[["open", "high", "low", "close", "volume"]].astype(float)
    data["open_time"] = pd.to_datetime(data["open_time"], unit="ms", utc=True)
    data["close_time"] = pd.to_datetime(data["close_time"], unit="ms", utc=True)
    return data.loc[data["close_time"] < pd.Timestamp.now(tz="UTC")].reset_index(drop=True)


def detect_pinbar(candle: pd.Series, config: PinBarConfig = PinBarConfig()) -> PinBarSignal | None:
    """Classify one closed OHLC candle as a bullish or bearish pin bar.

    Bearish: upper wick >= 3 * body, lower wick <= 1.5 * body.
    Bullish: lower wick >= 3 * body, upper wick <= 1.5 * body.
    Both must have high-low range >= 0.8% of open price.
    """
    open_, high, low, close = (float(candle[key]) for key in ("open", "high", "low", "close"))
    if not low <= min(open_, close) <= max(open_, close) <= high:
        raise ValueError("Invalid OHLC candle")
    candle_range = high - low
    if open_ <= 0 or candle_range <= 0:
        return None

    body = abs(close - open_)
    upper_wick = high - max(open_, close)
    lower_wick = min(open_, close) - low
    range_pct = candle_range / open_
    if range_pct < config.min_range_pct or body < candle_range * config.min_body_range_ratio:
        return None

    values = dict(
        open_time=candle["open_time"], open=open_, high=high, low=low, close=close,
        body=body, upper_wick=upper_wick, lower_wick=lower_wick, range_pct=range_pct,
    )
    if (
        upper_wick >= config.min_main_wick_body_ratio * body
        and lower_wick <= config.max_opposite_wick_body_ratio * body
    ):
        return PinBarSignal(kind="bearish_pinbar", **values)
    if (
        lower_wick >= config.min_main_wick_body_ratio * body
        and upper_wick <= config.max_opposite_wick_body_ratio * body
    ):
        return PinBarSignal(kind="bullish_pinbar", **values)
    return None


def add_prior_atr(data: pd.DataFrame, period: int) -> pd.DataFrame:
    """Add Wilder ATR shifted by one candle to avoid using the tested bar."""
    result = data.copy()
    previous_close = result["close"].shift(1)
    true_range = pd.concat(
        [
            result["high"] - result["low"],
            (result["high"] - previous_close).abs(),
            (result["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    result["prior_atr"] = true_range.ewm(alpha=1 / period, adjust=False).mean().shift(1)
    return result


def detect_latest_pinbar(data: pd.DataFrame, config: PinBarConfig = PinBarConfig()) -> PinBarSignal | None:
    """Detect a pin bar on the final candle with trend, position and ATR filters.

    The final candle must be the highest (bearish) or lowest (bullish) point in
    the preceding ``context_lookback`` candles.  Its range must satisfy both
    the percentage and the preceding ATR requirement, while its close must be
    in the appropriate end 30% of its range.
    """
    minimum_candles = max(config.context_lookback, config.atr_period) + 1
    if len(data) < minimum_candles:
        raise ValueError(f"Need at least {minimum_candles} closed candles for context and ATR filters")
    enriched = add_prior_atr(data, config.atr_period)
    candle = enriched.iloc[-1]
    signal = detect_pinbar(candle, config)
    if signal is None:
        return None

    candle_range = signal.high - signal.low
    prior_atr = candle["prior_atr"]
    if not pd.notna(prior_atr) or candle_range < config.min_range_atr * float(prior_atr):
        return None
    previous = enriched.iloc[-1 - config.context_lookback:-1]
    if signal.kind == "bullish_pinbar":
        close_is_near_high = signal.close >= signal.high - config.close_end_zone_ratio * candle_range
        is_context_low = signal.low <= previous["low"].min()
        if not (close_is_near_high and is_context_low):
            return None
    else:
        close_is_near_low = signal.close <= signal.low + config.close_end_zone_ratio * candle_range
        is_context_high = signal.high >= previous["high"].max()
        if not (close_is_near_low and is_context_high):
            return None
    return signal


def select_historical_candle(data: pd.DataFrame, backtest_offset: int = 0, as_of: str | None = None) -> pd.Series:
    """Select the newest or a historical closed candle without future access.

    ``backtest_offset=9`` means exclude the nine newest closed candles and
    test the tenth-newest one.  ``as_of`` takes an exact UTC candle open time
    (for example ``2026-08-13T06:00:00Z``) and is useful when the desired
    chart timestamp matters more than its current position in the data.
    """
    if backtest_offset < 0:
        raise ValueError("backtest_offset must be zero or greater")
    if as_of is not None and backtest_offset:
        raise ValueError("Use either --backtest-offset or --as-of, not both")
    if as_of is not None:
        target = pd.Timestamp(as_of)
        if target.tzinfo is None:
            raise ValueError("--as-of must include a timezone, e.g. 2026-08-13T06:00:00Z")
        matched = data.loc[data["open_time"] == target.tz_convert("UTC")]
        if matched.empty:
            raise ValueError(f"No closed candle found for --as-of {as_of}")
        return matched.iloc[0]
    if backtest_offset >= len(data):
        raise ValueError("--backtest-offset is larger than available closed candles")
    return data.iloc[-1 - backtest_offset]


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect a pin bar on the newest Binance USD-M candle.")
    parser.add_argument("--symbol", default="SOLUSDT", help="USD-M perpetual symbol, e.g. SOLUSDT")
    parser.add_argument("--interval", default="1h", help="Binance interval, e.g. 1h or 4h")
    parser.add_argument(
        "--backtest-offset", type=int, default=0,
        help="Exclude this many newest closed candles; 9 checks the 10th-newest closed candle",
    )
    parser.add_argument("--as-of", help="Exact UTC candle open time, e.g. 2026-08-13T06:00:00Z")
    args = parser.parse_args()
    if args.backtest_offset < 0:
        parser.error("--backtest-offset must be zero or greater")
    if args.as_of and args.backtest_offset:
        parser.error("Use either --backtest-offset or --as-of, not both")

    config = PinBarConfig()
    # ATR/context filters require sufficient candles before the tested candle.
    required_history = max(config.context_lookback, config.atr_period) + 1
    # For an exact historical timestamp Binance needs enough data to include
    # it, so request the normal analysis window rather than just two candles.
    limit = 1000 if args.as_of else args.backtest_offset + required_history + 1
    data = fetch_futures_klines(args.symbol, args.interval, limit)
    try:
        candle = select_historical_candle(data, args.backtest_offset, args.as_of)
    except ValueError as error:
        parser.error(str(error))
    candle_position = int(data.index[data["open_time"] == candle["open_time"]][0])
    signal = detect_latest_pinbar(data.iloc[:candle_position + 1].reset_index(drop=True), config)
    print(f"symbol={args.symbol.upper()} interval={args.interval} candle={candle.open_time:%Y-%m-%d %H:%M} UTC")
    if not signal:
        print("No pin bar after geometry, close-position, 12-candle context, and ATR filters.")
        return
    print(
        f"{signal.kind} | O={signal.open:.6g} H={signal.high:.6g} L={signal.low:.6g} C={signal.close:.6g} | "
        f"range={signal.range_pct:.2%} body={signal.body:.6g} "
        f"upper_wick={signal.upper_wick:.6g} lower_wick={signal.lower_wick:.6g}"
    )


if __name__ == "__main__":
    main()
