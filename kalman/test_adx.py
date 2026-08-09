import unittest

import numpy as np
import pandas as pd

from kalman_trend import ADX_ENTRY_THRESHOLD, calculate_adx_sma, simulate_strategy


class AdxTest(unittest.TestCase):
    def test_sma_adx_is_available_after_two_14_bar_windows(self):
        close = pd.Series(np.arange(100.0, 140.0))
        df = pd.DataFrame(
            {
                'open': close - 0.2,
                'high': close + 0.5,
                'low': close - 0.5,
                'close': close,
            }
        )

        adx = calculate_adx_sma(df)

        self.assertTrue(pd.isna(adx.iloc[25]))
        self.assertGreater(adx.iloc[-1], ADX_ENTRY_THRESHOLD)

    def test_adx_confirms_transition_not_the_later_pullback_entry(self):
        def strategy_frame(transition_confirmed):
            return pd.DataFrame(
                {
                    'open': [100.0, 100.0, 100.0],
                    'high': [101.0, 101.0, 101.0],
                    'low': [99.0, 99.0, 99.0],
                    'close': [100.0, 100.0, 100.0],
                    'short_kalman': [100.0, 100.0, 100.0],
                    'long_kalman': [90.0, 90.0, 90.0],
                    'adx_sma_14': [19.0, 5.0, 5.0],
                    'kalman_bullish_transition': [True, False, False],
                    'kalman_bearish_transition': [False, False, False],
                    'bullish_transition': [transition_confirmed, False, False],
                    'bearish_transition': [False, False, False],
                    'trend_up': [True, True, True],
                }
            )

        blocked = simulate_strategy(strategy_frame(False))
        allowed = simulate_strategy(strategy_frame(True))

        self.assertFalse(blocked['strat_long_entry'].any())
        self.assertTrue(allowed['strat_long_entry'].iloc[1])


if __name__ == '__main__':
    unittest.main()
