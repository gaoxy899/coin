import pandas as pd
import numpy as np

def kalman_filter(src: np.ndarray, length: int, R: float = 0.01, Q: float = 0.1) -> np.ndarray:
    """
    Kalman filter implementation matching the Pine Script formulation.
    """
    n = len(src)
    estimate = np.full(n, np.nan)
    error_est = 1.0
    error_meas = R * length
    
    # Initialize the estimate with the first non-NaN value
    first_valid_idx = -1
    for idx in range(n):
        if not np.isnan(src[idx]):
            first_valid_idx = idx
            break
            
    if first_valid_idx == -1 or first_valid_idx == n - 1:
        return estimate  # Entirely NaN or unable to initialize

    # Set up initial estimate (equivalent to estimate := src[1] initialization at first valid bar)
    current_est = src[first_valid_idx]
    
    for i in range(first_valid_idx + 1, n):
        val = src[i]
        if np.isnan(val):
            estimate[i] = current_est
            continue
            
        prediction = current_est
        kalman_gain = error_est / (error_est + error_meas)
        current_est = prediction + kalman_gain * (val - prediction)
        error_est = (1.0 - kalman_gain) * error_est + Q / length
        estimate[i] = current_est
        
    return estimate

def calculate_atr(df: pd.DataFrame, length: int = 200) -> pd.Series:
    """
    Calculates Wilder's Average True Range (ATR) matching Pine Script ta.atr.
    """
    high = df['high'].values
    low = df['low'].values
    close = df['close'].values
    n = len(df)
    
    tr = np.zeros(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1])
        )
        
    # Wilders MA / RMA
    rma = np.full(n, np.nan)
    if n >= length:
        rma[length - 1] = np.mean(tr[:length])
        alpha = 1.0 / length
        for i in range(length, n):
            rma[i] = alpha * tr[i] + (1 - alpha) * rma[i - 1]
            
    return pd.Series(rma, index=df.index)

def apply_kalman_trend_indicator(
    df: pd.DataFrame,
    short_len: int = 50,
    long_len: int = 150,
    retest_sig: bool = False,
    candle_color_enabled: bool = True,
    upper_col: str = "#13bd6e",
    lower_col: str = "#af0d4b"
) -> pd.DataFrame:
    """
    Translates the Pine Script Kalman Filter and Box Retest Trend Indicator into Python.
    Accepts a pandas DataFrame with datetime index and columns ['open', 'high', 'low', 'close'].
    Returns a copy of the DataFrame with added metrics, colors, signals and label values.
    """
    result = df.copy()
    close_vals = result['close'].values
    high_vals = result['high'].values
    low_vals = result['low'].values
    n = len(result)
    
    # 1. Calculations
    half_atr = calculate_atr(result, 200).values * 0.5
    short_kalman = kalman_filter(close_vals, short_len)
    long_kalman = kalman_filter(close_vals, long_len)
    
    result['half_atr'] = half_atr
    result['short_kalman'] = short_kalman
    result['long_kalman'] = long_kalman
    
    # 2. Trend & Colors
    # trend_up = short_kalman > long_kalman
    trend_up = np.zeros(n, dtype=bool)
    trend_col = np.full(n, "", dtype=object)
    trend_col1 = np.full(n, "", dtype=object)
    candle_col = np.full(n, "", dtype=object)
    
    # Signals & Boxes Data
    bullish_transition = np.zeros(n, dtype=bool)
    bearish_transition = np.zeros(n, dtype=bool)
    label_text = np.full(n, None)
    
    # Box Coordinates (Left/Right bars indexes and Top/Bottom prices)
    lower_box_top = np.full(n, np.nan)
    lower_box_bottom = np.full(n, np.nan)
    lower_box_left = np.full(n, np.nan)
    lower_box_right = np.full(n, np.nan)
    
    upper_box_top = np.full(n, np.nan)
    upper_box_bottom = np.full(n, np.nan)
    upper_box_left = np.full(n, np.nan)
    upper_box_right = np.full(n, np.nan)
    
    # Retest signals
    retest_x_signal = np.zeros(n, dtype=bool)
    retest_plus_signal = np.zeros(n, dtype=bool)
    
    # Persistent Box State
    curr_low_box = None  # (top, bottom, left_idx, right_idx)
    curr_up_box = None   # (top, bottom, left_idx, right_idx)
    
    for i in range(n):
        if np.isnan(short_kalman[i]) or np.isnan(long_kalman[i]):
            trend_up[i] = False
            trend_col[i] = "na"
            trend_col1[i] = "na"
            candle_col[i] = "na"
            continue
            
        is_up = short_kalman[i] > long_kalman[i]
        trend_up[i] = is_up
        
        # Color states
        trend_col[i] = upper_col if is_up else lower_col
        
        # trend_col1 = short_kalman > short_kalman[2] ? upper_col : lower_col
        short_prev2 = short_kalman[i - 2] if i >= 2 else np.nan
        if not np.isnan(short_prev2):
            trend_col1[i] = upper_col if short_kalman[i] > short_prev2 else lower_col
        else:
            trend_col1[i] = "na"
            
        # candle_col evaluation
        if candle_color_enabled and not np.isnan(short_prev2):
            cond_up = is_up and short_kalman[i] > short_prev2
            cond_dn = (not is_up) and short_kalman[i] < short_prev2
            candle_col[i] = upper_col if cond_up else (lower_col if cond_dn else "gray")
        else:
            candle_col[i] = "na"
            
        # Transition Label Logic
        prev_up = trend_up[i - 1] if i > 0 else False
        is_bullish_crossover = is_up and not prev_up
        is_bearish_crossover = prev_up and not is_up
        
        if is_bullish_crossover:
            bullish_transition[i] = True
            label_text[i] = f"Bullish Cross ({round(close_vals[i], 1)})"
            # lower_box := box.new(bar_index, low+atr, bar_index, low)
            curr_low_box = {
                'top': low_vals[i] + (half_atr[i] if not np.isnan(half_atr[i]) else 0),
                'bottom': low_vals[i],
                'left': i,
                'right': i
            }
            
        if is_bearish_crossover:
            bearish_transition[i] = True
            label_text[i] = f"Bearish Cross ({round(close_vals[i], 1)})"
            # upper_box := box.new(bar_index, high, bar_index, high-atr)
            curr_up_box = {
                'top': high_vals[i],
                'bottom': high_vals[i] - (half_atr[i] if not np.isnan(half_atr[i]) else 0),
                'left': i,
                'right': i
            }
            
        # Box extensions when the trend doesn't change
        # if not ta.change(trend_up) => trend_up == trend_up[1]
        if i > 0 and (trend_up[i] == trend_up[i - 1]):
            if curr_low_box is not None:
                curr_low_box['right'] = i
            if curr_up_box is not None:
                curr_up_box['right'] = i
                
        # Save historical box levels for output
        if curr_low_box is not None:
            lower_box_top[i] = curr_low_box['top']
            lower_box_bottom[i] = curr_low_box['bottom']
            lower_box_left[i] = curr_low_box['left']
            lower_box_right[i] = curr_low_box['right']
            
        if curr_up_box is not None:
            upper_box_top[i] = curr_up_box['top']
            upper_box_bottom[i] = curr_up_box['bottom']
            upper_box_left[i] = curr_up_box['left']
            upper_box_right[i] = curr_up_box['right']
            
        # Retest Signal Logic (placed on bar_index - 1)
        if retest_sig and i > 0:
            # Bearish retest based on upper_box
            if curr_up_box is not None and not np.isnan(curr_up_box['bottom']):
                box_b = curr_up_box['bottom']
                if high_vals[i] < box_b and high_vals[i - 1] >= box_b:
                    retest_x_signal[i - 1] = True
                    
            # Bullish retest based on lower_box
            if curr_low_box is not None and not np.isnan(curr_low_box['top']):
                box_t = curr_low_box['top']
                if low_vals[i] > box_t and low_vals[i - 1] <= box_t:
                    retest_plus_signal[i - 1] = True
                    
    result['trend_up'] = trend_up
    result['trend_col'] = trend_col
    result['trend_col1'] = trend_col1
    result['candle_col'] = candle_col
    result['bullish_transition'] = bullish_transition
    result['bearish_transition'] = bearish_transition
    result['label_text'] = label_text
    result['lower_box_top'] = lower_box_top
    result['lower_box_bottom'] = lower_box_bottom
    result['lower_box_left'] = lower_box_left
    result['lower_box_right'] = lower_box_right
    result['upper_box_top'] = upper_box_top
    result['upper_box_bottom'] = upper_box_bottom
    result['upper_box_left'] = upper_box_left
    result['upper_box_right'] = upper_box_right
    result['retest_x_signal'] = retest_x_signal
    result['retest_plus_signal'] = retest_plus_signal
    
    return result

