#!/usr/bin/env python3
"""Detect bullish and bearish pin bars from Binance USD-M futures candles.

By default only the newest completed candle is evaluated.  This makes the
script suitable for a scheduled run immediately after a candle closes.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
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
    # IFVG/FVG confirmation settings. A percentage floor makes the rule work
    # on instruments with very different prices; the ATR floor filters gaps
    # that are visually tiny for the current volatility. The displacement and
    # confirmation candles must also have meaningful bodies.
    min_fvg_width_pct: float = 0.0015
    min_fvg_width_atr: float = 0.20
    min_fvg_qualifying_body_atr: float = 0.25
    source_fvg_lookback: int = 16
    fvg_max_bars_after_ifvg: int = 8


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


@dataclass(frozen=True)
class FairValueGap:
    """A three-candle body FVG, allowing the candles' wicks to overlap."""

    kind: Literal["bullish_fvg", "bearish_fvg"]
    left_time: pd.Timestamp
    middle_time: pd.Timestamp
    right_time: pd.Timestamp
    lower: float
    upper: float
    width: float
    width_pct: float


@dataclass(frozen=True)
class IFVGFVGSignal:
    """An IFVG reversal confirmed by a later same-direction FVG."""

    kind: Literal["bearish_ifvg_fvg", "bullish_ifvg_fvg"]
    ifvg: FairValueGap
    ifvg_inversion_time: pd.Timestamp
    fvg: FairValueGap


@dataclass(frozen=True)
class TelegramConfig:
    """Optional Telegram settings for real-time Pin Bar alerts."""

    enabled: bool
    bot_token: str | None
    chat_id: str | None
    mention: str
    state_file: Path


def _parse_bool(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def load_env_file(path: str | Path) -> dict[str, str]:
    """Read a minimal KEY=VALUE .env file without an extra dependency."""
    env_path = Path(path)
    if not env_path.is_file():
        return {}
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"Invalid .env line {line_number}: expected KEY=VALUE")
        key, value = (part.strip() for part in line.split("=", 1))
        if not key:
            raise ValueError(f"Invalid .env line {line_number}: empty key")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def load_telegram_config(env_path: str | Path) -> TelegramConfig:
    """Load Telegram settings; environment variables override no local secrets."""
    values = load_env_file(env_path)

    def setting(name: str, default: str = "") -> str:
        return os.environ.get(name, values.get(name, default)).strip()

    enabled = _parse_bool(setting("TELEGRAM_ENABLED", "false"), "TELEGRAM_ENABLED")
    token, chat_id = setting("TELEGRAM_BOT_TOKEN") or None, setting("TELEGRAM_CHAT_ID") or None
    if enabled and (not token or not chat_id):
        raise ValueError("TELEGRAM_ENABLED=true requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    state_value = setting("TELEGRAM_STATE_FILE", ".pinbar_telegram_state.json")
    state_file = Path(state_value)
    if not state_file.is_absolute():
        state_file = Path(env_path).resolve().parent / state_file
    return TelegramConfig(enabled, token, chat_id, setting("TELEGRAM_MENTION"), state_file)


def load_notification_state(path: Path) -> set[str]:
    """Return previously delivered alert keys; tolerate a corrupt state file."""
    if not path.is_file():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return set(payload) if isinstance(payload, list) and all(isinstance(item, str) for item in payload) else set()
    except (OSError, json.JSONDecodeError):
        return set()


def save_notification_state(path: Path, state: set[str]) -> None:
    """Atomically persist delivered alert keys to prevent duplicate messages."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(sorted(state)), encoding="utf-8")
    temporary.replace(path)


def pinbar_notification_key(symbol: str, interval: str, signal: PinBarSignal) -> str:
    return f"{symbol.upper()}:{interval.lower()}:{signal.kind}:{signal.open_time.isoformat()}"


def format_pinbar_signal(symbol: str, interval: str, signal: PinBarSignal) -> str:
    """Render a concise Telegram-safe real-time Pin Bar alert in UTC."""
    return (
        f"{symbol.upper()} {interval} {signal.kind}\n"
        f"candle={signal.open_time:%Y-%m-%d %H:%M} UTC\n"
        f"O={signal.open:.6g} H={signal.high:.6g} L={signal.low:.6g} C={signal.close:.6g}\n"
        f"range={signal.range_pct:.2%} body={signal.body:.6g} "
        f"upper_wick={signal.upper_wick:.6g} lower_wick={signal.lower_wick:.6g}"
    )


def send_telegram_message(message: str, config: TelegramConfig) -> None:
    """Send a message without exposing the Bot Token in raised errors."""
    if not config.enabled:
        return
    assert config.bot_token is not None and config.chat_id is not None
    endpoint = f"https://api.telegram.org/bot{config.bot_token}/sendMessage"
    try:
        response = requests.post(
            endpoint,
            json={"chat_id": config.chat_id, "text": f"{message} {config.mention}".rstrip()},
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        raise RuntimeError(f"Telegram notification failed ({error.__class__.__name__})") from error


def notify_pinbar_once(symbol: str, interval: str, signal: PinBarSignal, config: TelegramConfig) -> bool:
    """Send a real-time signal once; return True only after a message is sent."""
    if not config.enabled:
        return False
    state = load_notification_state(config.state_file)
    key = pinbar_notification_key(symbol, interval, signal)
    if key in state:
        return False
    send_telegram_message(format_pinbar_signal(symbol, interval, signal), config)
    state.add(key)
    # Keep state bounded; historical candles never need to be retained forever.
    if len(state) > 5_000:
        state = set(sorted(state)[-2_500:])
    save_notification_state(config.state_file, state)
    return True


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


def detect_fvg_at(
    data: pd.DataFrame,
    right_index: int,
    config: PinBarConfig = PinBarConfig(),
) -> FairValueGap | None:
    """Return the body FVG ending at ``right_index`` when it passes filters.

    Gap bounds use the first and third candle bodies, so ordinary upper/lower
    shadows may overlap. A bullish FVG has the third body entirely above the
    first body; a bearish FVG has it entirely below. The middle displacement
    candle and the third confirming candle must both meet the configured
    minimum body size relative to their preceding ATR.
    """
    if right_index < 2 or right_index >= len(data):
        return None
    left, middle, right = (data.iloc[index] for index in (right_index - 2, right_index - 1, right_index))
    left_body_low, left_body_high = sorted((float(left["open"]), float(left["close"])))
    right_body_low, right_body_high = sorted((float(right["open"]), float(right["close"])))
    if right_body_low > left_body_high:
        kind: Literal["bullish_fvg", "bearish_fvg"] = "bullish_fvg"
        lower, upper = left_body_high, right_body_low
    elif right_body_high < left_body_low:
        kind = "bearish_fvg"
        lower, upper = right_body_high, left_body_low
    else:
        return None

    width = upper - lower
    reference_price = float(middle["close"])
    if width <= 0 or reference_price <= 0:
        return None
    width_pct = width / reference_price
    prior_atr = data.iloc[right_index].get("prior_atr", float("nan"))
    if width_pct < config.min_fvg_width_pct:
        return None
    if pd.notna(prior_atr) and width < config.min_fvg_width_atr * float(prior_atr):
        return None
    for candle in (middle, right):
        candle_atr = candle.get("prior_atr", float("nan"))
        body = abs(float(candle["close"]) - float(candle["open"]))
        if pd.notna(candle_atr) and body < config.min_fvg_qualifying_body_atr * float(candle_atr):
            return None
    return FairValueGap(
        kind=kind,
        left_time=left["open_time"],
        middle_time=middle["open_time"],
        right_time=right["open_time"],
        lower=lower,
        upper=upper,
        width=width,
        width_pct=width_pct,
    )


def _find_ifvg_before_confirmation(
    data: pd.DataFrame,
    confirmation_index: int,
    expected_kind: Literal["bullish_fvg", "bearish_fvg"],
    config: PinBarConfig,
) -> tuple[FairValueGap, int] | None:
    """Find a prior FVG that became an IFVG before the final FVG starts."""
    first_fvg_end = max(2, confirmation_index - config.source_fvg_lookback)
    for fvg_end in range(confirmation_index - 3, first_fvg_end - 1, -1):
        fvg = detect_fvg_at(data, fvg_end, config)
        if fvg is None or fvg.kind != expected_kind:
            continue
        # A wick may enter the zone; only a close through its far edge turns
        # an FVG into an IFVG. The IFVG must exist before the final three-bar
        # FVG begins, and the later FVG must arrive within its time window.
        for inversion_index in range(fvg_end + 1, confirmation_index - 1):
            close = float(data.iloc[inversion_index]["close"])
            inverted = (
                close < fvg.lower if expected_kind == "bullish_fvg" else close > fvg.upper
            )
            if inverted and confirmation_index - inversion_index <= config.fvg_max_bars_after_ifvg:
                return fvg, inversion_index
    return None


def detect_latest_ifvg_fvg(
    data: pd.DataFrame,
    config: PinBarConfig = PinBarConfig(),
) -> IFVGFVGSignal | None:
    """Confirm the final FVG as a continuation of a preceding IFVG.

    Bearish confirmation is: a bullish FVG is closed below and becomes a
    bearish IFVG (resistance), then a later bearish FVG forms. Bullish
    confirmation is the exact mirror image. Pin Bar geometry and local
    high/low rules are intentionally not examined here.
    """
    minimum_candles = max(config.context_lookback, config.atr_period) + 3
    if len(data) < minimum_candles:
        raise ValueError(f"Need at least {minimum_candles} closed candles for IFVG/FVG confirmation")
    enriched = add_prior_atr(data, config.atr_period)
    final_index = len(enriched) - 1
    confirming_fvg = detect_fvg_at(enriched, final_index, config)
    if confirming_fvg is None:
        return None

    original_fvg_kind: Literal["bullish_fvg", "bearish_fvg"]
    signal_kind: Literal["bearish_ifvg_fvg", "bullish_ifvg_fvg"]
    if confirming_fvg.kind == "bearish_fvg":
        original_fvg_kind, signal_kind = "bullish_fvg", "bearish_ifvg_fvg"
    else:
        original_fvg_kind, signal_kind = "bearish_fvg", "bullish_ifvg_fvg"

    inverted_fvg = _find_ifvg_before_confirmation(
        enriched, final_index, original_fvg_kind, config
    )
    if inverted_fvg is None:
        return None
    ifvg, inversion_index = inverted_fvg
    return IFVGFVGSignal(
        kind=signal_kind,
        ifvg=ifvg,
        ifvg_inversion_time=enriched.iloc[inversion_index]["open_time"],
        fvg=confirming_fvg,
    )


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
    parser = argparse.ArgumentParser(
        description="Detect Pin Bars and independent IFVG → FVG confirmations."
    )
    parser.add_argument("--symbol", default="SOLUSDT", help="USD-M perpetual symbol, e.g. SOLUSDT")
    parser.add_argument("--interval", default="1h", help="Binance interval, e.g. 1h or 4h")
    parser.add_argument(
        "--env", default=str(Path(__file__).resolve().with_name(".env")),
        help="Telegram settings file (default: .env next to this script)",
    )
    parser.add_argument("--no-notify", action="store_true", help="Do not send a Telegram alert for this live check")
    parser.add_argument(
        "--backtest-offset", type=int, default=0,
        help="Exclude this many newest closed candles; 9 checks the 10th-newest closed candle",
    )
    parser.add_argument("--as-of", help="Exact UTC candle open time, e.g. 2026-08-13T06:00:00Z")
    parser.add_argument(
        "--min-fvg-width-pct", type=float, default=PinBarConfig.min_fvg_width_pct,
        help="Minimum IFVG/FVG width as a fraction of price (default: 0.0015 = 0.15%%)",
    )
    parser.add_argument(
        "--min-fvg-width-atr", type=float, default=PinBarConfig.min_fvg_width_atr,
        help="Minimum IFVG/FVG width in prior ATR multiples (default: 0.20)",
    )
    parser.add_argument(
        "--min-fvg-body-atr", type=float, default=PinBarConfig.min_fvg_qualifying_body_atr,
        help="Minimum middle/third FVG candle body in prior ATR multiples (default: 0.25)",
    )
    parser.add_argument(
        "--fvg-max-bars-after-ifvg", type=int, default=PinBarConfig.fvg_max_bars_after_ifvg,
        help="Maximum bars from IFVG conversion to the confirming FVG (default: 8)",
    )
    parser.add_argument(
        "--only-ifvg-fvg", action="store_true",
        help="Only print the independent IFVG → FVG result",
    )
    parser.add_argument(
        "--detect-ifvg-fvg", action="store_true",
        help="Also detect the independent IFVG → FVG pattern",
    )
    args = parser.parse_args()
    if args.backtest_offset < 0:
        parser.error("--backtest-offset must be zero or greater")
    if args.as_of and args.backtest_offset:
        parser.error("Use either --backtest-offset or --as-of, not both")
    if args.min_fvg_width_pct <= 0 or args.min_fvg_width_atr < 0 or args.min_fvg_body_atr < 0:
        parser.error("FVG width/body settings must be positive (ATR multiples may be zero)")
    if args.fvg_max_bars_after_ifvg < 2:
        parser.error("--fvg-max-bars-after-ifvg must be at least 2")
    try:
        telegram = load_telegram_config(args.env)
    except ValueError as error:
        parser.error(str(error))

    config = replace(
        PinBarConfig(),
        min_fvg_width_pct=args.min_fvg_width_pct,
        min_fvg_width_atr=args.min_fvg_width_atr,
        min_fvg_qualifying_body_atr=args.min_fvg_body_atr,
        fvg_max_bars_after_ifvg=args.fvg_max_bars_after_ifvg,
    )
    # ATR/context filters require sufficient candles before the tested candle.
    required_history = max(config.context_lookback, config.atr_period) + 1
    if args.detect_ifvg_fvg or args.only_ifvg_fvg:
        required_history = max(
            required_history,
            config.atr_period + config.source_fvg_lookback + 4,
        )
    # For an exact historical timestamp Binance needs enough data to include
    # it, so request the normal analysis window rather than just two candles.
    limit = 1000 if args.as_of else args.backtest_offset + required_history + 1
    data = fetch_futures_klines(args.symbol, args.interval, limit)
    try:
        candle = select_historical_candle(data, args.backtest_offset, args.as_of)
    except ValueError as error:
        parser.error(str(error))
    candle_position = int(data.index[data["open_time"] == candle["open_time"]][0])
    selected_data = data.iloc[:candle_position + 1].reset_index(drop=True)
    print(f"symbol={args.symbol.upper()} interval={args.interval} candle={candle.open_time:%Y-%m-%d %H:%M} UTC")
    if not args.only_ifvg_fvg:
        pinbar_signal = detect_latest_pinbar(selected_data, config)
        if pinbar_signal:
            signal_text = (
                f"{pinbar_signal.kind} | O={pinbar_signal.open:.6g} H={pinbar_signal.high:.6g} "
                f"L={pinbar_signal.low:.6g} C={pinbar_signal.close:.6g} | "
                f"range={pinbar_signal.range_pct:.2%} body={pinbar_signal.body:.6g} "
                f"upper_wick={pinbar_signal.upper_wick:.6g} lower_wick={pinbar_signal.lower_wick:.6g}"
            )
            print(signal_text)
            # Historical probes must never alert a real Telegram chat.  For a
            # normal run this is the newest completed K line, deduplicated by
            # symbol, interval, direction and candle open time.
            if args.as_of is None and args.backtest_offset == 0 and not args.no_notify:
                try:
                    if notify_pinbar_once(args.symbol, args.interval, pinbar_signal, telegram):
                        print("Telegram notification sent.")
                    elif telegram.enabled:
                        print("Telegram notification already sent for this candle.")
                except RuntimeError as error:
                    print(f"Telegram notification ERROR: {error}")
        else:
            print("No Pin Bar.")
    if args.detect_ifvg_fvg or args.only_ifvg_fvg:
        ifvg_fvg_signal = detect_latest_ifvg_fvg(selected_data, config)
        if ifvg_fvg_signal:
            print(
                f"{ifvg_fvg_signal.kind} | IFVG={ifvg_fvg_signal.ifvg.lower:.6g}-{ifvg_fvg_signal.ifvg.upper:.6g} "
                f"inverted={ifvg_fvg_signal.ifvg_inversion_time:%Y-%m-%d %H:%M} UTC | "
                f"FVG={ifvg_fvg_signal.fvg.lower:.6g}-{ifvg_fvg_signal.fvg.upper:.6g}"
            )
        else:
            print("No IFVG → FVG confirmation.")


if __name__ == "__main__":
    main()
