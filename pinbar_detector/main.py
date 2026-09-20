#!/usr/bin/env python3
"""Continuously monitor Binance USD-M Pin Bar signals.

This is deliberately a small scheduling wrapper around ``pinbar_detector``.
The detection rules themselves remain in pinbar_detector.py, so historical
commands such as ``python3 pinbar_detector.py --as-of ...`` behave unchanged.

Examples
--------
python3 main.py
python3 main.py --interval 1h
python3 main.py --monitor
python3 main.py --env /path/to/pinbar.env
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import TypeVar
import warnings

import pandas as pd

# macOS system Python can be linked against LibreSSL.  This warning does not
# affect the public HTTPS request; keep monitor logs readable by suppressing
# only urllib3's compatibility notice.
warnings.filterwarnings("ignore", message=r"urllib3 v2 only supports OpenSSL 1\.1\.1\+.*")
import requests

from pinbar_detector import (
    PinBarConfig,
    PinBarSignal,
    TelegramConfig,
    detect_latest_pinbar,
    fetch_futures_klines,
    load_env_file,
    load_notification_state,
    load_telegram_config,
    notify_pinbar_once,
    pinbar_notification_key,
    save_notification_state,
)


T = TypeVar("T")


@dataclass(frozen=True)
class MonitorConfig:
    """Settings for the live Pin Bar scheduler."""

    symbols: tuple[str, ...]
    intervals: tuple[str, ...]
    close_delay_minutes: int
    kline_limit: int
    check_on_start: bool
    retry_count: int
    retry_delay_seconds: float
    pinbar: PinBarConfig
    telegram: TelegramConfig
    iphone: "IPhoneConfig"


@dataclass(frozen=True)
class IPhoneConfig:
    """Optional local relay settings for iPhone notifications."""

    enabled: bool
    url: str | None
    state_file: Path


def parse_bool(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def parse_csv(value: str, name: str, *, uppercase: bool = False) -> tuple[str, ...]:
    items = tuple(
        item.strip().upper() if uppercase else item.strip().lower()
        for item in value.split(",")
        if item.strip()
    )
    if not items:
        raise ValueError(f"{name} must contain at least one value")
    return items


def parse_cli_csv(values: list[str] | None, name: str, *, uppercase: bool = False) -> tuple[str, ...] | None:
    """Parse repeated CLI options, each of which may also contain commas."""
    if not values:
        return None
    return parse_csv(
        ",".join(values),
        name,
        uppercase=uppercase,
    )


def interval_to_seconds(interval: str) -> int:
    """Convert Binance's fixed-size K-line intervals to seconds."""
    units = {"m": 60, "h": 3_600, "d": 86_400, "w": 604_800}
    if len(interval) < 2 or interval[-1] not in units or not interval[:-1].isdigit():
        raise ValueError(f"Unsupported interval {interval!r}; use forms such as 15m, 1h, 4h, 1d")
    multiplier = int(interval[:-1])
    if multiplier <= 0:
        raise ValueError("Interval multiplier must be positive")
    return multiplier * units[interval[-1]]


def next_check_time(interval: str, delay_minutes: int, now: datetime | None = None) -> datetime:
    """Return the next Binance close boundary plus the configured delay."""
    if delay_minutes < 0:
        raise ValueError("CLOSE_DELAY_MINUTES must be zero or greater")
    current = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    period = interval_to_seconds(interval)
    delay_seconds = delay_minutes * 60
    next_close_epoch = (int(current.timestamp()) - delay_seconds) // period * period + period
    return datetime.fromtimestamp(next_close_epoch + delay_seconds, tz=timezone.utc)


def load_monitor_config(env_path: str | Path) -> MonitorConfig:
    """Load monitor and Telegram settings from one .env file."""
    path = Path(env_path)
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}. Copy .env.example to .env first.")
    values = load_env_file(path)

    def setting(name: str, default: str | None = None) -> str:
        value = os.environ.get(name, values.get(name, default or "")).strip()
        if not value and default is None:
            raise ValueError(f"Missing required setting: {name}")
        return value

    intervals = parse_csv(setting("INTERVALS"), "INTERVALS")
    for interval in intervals:
        interval_to_seconds(interval)
    pinbar = replace(
        PinBarConfig(),
        min_range_pct=float(setting("PINBAR_MIN_RANGE_PCT", "0.008")),
        min_range_atr=float(setting("PINBAR_MIN_RANGE_ATR", "1.0")),
        min_main_wick_body_ratio=float(setting("PINBAR_MIN_MAIN_WICK_BODY_RATIO", "3.0")),
        max_opposite_wick_body_ratio=float(setting("PINBAR_MAX_OPPOSITE_WICK_BODY_RATIO", "1.5")),
        min_body_range_ratio=float(setting("PINBAR_MIN_BODY_RANGE_RATIO", "0.05")),
        close_end_zone_ratio=float(setting("PINBAR_CLOSE_END_ZONE_RATIO", "0.40")),
        context_lookback=int(setting("PINBAR_CONTEXT_LOOKBACK", "12")),
        atr_period=int(setting("PINBAR_ATR_PERIOD", "14")),
    )
    if (
        pinbar.min_range_pct <= 0
        or pinbar.min_range_atr < 0
        or pinbar.min_main_wick_body_ratio <= 0
        or pinbar.max_opposite_wick_body_ratio < 0
        or not 0 < pinbar.min_body_range_ratio <= 1
        or not 0 < pinbar.close_end_zone_ratio <= 1
        or pinbar.context_lookback < 1
        or pinbar.atr_period < 1
    ):
        raise ValueError("Invalid PINBAR_* threshold in configuration")

    close_delay = int(setting("CLOSE_DELAY_MINUTES", "1"))
    kline_limit = int(setting("KLINE_LIMIT", "100"))
    retry_count = int(setting("API_RETRY_COUNT", "3"))
    retry_delay = float(setting("API_RETRY_DELAY_SECONDS", "2"))
    minimum_history = max(pinbar.context_lookback, pinbar.atr_period) + 1
    if close_delay < 0:
        raise ValueError("CLOSE_DELAY_MINUTES must be zero or greater")
    if not minimum_history <= kline_limit <= 1500:
        raise ValueError(f"KLINE_LIMIT must be between {minimum_history} and 1500 for the configured filters")
    if retry_count < 1 or retry_delay < 0:
        raise ValueError("API_RETRY_COUNT must be at least 1 and API_RETRY_DELAY_SECONDS must be zero or greater")

    iphone_enabled = parse_bool(setting("IPHONE_ENABLED", "false"), "IPHONE_ENABLED")
    iphone_url = setting("IPHONE_NOTIFICATION_URL", "").strip() or None
    if iphone_enabled and not iphone_url:
        raise ValueError("IPHONE_ENABLED=true requires IPHONE_NOTIFICATION_URL")
    iphone_state_value = setting("IPHONE_STATE_FILE", ".pinbar_iphone_state.json")
    iphone_state_file = Path(iphone_state_value)
    if not iphone_state_file.is_absolute():
        iphone_state_file = path.resolve().parent / iphone_state_file

    return MonitorConfig(
        symbols=parse_csv(setting("SYMBOLS"), "SYMBOLS", uppercase=True),
        intervals=intervals,
        close_delay_minutes=close_delay,
        kline_limit=kline_limit,
        check_on_start=parse_bool(setting("CHECK_ON_START", "false"), "CHECK_ON_START"),
        retry_count=retry_count,
        retry_delay_seconds=retry_delay,
        pinbar=pinbar,
        telegram=load_telegram_config(path),
        iphone=IPhoneConfig(
            enabled=iphone_enabled,
            url=iphone_url,
            state_file=iphone_state_file,
        ),
    )


def fetch_with_retry(symbol: str, interval: str, config: MonitorConfig) -> pd.DataFrame:
    """Fetch candles with bounded retries; a failed market never ends the daemon."""
    last_error: Exception | None = None
    for attempt in range(1, config.retry_count + 1):
        try:
            return fetch_futures_klines(symbol, interval, config.kline_limit)
        except (requests.RequestException, RuntimeError, ValueError) as error:
            last_error = error
            if attempt < config.retry_count:
                time.sleep(config.retry_delay_seconds * attempt)
    assert last_error is not None
    raise RuntimeError(f"Binance K-line request failed after {config.retry_count} attempts ({last_error.__class__.__name__})") from last_error


def signal_summary(symbol: str, interval: str, signal: PinBarSignal) -> str:
    """Format console output. Telegram retains the existing detector format."""
    return (
        f"{symbol} {interval} {signal.kind} | "
        f"candle={signal.open_time:%Y-%m-%d %H:%M} UTC | "
        f"O={signal.open:.6g} H={signal.high:.6g} L={signal.low:.6g} C={signal.close:.6g}"
    )


def sendToIphone(title: str, msg: str, config: IPhoneConfig) -> bool:
    """Post one alert to the user's local iPhone-notification relay.

    The relay is deliberately optional: an unavailable local service returns
    ``False`` and must never stop Binance polling or Telegram delivery.
    """
    if not config.enabled:
        return False
    assert config.url is not None
    payload = {"title": title, "body": msg}
    headers = {"Content-Type": "application/json; charset=utf-8"}
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        response = requests.post(config.url, headers=headers, data=data, timeout=10)
        return 200 <= response.status_code < 300
    except requests.RequestException:
        return False


def notify_iphone_once(symbol: str, interval: str, signal: PinBarSignal, config: IPhoneConfig) -> bool | None:
    """Send an iPhone alert once per candle; ``None`` means it was already sent."""
    if not config.enabled:
        return None
    key = pinbar_notification_key(symbol, interval, signal)
    state = load_notification_state(config.state_file)
    if key in state:
        return None
    title = f"Pin Bar · {symbol.upper()} {interval} · {signal.kind.replace('_pinbar', '')}"
    if not sendToIphone(title, signal_summary(symbol.upper(), interval, signal), config):
        return False
    state.add(key)
    if len(state) > 5_000:
        state = set(sorted(state)[-2_500:])
    save_notification_state(config.state_file, state)
    return True


def check_one(symbol: str, interval: str, config: MonitorConfig, *, notify: bool = True) -> PinBarSignal | None:
    """Check exactly the newest closed K line and optionally deliver its alert."""
    data = fetch_with_retry(symbol, interval, config)
    signal = detect_latest_pinbar(data, config.pinbar)
    if signal is None:
        print(f"{symbol} {interval} | no Pin Bar on newest closed candle", flush=True)
        return None
    print(signal_summary(symbol, interval, signal), flush=True)
    if notify:
        try:
            if notify_pinbar_once(symbol, interval, signal, config.telegram):
                print(f"{symbol} {interval} | Telegram notification sent.", flush=True)
            elif config.telegram.enabled:
                print(f"{symbol} {interval} | Telegram notification already sent for this candle.", flush=True)
        except RuntimeError as error:
            print(f"{symbol} {interval} | Telegram notification ERROR: {error}", flush=True)
        try:
            iphone_sent = notify_iphone_once(symbol, interval, signal, config.iphone)
            iphone_error = False
        except OSError as error:
            # State-file trouble must not make the scheduled monitor exit.
            print(f"{symbol} {interval} | iPhone notification ERROR: {error.__class__.__name__}", flush=True)
            iphone_sent = None
            iphone_error = True
        if iphone_sent is True:
            print(f"{symbol} {interval} | iPhone notification sent.", flush=True)
        elif iphone_sent is False:
            print(f"{symbol} {interval} | iPhone notification ERROR: local relay did not return 2xx.", flush=True)
        elif config.iphone.enabled and not iphone_error:
            print(f"{symbol} {interval} | iPhone notification already sent for this candle.", flush=True)
    return signal


def run_all_checks(config: MonitorConfig, *, notify: bool) -> None:
    for symbol in config.symbols:
        for interval in config.intervals:
            try:
                check_one(symbol, interval, config, notify=notify)
            except Exception as error:  # Keep all other symbol/interval tasks alive.
                print(f"{symbol} {interval} | check ERROR: {error}", flush=True)


def run_monitor(config: MonitorConfig, *, notify: bool) -> None:
    """Run forever, checking each configured interval after its close delay."""
    tasks = [(symbol, interval) for symbol in config.symbols for interval in config.intervals]
    due_at = {
        task: next_check_time(task[1], config.close_delay_minutes)
        for task in tasks
    }
    if config.check_on_start:
        print("CHECK_ON_START=true: checking the newest closed candles now.", flush=True)
        run_all_checks(config, notify=notify)
    print(
        "Monitoring Pin Bars: "
        f"symbols={','.join(config.symbols)} intervals={','.join(config.intervals)} "
        f"close_delay={config.close_delay_minutes}m",
        flush=True,
    )
    while True:
        next_due = min(due_at.values())
        time.sleep(max(0.0, (next_due - datetime.now(timezone.utc)).total_seconds()))
        now = datetime.now(timezone.utc)
        for task, due in list(due_at.items()):
            if due <= now:
                symbol, interval = task
                try:
                    check_one(symbol, interval, config, notify=notify)
                except Exception as error:  # One API failure must not stop the daemon.
                    print(f"{symbol} {interval} | check ERROR: {error}", flush=True)
                due_at[task] = next_check_time(
                    interval, config.close_delay_minutes, datetime.now(timezone.utc)
                )


def main() -> None:
    parser = argparse.ArgumentParser(description="Continuously monitor Binance USD-M Pin Bar signals.")
    parser.add_argument(
        "--env",
        default=str(Path(__file__).resolve().with_name(".env")),
        help="Configuration file (default: .env next to main.py)",
    )
    parser.add_argument(
        "--symbol",
        action="append",
        help="Override SYMBOLS for this run; repeat it or use commas, e.g. --symbol BTCUSDT,ETHUSDT",
    )
    parser.add_argument(
        "--interval",
        action="append",
        help="Override INTERVALS for this run; repeat it or use commas, e.g. --interval 1h",
    )
    parser.add_argument(
        "--monitor",
        action="store_true",
        help="Run continuously after each configured K-line close (default: check once and exit)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Compatibility alias; check the newest closed candle once, then exit",
    )
    parser.add_argument("--no-notify", action="store_true", help="Print signals but never send Telegram messages")
    args = parser.parse_args()
    try:
        config = load_monitor_config(args.env)
    except (FileNotFoundError, ValueError) as error:
        parser.error(str(error))
    cli_symbols = parse_cli_csv(args.symbol, "--symbol", uppercase=True)
    cli_intervals = parse_cli_csv(args.interval, "--interval")
    try:
        if cli_intervals:
            for interval in cli_intervals:
                interval_to_seconds(interval)
        config = replace(
            config,
            symbols=cli_symbols or config.symbols,
            intervals=cli_intervals or config.intervals,
        )
    except ValueError as error:
        parser.error(str(error))
    if args.monitor:
        run_monitor(config, notify=not args.no_notify)
    else:
        run_all_checks(config, notify=not args.no_notify)


if __name__ == "__main__":
    main()
