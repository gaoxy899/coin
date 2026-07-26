import unittest

import numpy as np

from kalman_trend import INITIAL_DYNAMIC_STOP_BARS, should_exit_by_dynamic_stop


class DynamicStopTest(unittest.TestCase):
    def setUp(self):
        self.open_vals = np.full(40, 100.0)
        self.close_vals = np.full(40, 100.0)
        self.short_k = np.full(40, 100.0)
        self.long_k = np.full(40, 100.0)
        self.high_vals = np.full(40, 101.0)
        self.low_vals = np.full(40, 99.0)

    def test_long_uses_two_requested_candles_below_slow_line_within_36_bars(self):
        self.open_vals[1:3] = [97.0, 98.0]
        self.close_vals[1:3] = [98.0, 99.0]

        self.assertTrue(
            should_exit_by_dynamic_stop(
                'long', 0, 2, self.open_vals, self.close_vals, self.short_k, self.long_k,
                self.high_vals, self.low_vals
            )
        )

    def test_short_requires_two_consecutive_requested_candles_within_36_bars(self):
        self.open_vals[1:3] = [101.0, 98.0]
        self.close_vals[1:3] = [100.0, 99.0]
        self.assertFalse(
            should_exit_by_dynamic_stop(
                'short', 0, 2, self.open_vals, self.close_vals, self.short_k, self.long_k,
                self.high_vals, self.low_vals
            )
        )

        self.open_vals[1] = 98.0
        self.close_vals[1] = 99.0
        self.assertTrue(
            should_exit_by_dynamic_stop(
                'short', 0, 2, self.open_vals, self.close_vals, self.short_k, self.long_k,
                self.high_vals, self.low_vals
            )
        )

    def test_long_lower_shadow_pinbar_on_second_candle_keeps_position_open(self):
        self.open_vals[1:3] = [97.0, 98.0]
        self.close_vals[1:3] = [98.0, 99.0]
        self.low_vals[2] = 93.0  # 5x body (99 - 98) lower shadow

        self.assertFalse(
            should_exit_by_dynamic_stop(
                'long', 0, 2, self.open_vals, self.close_vals, self.short_k, self.long_k,
                self.high_vals, self.low_vals
            )
        )

    def test_short_upper_shadow_pinbar_on_second_candle_keeps_position_open(self):
        self.open_vals[1:3] = [97.0, 98.0]
        self.close_vals[1:3] = [98.0, 99.0]
        self.high_vals[2] = 104.0  # 5x body (99 - 98) upper shadow

        self.assertFalse(
            should_exit_by_dynamic_stop(
                'short', 0, 2, self.open_vals, self.close_vals, self.short_k, self.long_k,
                self.high_vals, self.low_vals
            )
        )

    def test_bar_37_reverts_to_existing_fast_line_stop(self):
        current_idx = INITIAL_DYNAMIC_STOP_BARS + 1
        self.open_vals[current_idx] = 98.0
        self.close_vals[current_idx] = 99.0

        self.assertTrue(
            should_exit_by_dynamic_stop(
                'long', 0, current_idx, self.open_vals, self.close_vals, self.short_k, self.long_k,
                self.high_vals, self.low_vals
            )
        )


if __name__ == '__main__':
    unittest.main()
