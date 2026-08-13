from __future__ import annotations

import pandas as pd

from pinbar_detector import PinBarConfig, detect_latest_pinbar, detect_pinbar, select_historical_candle


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
