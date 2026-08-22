from __future__ import annotations

import pandas as pd

from pinbar_detector import (
    PinBarConfig,
    PinBarSignal,
    detect_fvg_at,
    detect_latest_pinbar,
    detect_latest_ifvg_fvg,
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


def _ifvg_fvg_frame() -> pd.DataFrame:
    """Bearish body-FVG setup with overlapping shadows."""
    candles = [{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.2}] * 20
    candles += [
        # The first and third bodies form a bullish FVG [101, 104].
        {"open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0},
        {"open": 101.0, "high": 105.0, "low": 100.5, "close": 104.0},
        {"open": 104.0, "high": 106.0, "low": 103.0, "close": 105.0},
        # Recent 12-candle high: body=1, upper shadow=8, lower shadow=1.
        {"open": 104.0, "high": 112.0, "low": 102.0, "close": 103.0},
        # This close below 101 converts the old bullish FVG into bearish IFVG.
        {"open": 103.0, "high": 104.0, "low": 100.0, "close": 100.5},
        {"open": 101.0, "high": 102.0, "low": 99.0, "close": 99.5},
        # The body high (99) remains below candle -3's body low (100.5).
        {"open": 99.0, "high": 99.0, "low": 96.0, "close": 97.0},
    ]
    data = pd.DataFrame(candles)
    data.insert(0, "open_time", pd.date_range("2026-08-12", periods=len(data), freq="h", tz="UTC"))
    return data


def test_fvg_uses_bodies_allows_wick_overlap_and_enforces_minimum_width() -> None:
    data = _ifvg_fvg_frame()
    data["prior_atr"] = 2.0
    config = PinBarConfig(min_fvg_width_pct=0.005, min_fvg_width_atr=0.2)
    fvg = detect_fvg_at(data, len(data) - 1, config)
    assert fvg is not None
    assert fvg.kind == "bearish_fvg"
    assert (fvg.lower, fvg.upper) == (99.0, 100.5)

    assert detect_fvg_at(data, len(data) - 1, PinBarConfig(min_fvg_width_pct=0.02)) is None
    assert detect_fvg_at(data, len(data) - 1, PinBarConfig(min_fvg_qualifying_body_atr=1.0)) is None


def test_confirms_bearish_ifvg_then_later_fvg_without_pinbar() -> None:
    data = _ifvg_fvg_frame()
    # This was the pin bar in the drawing, but the IFVG/FVG result must not
    # depend on its geometry or on it being a recent high.
    data.loc[len(data) - 4, ["open", "high", "low", "close"]] = [104.0, 108.0, 102.0, 106.0]
    result = detect_latest_ifvg_fvg(data)
    assert result is not None
    assert result.kind == "bearish_ifvg_fvg"
    assert result.ifvg.kind == "bullish_fvg"
    assert (result.ifvg.lower, result.ifvg.upper) == (101.0, 104.0)
    assert result.fvg.kind == "bearish_fvg"


def test_confirms_bullish_ifvg_then_later_fvg() -> None:
    candles = [{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.2}] * 20
    candles += [
        # The first and third bodies form a bearish FVG [96, 99].
        {"open": 100.0, "high": 101.0, "low": 98.0, "close": 99.0},
        {"open": 99.0, "high": 99.5, "low": 95.0, "close": 96.0},
        {"open": 96.0, "high": 97.0, "low": 94.0, "close": 95.0},
        # Close above 99 converts it to a bullish IFVG.
        {"open": 95.0, "high": 101.0, "low": 94.0, "close": 100.0},
        {"open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0},
        # The low (101) remains above candle -3's high (100): bullish FVG.
        {"open": 101.0, "high": 104.0, "low": 101.0, "close": 103.0},
    ]
    data = pd.DataFrame(candles)
    data.insert(0, "open_time", pd.date_range("2026-08-12", periods=len(data), freq="h", tz="UTC"))
    result = detect_latest_ifvg_fvg(data)
    assert result is not None
    assert result.kind == "bullish_ifvg_fvg"
    assert (result.ifvg.lower, result.ifvg.upper) == (96.0, 99.0)
    assert result.fvg.kind == "bullish_fvg"


def test_rejects_fvg_that_forms_before_ifvg_inversion() -> None:
    data = _ifvg_fvg_frame()
    # Keep the potential bearish FVG, but do not close through the earlier
    # bullish FVG until the FVG's first candle, which is too late to confirm.
    data.loc[len(data) - 3, "close"] = 102.0
    assert detect_latest_ifvg_fvg(data) is None
