from __future__ import annotations

import numpy as np
import pandas as pd

from datetime import datetime, timezone

from macd_divergence import (
    DetectorConfig,
    Divergence,
    detect_regular_divergences,
    latest_divergence,
    next_check_time,
    MonitorConfig,
    send_telegram_message,
    signal_on_latest_closed_candle,
    format_signal,
    simulate_historical_as_of,
    simulate_historical_at_time,
)


def _fixture(macd: np.ndarray, macd_signal: np.ndarray | None = None) -> pd.DataFrame:
    """Build candles with two price highs at index 10 and index 39."""
    length = len(macd)
    high, low = np.full(length, 100.0), np.full(length, 95.0)
    high[10], high[39] = 110.0, 114.0
    return pd.DataFrame({
        "open_time": pd.date_range("2026-01-01", periods=length, freq="h", tz="UTC"),
        "high": high, "low": low, "close": (high + low) / 2,
        "macd": macd,
        "macd_signal": np.full(length, 0.2) if macd_signal is None else macd_signal,
        "atr": np.ones(length),
    })


def test_bearish_divergence_is_confirmed_by_death_cross_without_right_pivots() -> None:
    # Two positive MACD lobes: peaks at 10 and 35; death crosses at 14 and 39.
    macd, signal = np.full(50, 0.3), np.full(50, 0.6)
    macd[1:14], signal[1:14] = 0.8, 0.5
    macd[10] = 2.0
    macd[20:39], signal[20:39] = 0.8, 0.5
    macd[35] = 1.4
    macd[39], signal[39] = 0.4, 0.5
    config = DetectorConfig(
        min_price_swing_atr=0.1, min_macd_value_atr=0.1, min_macd_swing_atr=0.1,
        min_price_change_pct=0.01,
    )
    signals = detect_regular_divergences(_fixture(macd, signal), config)
    assert [item.kind for item in signals] == ["bearish"]
    assert signals[0].second_time == pd.Timestamp("2026-01-02 15:00", tz="UTC")
    assert signals[0].confirmation_time == pd.Timestamp("2026-01-02 15:00", tz="UTC")

    macd[23] = -0.01
    assert not detect_regular_divergences(_fixture(macd, signal), config)


def test_historical_simulation_hides_the_requested_newest_candles() -> None:
    frame = _fixture(np.full(50, 0.2))
    simulated = simulate_historical_as_of(frame, 7)
    assert len(simulated) == 43
    assert simulated.iloc[-1]["open_time"] == frame.iloc[-8]["open_time"]


def test_historical_as_of_includes_the_exact_requested_timezone_aware_candle() -> None:
    frame = _fixture(np.full(50, 0.2))
    target = frame.iloc[20]["open_time"]
    simulated = simulate_historical_at_time(frame, target.isoformat())
    assert len(simulated) == 21
    assert simulated.iloc[-1]["open_time"] == target


def test_price_led_bullish_divergence_after_a_golden_cross() -> None:
    """Price may set its second low after MACD's below-zero golden cross."""
    length = 50
    macd, signal = np.full(length, -0.2), np.full(length, -0.5)
    # Negative lobe from death cross 5 to golden cross 20, MACD low at 15.
    macd[:5], signal[:5] = 0.2, 0.0
    macd[5:20], signal[5:20] = -0.8, -0.5
    macd[15] = -2.0
    macd[20], signal[20] = -0.2, -0.5
    # MACD weakens again but stays well above the first low as price undercuts.
    macd[35], signal[35] = -0.5, -0.6
    high, low = np.full(length, 100.0), np.full(length, 95.0)
    low[10], low[35] = 90.0, 88.0
    data = pd.DataFrame({
        "open_time": pd.date_range("2026-01-01", periods=length, freq="h", tz="UTC"),
        "high": high, "low": low, "close": (high + low) / 2,
        "macd": macd, "macd_signal": signal, "atr": np.ones(length),
    })
    config = DetectorConfig(
        min_price_swing_atr=0.1, min_macd_value_atr=0.1, min_macd_swing_atr=0.1,
        min_price_change_pct=0.005, min_price_divergence_atr=0.1,
    )
    signals = detect_regular_divergences(data, config)
    result = [item for item in signals if item.kind == "bullish"]
    assert len(result) == 1
    assert result[0].second_time == pd.Timestamp("2026-01-02 11:00", tz="UTC")
    assert result[0].confirmation_time == result[0].second_time
    assert result[0].macd_cross_time == pd.Timestamp("2026-01-01 20:00", tz="UTC")


def test_latest_divergence_returns_only_the_most_recent_requested_direction() -> None:
    early = Divergence("bullish", pd.Timestamp("2026-01-01", tz="UTC"), pd.Timestamp("2026-01-02", tz="UTC"), pd.Timestamp("2026-01-01", tz="UTC"), pd.Timestamp("2026-01-02", tz="UTC"), 1, 0.9, -2, -1, 24, pd.Timestamp("2026-01-02", tz="UTC"), pd.Timestamp("2026-01-01 12:00", tz="UTC"))
    latest = Divergence("bearish", pd.Timestamp("2026-01-03", tz="UTC"), pd.Timestamp("2026-01-04", tz="UTC"), pd.Timestamp("2026-01-03", tz="UTC"), pd.Timestamp("2026-01-04", tz="UTC"), 1, 1.1, 2, 1, 24, pd.Timestamp("2026-01-04", tz="UTC"), pd.Timestamp("2026-01-04", tz="UTC"))
    assert latest_divergence([early, latest]) == latest
    assert latest_divergence([early, latest], "bullish") == early


def test_output_labels_price_and_green_macd_extreme_times_separately() -> None:
    signal = Divergence(
        "bullish", pd.Timestamp("2026-08-07 16:00", tz="UTC"), pd.Timestamp("2026-08-11 12:00", tz="UTC"),
        pd.Timestamp("2026-08-07 20:00", tz="UTC"), pd.Timestamp("2026-08-11 12:00", tz="UTC"),
        1.012, 0.9886, -0.0127671, -0.0100345, 23,
        pd.Timestamp("2026-08-12", tz="UTC"), pd.Timestamp("2026-08-12", tz="UTC"),
    )
    text = format_signal(signal, "Asia/Taipei")
    assert "price_extreme=2026-08-08 00:00" in text
    assert "green_MACD_extreme=2026-08-08 04:00" in text


def test_live_check_does_not_repeat_a_historical_divergence() -> None:
    macd, signal = np.full(50, 0.3), np.full(50, 0.6)
    macd[1:14], signal[1:14] = 0.8, 0.5
    macd[10] = 2.0
    macd[20:39], signal[20:39] = 0.8, 0.5
    macd[35] = 1.4
    macd[39], signal[39] = 0.4, 0.5
    config = DetectorConfig(min_price_swing_atr=0.1, min_macd_value_atr=0.1, min_macd_swing_atr=0.1)
    data = _fixture(macd, signal)
    assert signal_on_latest_closed_candle(data, config) is None
    assert signal_on_latest_closed_candle(data.iloc[:40].copy(), config) is not None


def test_next_check_time_is_aligned_to_close_plus_delay() -> None:
    now = datetime(2026, 8, 13, 1, 1, tzinfo=timezone.utc)
    assert next_check_time("1h", 2, now) == datetime(2026, 8, 13, 1, 2, tzinfo=timezone.utc)
    assert next_check_time("1h", 2, datetime(2026, 8, 13, 1, 2, tzinfo=timezone.utc)) == datetime(2026, 8, 13, 2, 2, tzinfo=timezone.utc)
    assert next_check_time("4h", 2, now) == datetime(2026, 8, 13, 4, 2, tzinfo=timezone.utc)


def test_disabled_telegram_does_not_make_a_network_request() -> None:
    monitor = MonitorConfig(("SOLUSDT",), ("1h",), 2, 1000, "Asia/Taipei", False, False, None, None, "@apm")
    send_telegram_message("test", monitor)
