import unittest

from main import (
    SIGNAL_CLOSE_DELAY_SECONDS,
    advance_scheduled_timestamp,
    next_signal_run_timestamp,
    restore_trend_state,
)
import pandas as pd


class MainScheduleTest(unittest.TestCase):
    def test_position_check_does_not_skip_signal_one_minute_after_close(self):
        hour = 3600
        signal_run = next_signal_run_timestamp(10 * hour + 55 * 60, hour)
        self.assertEqual(signal_run, 11 * hour + SIGNAL_CLOSE_DELAY_SECONDS)

        # The 11:00 five-minute check is before the scheduled 11:01 signal run.
        self.assertEqual(advance_scheduled_timestamp(11 * hour, 300, 11 * hour), 11 * hour + 300)
        self.assertEqual(signal_run, 11 * hour + SIGNAL_CLOSE_DELAY_SECONDS)

        # Only after the 11:01 run is completed does its schedule advance to 12:01.
        self.assertEqual(advance_scheduled_timestamp(signal_run, hour, signal_run), 12 * hour + SIGNAL_CLOSE_DELAY_SECONDS)

    def test_restore_trend_state_uses_latest_closed_crossover(self):
        index = pd.date_range('2026-08-04 06:00:00', periods=4, freq='h', tz='Asia/Taipei')
        df = pd.DataFrame(
            {
                'bullish_transition': [False, True, False, False],
                'bearish_transition': [False, False, False, True],  # Last row is unclosed and ignored.
            },
            index=index,
        )
        state = {'trend': 'none', 'last_cross_time': '', 'has_entered_this_phase': 1, 'position': 'flat'}

        self.assertTrue(restore_trend_state(state, df, 'TEST/USDT'))
        self.assertEqual(state['trend'], 'bullish')
        self.assertEqual(state['last_cross_time'], '2026-08-04 07:00:00')
        self.assertEqual(state['has_entered_this_phase'], 0)


if __name__ == '__main__':
    unittest.main()
