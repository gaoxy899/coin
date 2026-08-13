#!/usr/bin/env python3
"""Monitor Binance futures for MACD divergence confirmed by a same-side pin bar.

This is an orchestration layer. It imports the independent MACD-divergence and
pin-bar detectors without changing either detector's rules.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from typing import Literal


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "macd_divergence_detector"))
sys.path.insert(0, str(PROJECT_ROOT / "pinbar_detector"))

from macd_divergence import (  # noqa: E402
    DetectorConfig,
    Divergence,
    ZoneInfo,
    add_indicators,
    fetch_futures_klines,
    format_signal,
    interval_to_seconds,
    next_check_time,
    signal_on_latest_closed_candle,
)
from pinbar_detector import PinBarConfig, PinBarSignal, detect_latest_pinbar  # noqa: E402
import requests  # noqa: E402


SignalKind = Literal["bullish", "bearish"]


@dataclass(frozen=True)
class MonitorConfig:
    symbols: tuple[str, ...]
    intervals: tuple[str, ...]
    close_delay_minutes: int
    kline_limit: int
    display_timezone: str
    check_on_start: bool
    pinbar_max_bars_after_divergence: int
    state_file: Path
    telegram_enabled: bool
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    telegram_mention: str


@dataclass(frozen=True)
class PendingDivergence:
    kind: SignalKind
    confirmation_time: str
    expires_after_open_time: str
    description: str


def _parse_bool(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _csv(value: str, *, uppercase: bool) -> tuple[str, ...]:
    items = tuple((item.strip().upper() if uppercase else item.strip().lower()) for item in value.split(",") if item.strip())
    if not items:
        raise ValueError("List must contain at least one item")
    return items


def load_env(path: str | Path) -> dict[str, str]:
    """Read a small dependency-free KEY=VALUE .env file."""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"Config file not found: {source}. Copy .env.example to .env first.")
    values: dict[str, str] = {}
    for number, raw in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"Invalid .env line {number}: expected KEY=VALUE")
        key, value = (part.strip() for part in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def load_config(path: str | Path) -> MonitorConfig:
    values = load_env(path)

    def required(name: str) -> str:
        value = values.get(name, os.environ.get(name, "")).strip()
        if not value:
            raise ValueError(f"Missing required setting: {name}")
        return value

    intervals = _csv(required("INTERVALS"), uppercase=False)
    for interval in intervals:
        interval_to_seconds(interval)
    delay, limit = int(required("CLOSE_DELAY_MINUTES")), int(required("KLINE_LIMIT"))
    max_after = int(required("PINBAR_MAX_BARS_AFTER_DIVERGENCE"))
    if delay < 0 or max_after < 0:
        raise ValueError("CLOSE_DELAY_MINUTES and PINBAR_MAX_BARS_AFTER_DIVERGENCE must be zero or greater")
    if not 20 <= limit <= 1500:
        raise ValueError("KLINE_LIMIT must be between 20 and 1500")
    timezone_name = values.get("DISPLAY_TIMEZONE", "Asia/Taipei")
    ZoneInfo(timezone_name)
    enabled = _parse_bool(values.get("TELEGRAM_ENABLED", "false"), "TELEGRAM_ENABLED")
    token, chat_id = values.get("TELEGRAM_BOT_TOKEN", "").strip() or None, values.get("TELEGRAM_CHAT_ID", "").strip() or None
    if enabled and (not token or not chat_id):
        raise ValueError("TELEGRAM_ENABLED=true requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    return MonitorConfig(
        symbols=_csv(required("SYMBOLS"), uppercase=True), intervals=intervals,
        close_delay_minutes=delay, kline_limit=limit, display_timezone=timezone_name,
        check_on_start=_parse_bool(values.get("CHECK_ON_START", "false"), "CHECK_ON_START"),
        pinbar_max_bars_after_divergence=max_after,
        state_file=Path(values.get("STATE_FILE", ".macd_pinbar_state.json")),
        telegram_enabled=enabled, telegram_bot_token=token, telegram_chat_id=chat_id,
        telegram_mention=values.get("TELEGRAM_MENTION", "").strip(),
    )


def load_state(path: Path) -> dict[str, PendingDivergence]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {key: PendingDivergence(**value) for key, value in payload.items()}
    except (OSError, json.JSONDecodeError, TypeError):
        # Do not crash a market monitor over an interrupted/corrupt state write.
        return {}


def save_state(path: Path, state: dict[str, PendingDivergence]) -> None:
    """Atomically persist pending signals so a restart does not lose them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps({key: vars(value) for key, value in state.items()}, indent=2), encoding="utf-8")
    temporary.replace(path)


def state_key(symbol: str, interval: str) -> str:
    return f"{symbol.upper()}:{interval.lower()}"


def compatible(divergence_kind: SignalKind, pinbar: PinBarSignal | None) -> bool:
    return pinbar is not None and (
        (divergence_kind == "bullish" and pinbar.kind == "bullish_pinbar")
        or (divergence_kind == "bearish" and pinbar.kind == "bearish_pinbar")
    )


def open_time_after_bars(open_time: str, interval: str, bars: int) -> str:
    timestamp = datetime.fromisoformat(open_time.replace("Z", "+00:00"))
    seconds = interval_to_seconds(interval) * bars
    return datetime.fromtimestamp(timestamp.timestamp() + seconds, tz=timezone.utc).isoformat()


def send_telegram(message: str, config: MonitorConfig) -> None:
    if not config.telegram_enabled:
        return
    assert config.telegram_bot_token and config.telegram_chat_id
    endpoint = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
    try:
        response = requests.post(
            endpoint,
            json={"chat_id": config.telegram_chat_id, "text": f"{message} {config.telegram_mention}".rstrip()},
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        raise RuntimeError(f"Telegram notification failed ({error.__class__.__name__})") from error


def process_latest_bar(
    symbol: str,
    interval: str,
    data,
    config: MonitorConfig,
    state: dict[str, PendingDivergence],
) -> str | None:
    """Process one new closed bar and return a combo message only when confirmed."""
    macd_data = add_indicators(data, DetectorConfig())
    divergence = signal_on_latest_closed_candle(macd_data, DetectorConfig())
    pinbar = detect_latest_pinbar(data, PinBarConfig())
    latest_open = data.iloc[-1]["open_time"].to_pydatetime().astimezone(timezone.utc).isoformat()
    key = state_key(symbol, interval)
    pending = state.get(key)

    # Expire old pending divergence before examining this bar.
    if pending and latest_open > pending.expires_after_open_time:
        state.pop(key, None)
        pending = None

    # A just-triggered divergence can be confirmed by the same candle or by a
    # future compatible pin bar, but no old divergence is rediscovered here.
    if divergence:
        kind: SignalKind = divergence.kind
        description = format_signal(divergence, config.display_timezone)
        expires = open_time_after_bars(
            divergence.confirmation_time.to_pydatetime().astimezone(timezone.utc).isoformat(),
            interval,
            config.pinbar_max_bars_after_divergence,
        )
        pending = PendingDivergence(kind, divergence.confirmation_time.isoformat(), expires, description)
        state[key] = pending

    if pending and compatible(pending.kind, pinbar):
        pinbar_time = pinbar.open_time.to_pydatetime().astimezone(ZoneInfo(config.display_timezone)).strftime("%Y-%m-%d %H:%M")
        message = (
            f"{symbol} {interval} MACD + Pin Bar confirmed\n"
            f"{pending.description}\n"
            f"pinbar={pinbar.kind} at {pinbar_time} {config.display_timezone}"
        )
        state.pop(key, None)  # A signal is consumed once: no duplicate alert.
        return message
    return None


def check_one(symbol: str, interval: str, config: MonitorConfig, state: dict[str, PendingDivergence]) -> None:
    try:
        data = fetch_futures_klines(symbol, interval, config.kline_limit)
        if len(data) < 20:
            raise RuntimeError("Not enough completed candles returned by Binance")
        message = process_latest_bar(symbol, interval, data, config, state)
        prefix = f"[{datetime.now(ZoneInfo(config.display_timezone)):%Y-%m-%d %H:%M:%S}] {symbol} {interval}"
        if message:
            print(f"{prefix} {message.replace(chr(10), ' | ')}", flush=True)
            send_telegram(message, config)
        else:
            print(f"{prefix} no new MACD + Pin Bar confirmation", flush=True)
    except Exception as error:
        # Keep checking all other markets. Telegram errors omit the token.
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {symbol} {interval} ERROR: {error}", flush=True)


def run_checks(config: MonitorConfig, state: dict[str, PendingDivergence]) -> None:
    for symbol in config.symbols:
        for interval in config.intervals:
            check_one(symbol, interval, config, state)
            save_state(config.state_file, state)


def run_monitor(config: MonitorConfig) -> None:
    tasks = [(symbol, interval) for symbol in config.symbols for interval in config.intervals]
    due_at = {task: next_check_time(task[1], config.close_delay_minutes) for task in tasks}
    state = load_state(config.state_file)
    print(
        f"Monitoring MACD + Pin Bar: symbols={','.join(config.symbols)} intervals={','.join(config.intervals)} "
        f"delay={config.close_delay_minutes}m pinbar_window={config.pinbar_max_bars_after_divergence} bars",
        flush=True,
    )
    if config.check_on_start:
        run_checks(config, state)
    while True:
        next_due = min(due_at.values())
        time.sleep(max(0.0, (next_due - datetime.now(timezone.utc)).total_seconds()))
        now = datetime.now(timezone.utc)
        for task, due in list(due_at.items()):
            if due <= now:
                check_one(task[0], task[1], config, state)
                save_state(config.state_file, state)
                due_at[task] = next_check_time(task[1], config.close_delay_minutes, datetime.now(timezone.utc))


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor MACD divergence confirmed by a matching pin bar.")
    parser.add_argument("--env", default=".env", help="Path to monitor settings (default: .env)")
    parser.add_argument("--once", action="store_true", help="Check the latest closed candle once, then exit")
    args = parser.parse_args()
    config = load_config(args.env)
    state = load_state(config.state_file)
    if args.once:
        run_checks(config, state)
    else:
        run_monitor(config)


if __name__ == "__main__":
    main()
