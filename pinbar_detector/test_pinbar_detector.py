from __future__ import annotations

import pandas as pd

from pinbar_detector import (
    PinBarConfig,
    PinBarSignal,
    detect_fvg_at,
    detect_latest_pinbar,
    detect_latest_fvg,
    detect_pinbar,
    format_pinbar_signal,
    load_notification_state,
    notify_pinbar_once,
    pinbar_notification_key,
    save_notification_state,
    select_historical_candle,
    TelegramConfig,
)


def _candle(open_: float, high: float, low: float, close: float) -> pd.Series:
    return pd.Series({
        "open_time": pd.Timestamp("2026-08-13 00:00", tz="UTC"),
        "open": open_, "high": high, "low": low, "close": close,
    })


def test_detects_bearish_pinbar() -> None:
    # Body=1; upper shadow=4; lower shadow=1; range=6%.
    result = detect_pinbar(_candle(100, 105, 99, 101))
    assert result is not None
    assert result.kind == "bearish_pinbar"


def test_detects_bullish_pinbar() -> None:
    # Body=1; lower shadow=4; upper shadow=1; range=6%.
    result = detect_pinbar(_candle(100, 102, 95, 101))
    assert result is not None
    assert result.kind == "bullish_pinbar"


def test_rejects_range_below_point_eight_percent() -> None:
    # Geometry is pin-bar-like but high-low range is only 0.6%.
    assert detect_pinbar(_candle(100, 100.5, 99.9, 100.1)) is None


def test_rejects_too_large_opposite_wick() -> None:
    # Bearish main wick=4, but lower wick=2 > 1.5 * body.
    assert detect_pinbar(_candle(100, 105, 98, 101)) is None


def test_rejects_doji() -> None:
    assert detect_pinbar(_candle(100, 106, 94, 100)) is None


def test_historical_offset_excludes_the_newest_requested_candles() -> None:
    data = pd.DataFrame({
        "open_time": pd.date_range("2026-08-13", periods=12, freq="h", tz="UTC"),
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
    })
    assert select_historical_candle(data, backtest_offset=0).open_time == data.iloc[-1].open_time
    assert select_historical_candle(data, backtest_offset=9).open_time == data.iloc[-10].open_time


def test_historical_as_of_selects_the_exact_utc_candle() -> None:
    data = pd.DataFrame({
        "open_time": pd.date_range("2026-08-13", periods=12, freq="h", tz="UTC"),
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
    })
    selected = select_historical_candle(data, as_of="2026-08-13T06:00:00Z")
    assert selected.open_time == pd.Timestamp("2026-08-13T06:00:00Z")


def _signal() -> PinBarSignal:
    return PinBarSignal("bullish_pinbar", pd.Timestamp("2026-08-13 00:00", tz="UTC"), 100, 102, 95, 101, 1, 1, 4, 0.07)


def test_pinbar_notification_key_includes_market_candle_and_direction() -> None:
    assert pinbar_notification_key("dogeusdt", "1h", _signal()) == "DOGEUSDT:1h:bullish_pinbar:2026-08-13T00:00:00+00:00"
    assert "DOGEUSDT 1h bullish_pinbar" in format_pinbar_signal("DOGEUSDT", "1h", _signal())


def test_disabled_telegram_never_sends_or_writes_state(tmp_path) -> None:
    config = TelegramConfig(False, None, None, "@apm", tmp_path / "state.json")
    assert not notify_pinbar_once("DOGEUSDT", "1h", _signal(), config)
    assert not config.state_file.exists()


def test_notification_state_round_trip(tmp_path) -> None:
    path = tmp_path / "state.json"
    state = {pinbar_notification_key("DOGEUSDT", "1h", _signal())}
    save_notification_state(path, state)
    assert load_notification_state(path) == state


def _context_frame(last: dict[str, float], *, prior_high: float = 101.0, prior_low: float = 99.0) -> pd.DataFrame:
    """Create enough quiet preceding candles for context and prior-ATR checks."""
    previous = pd.DataFrame({
        "open_time": pd.date_range("2026-08-12", periods=20, freq="h", tz="UTC"),
        "open": 100.0, "high": prior_high, "low": prior_low, "close": 100.1,
    })
    return pd.concat([previous, pd.DataFrame([{
        "open_time": pd.Timestamp("2026-08-13", tz="UTC"), **last,
    }])], ignore_index=True)


def test_latest_filter_accepts_contextual_bearish_pinbar() -> None:
    # High is the 12-candle high; close is in the lower 30%; range exceeds ATR.
    data = _context_frame({"open": 101.0, "high": 105.0, "low": 99.0, "close": 100.0})
    result = detect_latest_pinbar(data)
    assert result is not None
    assert result.kind == "bearish_pinbar"


def test_latest_filter_rejects_pinbar_with_close_in_wrong_end_of_range() -> None:
    # It has valid shadow ratios but closes above the lower 40% required for bear.
    data = _context_frame({"open": 100.0, "high": 105.0, "low": 99.0, "close": 102.0})
    assert detect_latest_pinbar(data) is None


def test_latest_filter_rejects_pinbar_not_at_context_extreme() -> None:
    data = _context_frame({"open": 101.0, "high": 105.0, "low": 99.0, "close": 100.0}, prior_high=106.0)
    assert detect_latest_pinbar(data) is None


def test_latest_filter_rejects_range_smaller_than_prior_atr() -> None:
    # Prior candles have a 10-point true range; final 6-point pin bar is too small.
    data = _context_frame(
        {"open": 101.0, "high": 105.0, "low": 99.0, "close": 100.0},
        prior_high=104.9, prior_low=94.9,
    )
    assert detect_latest_pinbar(data) is None


def _fvg_frame() -> pd.DataFrame:
    """Bearish body-FVG setup with overlapping shadows."""
    candles = [{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.2}] * 20
    candles += [
        # The first and third bodies form a bullish FVG [101, 104].
        {"open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0},
        {"open": 101.0, "high": 105.0, "low": 100.5, "close": 104.0},
        {"open": 104.0, "high": 106.0, "low": 103.0, "close": 105.0},
        # Recent 12-candle high: body=1, upper shadow=8, lower shadow=1.
        {"open": 104.0, "high": 112.0, "low": 102.0, "close": 103.0},
        # This candle is part of the subsequent bearish body FVG.
        {"open": 103.0, "high": 104.0, "low": 100.0, "close": 100.5},
        {"open": 101.0, "high": 102.0, "low": 99.0, "close": 99.5},
        # The body high (99) remains below candle -3's body low (100.5).
        {"open": 99.0, "high": 99.0, "low": 96.0, "close": 97.0},
    ]
    data = pd.DataFrame(candles)
    data.insert(0, "open_time", pd.date_range("2026-08-12", periods=len(data), freq="h", tz="UTC"))
    return data


def test_fvg_uses_bodies_allows_wick_overlap_and_enforces_minimum_width() -> None:
    data = _fvg_frame()
    data["prior_atr"] = 2.0
    config = PinBarConfig(min_fvg_width_pct=0.005, min_fvg_width_atr=0.2)
    fvg = detect_fvg_at(data, len(data) - 1, config)
    assert fvg is not None
    assert fvg.kind == "bearish_fvg"
    assert (fvg.lower, fvg.upper) == (99.0, 100.5)

    assert detect_fvg_at(data, len(data) - 1, PinBarConfig(min_fvg_width_pct=0.02)) is None
    assert detect_fvg_at(data, len(data) - 1, PinBarConfig(min_fvg_middle_body_atr=1.0)) is None


def test_fvg_atr_body_filters_use_outer_and_middle_thresholds() -> None:
    data = _fvg_frame()
    data["prior_atr"] = 2.0
    # Keep the bearish body gap. Outer bodies are 0.21 (> 0.10 * ATR),
    # while the middle displacement body remains 1.5 (> 0.25 * ATR).
    data.loc[len(data) - 3, ["open", "close"]] = [100.71, 100.5]
    data.loc[len(data) - 1, ["open", "high", "close"]] = [99.21, 99.3, 99.0]
    assert detect_fvg_at(data, len(data) - 1) is not None
    data.loc[len(data) - 1, ["open", "high", "close"]] = [99.19, 99.3, 99.0]
    assert detect_fvg_at(data, len(data) - 1) is None


def _extreme_fvg_frame(kind: str) -> pd.DataFrame:
    """Build 96 pre-FVG candles, with the five-bar extreme near the FVG."""
    candles = [{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.2}] * 96
    if kind == "bullish":
        # The 5 bars immediately before the FVG hold the 96-bar low.
        candles[93] = {"open": 92.0, "high": 93.0, "low": 90.0, "close": 91.0}
        candles += [
            {"open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0},
            {"open": 101.0, "high": 105.0, "low": 100.0, "close": 104.0},
            {"open": 104.0, "high": 106.0, "low": 103.0, "close": 105.0},
        ]
    else:
        # The 5 bars immediately before the FVG hold the 96-bar high.
        candles[93] = {"open": 108.0, "high": 110.0, "low": 107.0, "close": 109.0}
        candles += [
            {"open": 100.0, "high": 101.0, "low": 98.0, "close": 99.0},
            {"open": 99.0, "high": 100.0, "low": 95.0, "close": 96.0},
            {"open": 96.0, "high": 97.0, "low": 94.0, "close": 95.0},
        ]
    data = pd.DataFrame(candles)
    data.insert(0, "open_time", pd.date_range("2026-08-12", periods=len(data), freq="h", tz="UTC"))
    return data


def test_confirms_bullish_fvg_after_five_bar_96_bar_low() -> None:
    result = detect_latest_fvg(_extreme_fvg_frame("bullish"))
    assert result is not None
    assert result.kind == "bullish_fvg"
    assert (result.lower, result.upper) == (101.0, 104.0)


def test_confirms_bearish_fvg_after_five_bar_96_bar_high() -> None:
    result = detect_latest_fvg(_extreme_fvg_frame("bearish"))
    assert result is not None
    assert result.kind == "bearish_fvg"
    assert (result.lower, result.upper) == (96.0, 99.0)


def test_rejects_bullish_fvg_when_96_bar_low_is_not_in_previous_five_bars() -> None:
    data = _extreme_fvg_frame("bullish")
    data.loc[50, "low"] = 89.0
    assert detect_latest_fvg(data) is None


def test_rejects_bearish_fvg_when_96_bar_high_is_not_in_previous_five_bars() -> None:
    data = _extreme_fvg_frame("bearish")
    data.loc[50, "high"] = 111.0
    assert detect_latest_fvg(data) is None
