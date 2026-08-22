#!/usr/bin/env python3
"""Detect regular MACD divergences from Binance USD-M futures candles.

Examples
--------
python macd_divergence.py --symbol SOLUSDT --interval 1h --limit 1000
python macd_divergence.py --symbol BTCUSDT --interval 4h --kind bearish
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
import warnings
import os
import time

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ModuleNotFoundError:  # Ubuntu 20.04 commonly ships Python 3.8.
    from backports.zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# macOS's system Python links against LibreSSL and urllib3 v2 emits this
# compatibility warning when ``requests`` imports urllib3.  It does not affect
# this public HTTPS request; suppress only this exact warning, not all warnings.
warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL 1\.1\.1\+.*",
)
import requests


BINANCE_FUTURES_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"


@dataclass(frozen=True)
class DetectorConfig:
    """Filtering parameters. All amplitude ratios are relative to ATR."""

    fast_period: int = 12
    slow_period: int = 26
    signal_period: int = 9
    atr_period: int = 14
    max_price_macd_offset: int = 8
    min_bars_between: int = 20
    max_bars_between: int = 120
    min_price_change_pct: float = 0.005
    min_price_divergence_atr: float = 1.25
    min_price_swing_atr: float = 1.25
    min_macd_value_atr: float = 0.15
    min_macd_drop_pct: float = 0.12
    min_macd_swing_atr: float = 0.10
    swing_lookback: int = 12


@dataclass(frozen=True)
class Divergence:
    kind: Literal["bearish", "bullish"]
    # ``first_time``/``second_time`` are price-extreme timestamps. MACD can
    # reach its extrema a few candles earlier/later, so preserve both times.
    first_time: pd.Timestamp
    second_time: pd.Timestamp
    first_macd_time: pd.Timestamp
    second_macd_time: pd.Timestamp
    first_price: float
    second_price: float
    first_macd: float
    second_macd: float
    bars_between: int
    confirmation_time: pd.Timestamp
    macd_cross_time: pd.Timestamp


@dataclass(frozen=True)
class MonitorConfig:
    """Runtime settings loaded from the local .env file."""

    symbols: tuple[str, ...]
    intervals: tuple[str, ...]
    close_delay_minutes: int
    kline_limit: int
    display_timezone: str
    check_on_start: bool
    telegram_enabled: bool
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    telegram_mention: str


def _parse_csv(value: str, name: str, *, uppercase: bool = False) -> tuple[str, ...]:
    items = tuple(
        item.strip().upper() if uppercase else item.strip().lower()
        for item in value.split(",") if item.strip()
    )
    if not items:
        raise ValueError(f"{name} must contain at least one value")
    return items


def _parse_bool(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def load_env_file(path: str | Path) -> dict[str, str]:
    """Read a small dependency-free KEY=VALUE .env file."""
    env_path = Path(path)
    if not env_path.is_file():
        raise FileNotFoundError(f"Config file not found: {env_path}. Copy .env.example to .env first.")
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"Invalid .env line {line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key:
            raise ValueError(f"Invalid .env line {line_number}: empty key")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def load_monitor_config(path: str | Path) -> MonitorConfig:
    """Load and validate monitoring settings from a .env file."""
    values = load_env_file(path)

    def required(name: str) -> str:
        value = values.get(name, os.environ.get(name, "")).strip()
        if not value:
            raise ValueError(f"Missing required setting: {name}")
        return value

    intervals = _parse_csv(required("INTERVALS"), "INTERVALS")
    for interval in intervals:
        interval_to_seconds(interval)  # Validate Binance-like interval syntax early.
    delay = int(required("CLOSE_DELAY_MINUTES"))
    limit = int(required("KLINE_LIMIT"))
    if delay < 0:
        raise ValueError("CLOSE_DELAY_MINUTES must be zero or greater")
    if not 1 <= limit <= 1500:
        raise ValueError("KLINE_LIMIT must be between 1 and 1500")
    timezone_name = values.get("DISPLAY_TIMEZONE", os.environ.get("DISPLAY_TIMEZONE", "Asia/Taipei"))
    ZoneInfo(timezone_name)  # Validate it before monitoring begins.
    telegram_enabled = _parse_bool(
        values.get("TELEGRAM_ENABLED", os.environ.get("TELEGRAM_ENABLED", "false")),
        "TELEGRAM_ENABLED",
    )
    bot_token = values.get("TELEGRAM_BOT_TOKEN", os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip() or None
    chat_id = values.get("TELEGRAM_CHAT_ID", os.environ.get("TELEGRAM_CHAT_ID", "")).strip() or None
    if telegram_enabled and (not bot_token or not chat_id):
        raise ValueError("TELEGRAM_ENABLED=true requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    return MonitorConfig(
        symbols=_parse_csv(required("SYMBOLS"), "SYMBOLS", uppercase=True),
        intervals=intervals,
        close_delay_minutes=delay,
        kline_limit=limit,
        display_timezone=timezone_name,
        check_on_start=_parse_bool(values.get("CHECK_ON_START", os.environ.get("CHECK_ON_START", "false")), "CHECK_ON_START"),
        telegram_enabled=telegram_enabled,
        telegram_bot_token=bot_token,
        telegram_chat_id=chat_id,
        telegram_mention=values.get("TELEGRAM_MENTION", os.environ.get("TELEGRAM_MENTION", "")).strip(),
    )


def fetch_futures_klines(symbol: str, interval: str, limit: int = 1000) -> pd.DataFrame:
    """Fetch completed USD-M perpetual futures candles from Binance's public API."""
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
    frame = pd.DataFrame(payload, columns=columns)
    frame[["open", "high", "low", "close", "volume"]] = frame[["open", "high", "low", "close", "volume"]].astype(float)
    frame["open_time"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    frame["close_time"] = pd.to_datetime(frame["close_time"], unit="ms", utc=True)

    # The final Binance kline may still be in progress.  Excluding it makes the
    # result non-repainting: both pivots and their confirmation candles closed.
    return frame.loc[frame["close_time"] < pd.Timestamp.now(tz="UTC")].reset_index(drop=True)


def add_indicators(frame: pd.DataFrame, config: DetectorConfig) -> pd.DataFrame:
    """Return a copy containing MACD line, signal, histogram, and Wilder ATR."""
    data = frame.copy()
    close = data["close"]
    data["macd"] = close.ewm(span=config.fast_period, adjust=False).mean() - close.ewm(span=config.slow_period, adjust=False).mean()
    data["macd_signal"] = data["macd"].ewm(span=config.signal_period, adjust=False).mean()
    data["macd_hist"] = data["macd"] - data["macd_signal"]

    previous_close = close.shift()
    true_range = pd.concat(
        [data["high"] - data["low"], (data["high"] - previous_close).abs(), (data["low"] - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    data["atr"] = true_range.ewm(alpha=1 / config.atr_period, adjust=False).mean()
    return data


def simulate_historical_as_of(frame: pd.DataFrame, backtest_offset: int) -> pd.DataFrame:
    """Hide the newest closed candles and return the data available at that time.

    For example, ``backtest_offset=76`` treats the candle 76 periods ago as
    the latest candle.  Indicators are calculated only after this truncation,
    so neither MACD nor ATR can see data after the simulated historical time.
    """
    if backtest_offset < 0:
        raise ValueError("backtest_offset must be zero or greater")
    if backtest_offset == 0:
        return frame.copy()
    if backtest_offset >= len(frame):
        raise ValueError(
            f"backtest_offset ({backtest_offset}) must be smaller than the "
            f"number of closed candles ({len(frame)})"
        )
    return frame.iloc[:-backtest_offset].copy().reset_index(drop=True)


def simulate_historical_at_time(frame: pd.DataFrame, as_of: str) -> pd.DataFrame:
    """Truncate at an exact timezone-aware candle open time, without future data.

    Examples: ``2026-08-12T22:00:00Z`` and
    ``2026-08-13T06:00:00+08:00`` select the same candle.
    """
    target = pd.Timestamp(as_of)
    if target.tzinfo is None:
        raise ValueError("--as-of must include a timezone, e.g. 2026-08-12T22:00:00Z")
    target = target.tz_convert("UTC")
    matches = frame.index[frame["open_time"] == target]
    if matches.empty:
        raise ValueError(
            f"No closed candle found for --as-of {as_of}; "
            f"available {frame['open_time'].min()} to {frame['open_time'].max()}"
        )
    return frame.iloc[:matches[0] + 1].copy().reset_index(drop=True)


def _cross_indices(data: pd.DataFrame, kind: Literal["death", "golden"]) -> list[int]:
    """Return closed-candle MACD/Signal crossover indices."""
    macd, signal = data["macd"].to_numpy(), data["macd_signal"].to_numpy()
    result: list[int] = []
    for index in range(1, len(data)):
        if not all(np.isfinite((macd[index - 1], signal[index - 1], macd[index], signal[index]))):
            continue
        if kind == "death" and macd[index - 1] >= signal[index - 1] and macd[index] < signal[index]:
            result.append(index)
        elif kind == "golden" and macd[index - 1] <= signal[index - 1] and macd[index] > signal[index]:
            result.append(index)
    return result


def _last_between(indices: list[int], start: int, end: int) -> int | None:
    """Return the final crossover strictly inside a lobe boundary."""
    matches = [index for index in indices if start < index < end]
    return matches[-1] if matches else None


def _extreme_index(data: pd.DataFrame, start: int, end: int, column: Literal["high", "low"], kind: Literal["high", "low"]) -> int:
    values = data.loc[start:end, column]
    return int(values.idxmax() if kind == "high" else values.idxmin())


def _price_swing_is_large(data: pd.DataFrame, index: int, kind: Literal["bearish", "bullish"], config: DetectorConfig) -> bool:
    """Reject minor candle wiggles with a local price swing measured in ATR."""
    start, atr = max(0, index - config.swing_lookback), data.at[index, "atr"]
    if not np.isfinite(atr) or atr <= 0:
        return False
    if kind == "bearish":
        swing = data.at[index, "high"] - data.loc[start:index, "low"].min()
    else:
        swing = data.loc[start:index, "high"].max() - data.at[index, "low"]
    return swing >= config.min_price_swing_atr * atr


def _macd_swing_is_large(data: pd.DataFrame, index: int, kind: Literal["bearish", "bullish"], config: DetectorConfig) -> bool:
    """Require a visible MACD lobe, not a tiny oscillation around zero."""
    start, atr, macd = max(0, index - config.swing_lookback), data.at[index, "atr"], data.at[index, "macd"]
    if not np.isfinite(atr) or atr <= 0:
        return False
    if kind == "bearish":
        swing = macd - data.loc[start:index, "macd"].min()
        return macd >= config.min_macd_value_atr * atr and swing >= config.min_macd_swing_atr * atr
    swing = data.loc[start:index, "macd"].max() - macd
    return -macd >= config.min_macd_value_atr * atr and swing >= config.min_macd_swing_atr * atr


def _price_divergence_is_large(
    data: pd.DataFrame,
    first_index: int,
    second_index: int,
    kind: Literal["bearish", "bullish"],
    config: DetectorConfig,
) -> bool:
    """Require a meaningful distance between the two price extremes.

    A percentage-only threshold is unreliable across different volatility
    regimes.  The move must clear both a small percentage threshold and an
    ATR threshold measured at the two endpoints.
    """
    column = "high" if kind == "bearish" else "low"
    first, second = data.at[first_index, column], data.at[second_index, column]
    atr = max(data.at[first_index, "atr"], data.at[second_index, "atr"])
    if not np.isfinite(atr) or atr <= 0:
        return False
    if kind == "bearish":
        percentage_move = second / first - 1
    else:
        percentage_move = first / second - 1
    return (
        percentage_move >= config.min_price_change_pct
        and abs(second - first) >= config.min_price_divergence_atr * atr
    )


def _build_divergence(
    data: pd.DataFrame,
    config: DetectorConfig,
    kind: Literal["bearish", "bullish"],
    first_lobe: tuple[int, int],
    second_lobe: tuple[int, int],
    confirmation_index: int,
) -> Divergence | None:
    """Validate one pair of MACD lobes and create a signal at a crossover."""
    first_start, first_end = first_lobe
    second_start, second_end = second_lobe
    price_column = "high" if kind == "bearish" else "low"
    macd_kind = "high" if kind == "bearish" else "low"

    m1_index = _extreme_index(data, first_start, first_end, "macd", macd_kind)
    m2_index = _extreme_index(data, second_start, second_end, "macd", macd_kind)
    # The price top/bottom is selected from the full MACD lobe up to its
    # crossover confirmation, rather than assuming the crossover candle is it.
    p1_index = _extreme_index(data, first_start, first_end, price_column, macd_kind)
    p2_index = _extreme_index(data, second_start, confirmation_index, price_column, macd_kind)
    # The requested 20-period spacing applies to the two price peaks/lows,
    # rather than to the MACD extrema which may lead or lag price.
    bars = p2_index - p1_index

    if not config.min_bars_between <= bars <= config.max_bars_between:
        return None
    if abs(p1_index - m1_index) > config.max_price_macd_offset:
        return None
    if abs(p2_index - m2_index) > config.max_price_macd_offset:
        return None
    if not all((
        _price_swing_is_large(data, p1_index, kind, config),
        _price_swing_is_large(data, p2_index, kind, config),
        _macd_swing_is_large(data, m1_index, kind, config),
        _macd_swing_is_large(data, m2_index, kind, config),
    )):
        return None

    p1, p2 = data.at[p1_index, price_column], data.at[p2_index, price_column]
    m1, m2 = data.at[m1_index, "macd"], data.at[m2_index, "macd"]
    macd_until_confirmation = data.loc[m1_index:confirmation_index, "macd"]

    # The second selected price extreme must be the final highest/lowest point
    # in the complete structure. This prevents connecting two peaks while a
    # higher high (or lower low) is hidden between them.
    structure_extreme = _extreme_index(data, p1_index, confirmation_index, price_column, macd_kind)
    if structure_extreme != p2_index:
        return None

    if kind == "bearish":
        price_diverges = _price_divergence_is_large(data, p1_index, p2_index, kind, config)
        macd_diverges = m2 <= m1 * (1 - config.min_macd_drop_pct)
        stays_on_correct_side = macd_until_confirmation.min() >= 0
        crossover_is_on_correct_side = data.at[confirmation_index, "macd"] >= 0
    else:
        price_diverges = _price_divergence_is_large(data, p1_index, p2_index, kind, config)
        macd_diverges = m2 >= m1 * (1 - config.min_macd_drop_pct)
        stays_on_correct_side = macd_until_confirmation.max() <= 0
        crossover_is_on_correct_side = data.at[confirmation_index, "macd"] <= 0
    if not (price_diverges and macd_diverges and stays_on_correct_side and crossover_is_on_correct_side):
        return None

    return Divergence(
        kind=kind,
        first_time=data.at[p1_index, "open_time"],
        second_time=data.at[p2_index, "open_time"],
        first_macd_time=data.at[m1_index, "open_time"],
        second_macd_time=data.at[m2_index, "open_time"],
        first_price=float(p1), second_price=float(p2),
        first_macd=float(m1), second_macd=float(m2), bars_between=bars,
        confirmation_time=data.at[confirmation_index, "open_time"],
        macd_cross_time=data.at[confirmation_index, "open_time"],
    )


def _build_bullish_recovery_divergence(
    data: pd.DataFrame,
    config: DetectorConfig,
    death_cross: int,
    golden_cross: int,
    recovery_end: int,
) -> Divergence | None:
    """Detect a lower price low after MACD has already crossed upward.

    This captures the ETH structure in the supplied chart: the first price and
    MACD lows occur in one negative lobe; MACD then makes a golden cross below
    zero; price subsequently undercuts its prior low while MACD remains much
    higher.  The divergence is actionable at that lower-price-low candle, with
    no right-side pivot requirement or future candle access.
    """
    m1_index = _extreme_index(data, death_cross, golden_cross, "macd", "low")
    p1_index = _extreme_index(data, death_cross, golden_cross, "low", "low")
    p2_index = _extreme_index(data, golden_cross, recovery_end, "low", "low")
    if p2_index <= golden_cross:
        return None
    if abs(p1_index - m1_index) > config.max_price_macd_offset:
        return None

    bars = p2_index - p1_index
    if not config.min_bars_between <= bars <= config.max_bars_between:
        return None
    if not all((
        _price_swing_is_large(data, p1_index, "bullish", config),
        _price_swing_is_large(data, p2_index, "bullish", config),
        _macd_swing_is_large(data, m1_index, "bullish", config),
        _macd_swing_is_large(data, p2_index, "bullish", config),
    )):
        return None

    m1, m2 = data.at[m1_index, "macd"], data.at[p2_index, "macd"]
    macd_between = data.loc[m1_index:p2_index, "macd"]
    if not (
        _price_divergence_is_large(data, p1_index, p2_index, "bullish", config)
        and m2 >= m1 * (1 - config.min_macd_drop_pct)
        and macd_between.max() <= 0
        and data.at[golden_cross, "macd"] <= 0
    ):
        return None

    return Divergence(
        kind="bullish",
        first_time=data.at[p1_index, "open_time"],
        second_time=data.at[p2_index, "open_time"],
        first_macd_time=data.at[m1_index, "open_time"],
        second_macd_time=data.at[p2_index, "open_time"],
        first_price=float(data.at[p1_index, "low"]),
        second_price=float(data.at[p2_index, "low"]),
        first_macd=float(m1), second_macd=float(m2), bars_between=bars,
        # The lower-low candle completes this price-led structure. The prior
        # golden cross is retained separately so output stays unambiguous.
        confirmation_time=data.at[p2_index, "open_time"],
        macd_cross_time=data.at[golden_cross, "open_time"],
    )


def detect_regular_divergences(data: pd.DataFrame, config: DetectorConfig = DetectorConfig()) -> list[Divergence]:
    """Detect regular divergences when MACD crosses Signal, without look-ahead.

    A bearish signal is confirmed at a MACD death cross and a bullish signal at
    a golden cross. Price extrema are selected from their MACD lobes, so the
    confirmation candle itself does not have to be the price high/low.
    """
    required = {"open_time", "high", "low", "close", "macd", "macd_signal", "atr"}
    missing = required - set(data.columns)
    if missing:
        raise ValueError(f"data is missing columns: {sorted(missing)}")

    death_crosses = _cross_indices(data, "death")
    golden_crosses = _cross_indices(data, "golden")
    signals: list[Divergence] = []

    # Bearish: two positive MACD lobes, confirmed by the second death cross.
    for first_death, second_death in zip(death_crosses, death_crosses[1:]):
        first_golden = _last_between(golden_crosses, -1, first_death)
        second_golden = _last_between(golden_crosses, first_death, second_death)
        if first_golden is None or second_golden is None:
            continue
        signal = _build_divergence(
            data, config, "bearish", (first_golden, first_death),
            (second_golden, second_death), second_death,
        )
        if signal:
            signals.append(signal)

    # Bullish: two negative MACD lobes, confirmed by the second golden cross.
    for first_golden, second_golden in zip(golden_crosses, golden_crosses[1:]):
        first_death = _last_between(death_crosses, -1, first_golden)
        second_death = _last_between(death_crosses, first_golden, second_golden)
        if first_death is None or second_death is None:
            continue
        signal = _build_divergence(
            data, config, "bullish", (first_death, first_golden),
            (second_death, second_golden), second_golden,
        )
        if signal:
            signals.append(signal)

    # Price-led bullish divergence: MACD first recovers with a golden cross
    # below zero, then price makes a later lower low while MACD stays higher.
    for golden_cross in golden_crosses:
        prior_deaths = [index for index in death_crosses if index < golden_cross]
        if not prior_deaths:
            continue
        following_deaths = [index for index in death_crosses if index > golden_cross]
        recovery_end = following_deaths[0] if following_deaths else len(data) - 1
        signal = _build_bullish_recovery_divergence(
            data, config, prior_deaths[-1], golden_cross, recovery_end,
        )
        if signal:
            signals.append(signal)
    # The same historical structure can be rediscovered while replaying data;
    # retain one result per price endpoint, preferring a standard cross-confirmed
    # signal when both variants happen to end on the same candle.
    return list({(item.kind, item.first_time, item.second_time): item for item in signals}.values())


def latest_divergence(
    signals: list[Divergence], kind: Literal["all", "bearish", "bullish"] = "all",
) -> Divergence | None:
    """Return only the latest triggered divergence, optionally by direction."""
    candidates = [item for item in signals if kind == "all" or item.kind == kind]
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item.confirmation_time, item.second_time))


def interval_to_seconds(interval: str) -> int:
    """Convert supported Binance intervals to seconds for aligned scheduling."""
    units = {"m": 60, "h": 3_600, "d": 86_400, "w": 604_800}
    if len(interval) < 2 or interval[-1] not in units or not interval[:-1].isdigit():
        raise ValueError(f"Unsupported interval {interval!r}; use forms such as 15m, 1h, 4h, 1d")
    count = int(interval[:-1])
    if count <= 0:
        raise ValueError("Interval multiplier must be positive")
    return count * units[interval[-1]]


def next_check_time(interval: str, delay_minutes: int, now: datetime | None = None) -> datetime:
    """Return the next wall-clock time to inspect a completed interval.

    A 1h interval with a 2-minute delay runs at ``HH:02:00``. A 4h interval
    runs at ``00:02``, ``04:02``, ``08:02`` … in UTC; this is the same clock
    alignment Binance uses for its kline boundaries.
    """
    if delay_minutes < 0:
        raise ValueError("delay_minutes must be zero or greater")
    current = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    period = interval_to_seconds(interval)
    delay = delay_minutes * 60
    next_close_epoch = (int(current.timestamp()) - delay) // period * period + period
    return datetime.fromtimestamp(next_close_epoch + delay, tz=timezone.utc)


def signal_on_latest_closed_candle(data: pd.DataFrame, config: DetectorConfig) -> Divergence | None:
    """Return a divergence only if its trigger belongs to the newest closed bar.

    This is the key live-monitor rule: older signals remain available for the
    calculation, but are never printed when a later scheduled check starts.
    """
    if data.empty:
        return None
    latest_open_time = data.iloc[-1]["open_time"]
    candidates = [
        item for item in detect_regular_divergences(data, config)
        if item.confirmation_time == latest_open_time
    ]
    return latest_divergence(candidates)


def format_signal(signal: Divergence, display_timezone: str) -> str:
    """Render one signal with timestamps in the configured display timezone."""
    tz = ZoneInfo(display_timezone)

    def stamp(value: pd.Timestamp) -> str:
        return value.to_pydatetime().astimezone(tz).strftime("%Y-%m-%d %H:%M")

    return (
        f"{signal.kind:7} | "
        f"price_extreme={stamp(signal.first_time)} -> {stamp(signal.second_time)} {display_timezone} | "
        f"price {signal.first_price:.6g} -> {signal.second_price:.6g} | "
        f"green_MACD_extreme={stamp(signal.first_macd_time)} -> {stamp(signal.second_macd_time)} {display_timezone} | "
        f"MACD {signal.first_macd:.6g} -> {signal.second_macd:.6g} | price_bars={signal.bars_between} | "
        f"confirmed={stamp(signal.confirmation_time)} | MACD_cross={stamp(signal.macd_cross_time)}"
    )


def send_telegram_message(message: str, monitor: MonitorConfig) -> None:
    """Send a notification for a new live signal using Telegram's Bot API."""
    if not monitor.telegram_enabled:
        return
    assert monitor.telegram_bot_token is not None and monitor.telegram_chat_id is not None
    text = f"{message} {monitor.telegram_mention}".rstrip()
    endpoint = f"https://api.telegram.org/bot{monitor.telegram_bot_token}/sendMessage"
    try:
        response = requests.post(
            endpoint,
            json={"chat_id": monitor.telegram_chat_id, "text": text},
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        # Do not include the exception text: HTTP errors can contain the bot
        # token inside the endpoint URL and should never leak into logs.
        raise RuntimeError(f"Telegram notification failed ({error.__class__.__name__})") from error


def check_market(
    symbol: str,
    interval: str,
    limit: int,
    config: DetectorConfig,
    *,
    backtest_offset: int | None = None,
    as_of: str | None = None,
) -> tuple[pd.DataFrame, Divergence | None]:
    """Fetch one market and return either the current-bar or backtest signal."""
    if backtest_offset is not None and as_of is not None:
        raise ValueError("Use either --backtest-offset or --as-of, not both")
    raw_data = fetch_futures_klines(symbol, interval, limit)
    if as_of is not None:
        data = simulate_historical_at_time(raw_data, as_of)
    elif backtest_offset is not None:
        data = simulate_historical_as_of(raw_data, backtest_offset)
    else:
        data = raw_data
    data = add_indicators(data, config)
    if backtest_offset is not None or as_of is not None:
        return data, latest_divergence(detect_regular_divergences(data, config))
    return data, signal_on_latest_closed_candle(data, config)


def run_checks(
    symbols: tuple[str, ...],
    intervals: tuple[str, ...],
    monitor: MonitorConfig,
    detector: DetectorConfig,
    *,
    backtest_offset: int | None = None,
    as_of: str | None = None,
) -> None:
    """Check every configured symbol/interval once and print only new signals."""
    for symbol in symbols:
        for interval in intervals:
            try:
                data, signal = check_market(
                    symbol, interval, monitor.kline_limit, detector,
                    backtest_offset=backtest_offset, as_of=as_of,
                )
                as_of = data.iloc[-1]["close_time"].to_pydatetime().astimezone(ZoneInfo(monitor.display_timezone))
                prefix = f"[{datetime.now(ZoneInfo(monitor.display_timezone)):%Y-%m-%d %H:%M:%S}] {symbol} {interval}"
                if signal:
                    signal_text = format_signal(signal, monitor.display_timezone)
                    print(f"{prefix} {signal_text}", flush=True)
                    # Historical replay is for validation only and must never
                    # notify a real chat. Live checks only emit current-bar signals.
                    if backtest_offset is None and as_of is None:
                        send_telegram_message(f"{symbol} {interval}\n{signal_text}", monitor)
                else:
                    scope = "historical backtest" if backtest_offset is not None or as_of is not None else "latest closed candle"
                    print(f"{prefix} no divergence on {scope}; as_of={as_of:%Y-%m-%d %H:%M %Z}", flush=True)
            except Exception as error:  # Keep other configured markets alive after an API failure.
                print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {symbol} {interval} ERROR: {error}", flush=True)


def run_monitor(monitor: MonitorConfig, detector: DetectorConfig) -> None:
    """Continuously run each configured check after each interval closes."""
    tasks = [(symbol, interval) for symbol in monitor.symbols for interval in monitor.intervals]
    due_at = {
        task: next_check_time(task[1], monitor.close_delay_minutes)
        for task in tasks
    }
    print(
        f"Monitoring symbols={','.join(monitor.symbols)} intervals={','.join(monitor.intervals)} "
        f"close_delay={monitor.close_delay_minutes}m timezone={monitor.display_timezone}",
        flush=True,
    )
    if monitor.check_on_start:
        print("Running immediate startup check (latest closed candles only).", flush=True)
        run_checks(monitor.symbols, monitor.intervals, monitor, detector)

    while True:
        next_due = min(due_at.values())
        seconds_to_wait = max(0.0, (next_due - datetime.now(timezone.utc)).total_seconds())
        time.sleep(seconds_to_wait)
        now = datetime.now(timezone.utc)
        ready = [task for task, due in due_at.items() if due <= now]
        for symbol, interval in ready:
            run_checks((symbol,), (interval,), monitor, detector)
            due_at[(symbol, interval)] = next_check_time(interval, monitor.close_delay_minutes, datetime.now(timezone.utc))


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor current MACD divergences from Binance USD-M futures candles.")
    parser.add_argument("--env", default=".env", help="Path to monitor settings (default: .env)")
    parser.add_argument("--once", action="store_true", help="Check configured markets immediately, then exit")
    parser.add_argument("--symbol", help="One symbol override for --once/backtest, e.g. SOLUSDT")
    parser.add_argument("--interval", help="One interval override for --once/backtest, e.g. 1h")
    parser.add_argument("--limit", type=int, help="Kline limit override for --once/backtest")
    parser.add_argument(
        "--backtest-offset", type=int,
        help="Optional historical simulation: hide newest candles, print the latest signal at that historical point, then exit",
    )
    parser.add_argument(
        "--as-of",
        help="Exact candle open time, e.g. 2026-08-12T22:00:00Z or 2026-08-13T06:00:00+08:00",
    )
    args = parser.parse_args()

    monitor = load_monitor_config(args.env)
    if args.limit is not None:
        if not 1 <= args.limit <= 1500:
            parser.error("--limit must be between 1 and 1500")
        monitor = MonitorConfig(**{**asdict(monitor), "kline_limit": args.limit})
    symbols = (args.symbol.upper(),) if args.symbol else monitor.symbols
    intervals = (args.interval.lower(),) if args.interval else monitor.intervals
    for interval in intervals:
        interval_to_seconds(interval)
    detector = DetectorConfig()
    print("filters=", asdict(detector), flush=True)

    if args.as_of and args.backtest_offset is not None:
        parser.error("Use either --backtest-offset or --as-of, not both")
    if args.as_of:
        run_checks(symbols, intervals, monitor, detector, as_of=args.as_of)
    elif args.backtest_offset is not None:
        if args.backtest_offset < 0:
            parser.error("--backtest-offset must be zero or greater")
        run_checks(symbols, intervals, monitor, detector, backtest_offset=args.backtest_offset)
    elif args.once:
        run_checks(symbols, intervals, monitor, detector)
    else:
        run_monitor(monitor, detector)


if __name__ == "__main__":
    main()
