from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from macd_pinbar_monitor import PendingDivergence, compatible, load_state, open_time_after_bars, save_state, state_key
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pinbar_detector"))
from pinbar_detector import PinBarSignal


def _pinbar(kind: str) -> PinBarSignal:
    return PinBarSignal(kind, pd.Timestamp("2026-08-13", tz="UTC"), 1, 2, 0, 1, 1, 1, 1, 1)  # type: ignore[arg-type]


def test_requires_matching_direction() -> None:
    assert compatible("bullish", _pinbar("bullish_pinbar"))
    assert compatible("bearish", _pinbar("bearish_pinbar"))
    assert not compatible("bullish", _pinbar("bearish_pinbar"))
    assert not compatible("bearish", None)


def test_pending_state_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = {state_key("solusdt", "1h"): PendingDivergence("bullish", "2026-08-13T00:00:00+00:00", "2026-08-13T02:00:00+00:00", "signal")}
    save_state(path, state)
    assert load_state(path) == state


def test_confirmation_window_uses_interval_bars() -> None:
    assert open_time_after_bars("2026-08-13T00:00:00+00:00", "1h", 2) == "2026-08-13T02:00:00+00:00"
    assert open_time_after_bars("2026-08-13T00:00:00+00:00", "4h", 2) == "2026-08-13T08:00:00+00:00"
