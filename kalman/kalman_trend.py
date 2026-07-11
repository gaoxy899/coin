import pandas as pd
import numpy as np

def kalman_filter(src: np.ndarray, length: int, R: float = 0.01, Q: float = 0.1) -> np.ndarray:
    """
    Pine Script compatible Kalman Filter implementation.
    """
    n = len(src)
    estimate = np.full(n, np.nan)
    error_est = 1.0
    error_meas = R * length
    
    first_valid_idx = -1
    for idx in range(n):
        if not np.isnan(src[idx]):
            first_valid_idx = idx
            break
            
    if first_valid_idx == -1 or first_valid_idx == n - 1:
        return estimate

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
    Pine Script ta.atr equivalent (Wilder's Moving Average ATR).
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
        
    rma = np.full(n, np.nan)
    if n >= length:
        rma[length - 1] = np.mean(tr[:length])
        alpha = 1.0 / length
        for i in range(length, n):
            rma[i] = alpha * tr[i] + (1 - alpha) * rma[i - 1]
            
    return pd.Series(rma, index=df.index)

def simulate_strategy(df: pd.DataFrame) -> pd.DataFrame:
    """
    Simulates the Kalman pullback entry strategy:
    - Entry window: touch short_kalman within 30 periods after transition crossover.
    - Risk & reward (1:1, 1:2) SL/TP calculations.
    - Double close filter: exit if both open and close cross short_kalman.
    """
    n = len(df)
    close_vals = df['close'].values
    high_vals = df['high'].values
    low_vals = df['low'].values
    open_vals = df['open'].values
    short_k = df['short_kalman'].values
    long_k = df['long_kalman'].values
    bull_trans = df['bullish_transition'].values
    bear_trans = df['bearish_transition'].values
    trend_up = df['trend_up'].values
    
    strat_pos = np.zeros(n)
    strat_entry_price = np.full(n, np.nan)
    strat_sl_price = np.full(n, np.nan)
    strat_tp1_price = np.full(n, np.nan)
    strat_tp2_price = np.full(n, np.nan)
    
    strat_long_entry = np.zeros(n, dtype=bool)
    strat_short_entry = np.zeros(n, dtype=bool)
    strat_exit_signal = np.full(n, None)  # None, or 'SL', 'TP2', 'SL_Line', 'Reversal'
    strat_tp1_hit = np.zeros(n, dtype=bool)
    strat_tp2_hit = np.zeros(n, dtype=bool)
    
    pos = 0 # 0: flat, 1: long, -1: short
    entry_price = 0.0
    sl_price = 0.0
    tp1_price = 0.0
    tp2_price = 0.0
    tp1_hit = False
    
    last_cross_idx = -1
    has_entered_this_phase = False
    
    for i in range(n):
        if np.isnan(short_k[i]) or np.isnan(long_k[i]):
            strat_pos[i] = 0
            continue
            
        # 1. Crossovers and Reversals
        if bull_trans[i]:
            last_cross_idx = i
            has_entered_this_phase = False
            if pos == -1:
                strat_exit_signal[i] = 'Reversal'
                pos = 0
        elif bear_trans[i]:
            last_cross_idx = i
            has_entered_this_phase = False
            if pos == 1:
                strat_exit_signal[i] = 'Reversal'
                pos = 0
                
        # 2. Check exits
        if pos == 1:
            if low_vals[i] <= sl_price:
                strat_exit_signal[i] = 'SL'
                pos = 0
            elif high_vals[i] >= tp2_price:
                strat_tp2_hit[i] = True
                strat_exit_signal[i] = 'TP2'
                pos = 0
            elif high_vals[i] >= tp1_price:
                if not tp1_hit:
                    strat_tp1_hit[i] = True
                    tp1_hit = True
                if open_vals[i] < short_k[i] and close_vals[i] < short_k[i]:
                    strat_exit_signal[i] = 'SL_Line'
                    pos = 0
            else:
                if open_vals[i] < short_k[i] and close_vals[i] < short_k[i]:
                    strat_exit_signal[i] = 'SL_Line'
                    pos = 0
                    
        elif pos == -1:
            if high_vals[i] >= sl_price:
                strat_exit_signal[i] = 'SL'
                pos = 0
            elif low_vals[i] <= tp2_price:
                strat_tp2_hit[i] = True
                strat_exit_signal[i] = 'TP2'
                pos = 0
            elif low_vals[i] <= tp1_price:
                if not tp1_hit:
                    strat_tp1_hit[i] = True
                    tp1_hit = True
                if open_vals[i] > short_k[i] and close_vals[i] > short_k[i]:
                    strat_exit_signal[i] = 'SL_Line'
                    pos = 0
            else:
                if open_vals[i] > short_k[i] and close_vals[i] > short_k[i]:
                    strat_exit_signal[i] = 'SL_Line'
                    pos = 0
                    
        # 3. Check Entries
        if pos == 0:
            if last_cross_idx != -1 and (0 < i - last_cross_idx <= 30) and not has_entered_this_phase:
                if trend_up[i]:  # Bullish -> Long Entry
                    if low_vals[i] <= short_k[i]:
                        pos = 1
                        entry_price = short_k[i]
                        has_entered_this_phase = True
                        
                        # SL 2% below long, max 4% total risk
                        sl_price = max(long_k[i] * 0.98, entry_price * 0.96)
                        if sl_price >= entry_price * 0.995:
                            sl_price = entry_price * 0.98
                        
                        risk = entry_price - sl_price
                        tp1_price = entry_price + risk
                        tp2_price = entry_price + 2 * risk
                        tp1_hit = False
                        strat_long_entry[i] = True
                        
                        # Same bar exits evaluation
                        if low_vals[i] <= sl_price:
                            strat_exit_signal[i] = 'SL'
                            pos = 0
                        elif high_vals[i] >= tp2_price:
                            strat_tp2_hit[i] = True
                            strat_exit_signal[i] = 'TP2'
                            pos = 0
                        elif high_vals[i] >= tp1_price:
                            strat_tp1_hit[i] = True
                            tp1_hit = True
                            if open_vals[i] < short_k[i] and close_vals[i] < short_k[i]:
                                strat_exit_signal[i] = 'SL_Line'
                                pos = 0
                        else:
                            if open_vals[i] < short_k[i] and close_vals[i] < short_k[i]:
                                strat_exit_signal[i] = 'SL_Line'
                                pos = 0
                else:  # Bearish -> Short Entry
                    if high_vals[i] >= short_k[i]:
                        pos = -1
                        entry_price = short_k[i]
                        has_entered_this_phase = True
                        
                        # SL 2% above long, max 4% total risk
                        sl_price = min(long_k[i] * 1.02, entry_price * 1.04)
                        if sl_price <= entry_price * 1.005:
                            sl_price = entry_price * 1.02
                            
                        risk = sl_price - entry_price
                        tp1_price = entry_price - risk
                        tp2_price = entry_price - 2 * risk
                        tp1_hit = False
                        strat_short_entry[i] = True
                        
                        # Same bar exits evaluation
                        if high_vals[i] >= sl_price:
                            strat_exit_signal[i] = 'SL'
                            pos = 0
                        elif low_vals[i] <= tp2_price:
                            strat_tp2_hit[i] = True
                            strat_exit_signal[i] = 'TP2'
                            pos = 0
                        elif low_vals[i] <= tp1_price:
                            strat_tp1_hit[i] = True
                            tp1_hit = True
                            if open_vals[i] > short_k[i] and close_vals[i] > short_k[i]:
                                strat_exit_signal[i] = 'SL_Line'
                                pos = 0
                        else:
                            if open_vals[i] > short_k[i] and close_vals[i] > short_k[i]:
                                strat_exit_signal[i] = 'SL_Line'
                                pos = 0
                                
        strat_pos[i] = pos
        if pos != 0:
            strat_entry_price[i] = entry_price
            strat_sl_price[i] = sl_price
            strat_tp1_price[i] = tp1_price
            strat_tp2_price[i] = tp2_price
            
    df['strat_pos'] = strat_pos
    df['strat_entry_price'] = strat_entry_price
    df['strat_sl_price'] = strat_sl_price
    df['strat_tp1_price'] = strat_tp1_price
    df['strat_tp2_price'] = strat_tp2_price
    df['strat_long_entry'] = strat_long_entry
    df['strat_short_entry'] = strat_short_entry
    df['strat_entry_signal'] = strat_long_entry | strat_short_entry
    df['strat_exit_signal'] = strat_exit_signal
    df['strat_tp1_hit'] = strat_tp1_hit
    df['strat_tp2_hit'] = strat_tp2_hit
    
    return df

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
    Computes indicators, zones, and retests.
    """
    result = df.copy()
    close_vals = result['close'].values
    high_vals = result['high'].values
    low_vals = result['low'].values
    n = len(result)
    
    half_atr = calculate_atr(result, 200).values * 0.5
    short_kalman = kalman_filter(close_vals, short_len)
    long_kalman = kalman_filter(close_vals, long_len)
    
    result['half_atr'] = half_atr
    result['short_kalman'] = short_kalman
    result['long_kalman'] = long_kalman
    
    trend_up = np.zeros(n, dtype=bool)
    trend_col = np.full(n, "", dtype=object)
    trend_col1 = np.full(n, "", dtype=object)
    candle_col = np.full(n, "", dtype=object)
    
    bullish_transition = np.zeros(n, dtype=bool)
    bearish_transition = np.zeros(n, dtype=bool)
    label_text = np.full(n, None)
    
    lower_box_top = np.full(n, np.nan)
    lower_box_bottom = np.full(n, np.nan)
    lower_box_left = np.full(n, np.nan)
    lower_box_right = np.full(n, np.nan)
    
    upper_box_top = np.full(n, np.nan)
    upper_box_bottom = np.full(n, np.nan)
    upper_box_left = np.full(n, np.nan)
    upper_box_right = np.full(n, np.nan)
    
    retest_x_signal = np.zeros(n, dtype=bool)
    retest_plus_signal = np.zeros(n, dtype=bool)
    
    curr_low_box = None
    curr_up_box = None
    
    for i in range(n):
        if np.isnan(short_kalman[i]) or np.isnan(long_kalman[i]):
            trend_up[i] = False
            trend_col[i] = "na"
            trend_col1[i] = "na"
            candle_col[i] = "na"
            continue
            
        is_up = short_kalman[i] > long_kalman[i]
        trend_up[i] = is_up
        trend_col[i] = upper_col if is_up else lower_col
        
        short_prev2 = short_kalman[i - 2] if i >= 2 else np.nan
        if not np.isnan(short_prev2):
            trend_col1[i] = upper_col if short_kalman[i] > short_prev2 else lower_col
        else:
            trend_col1[i] = "na"
            
        if candle_color_enabled and not np.isnan(short_prev2):
            cond_up = is_up and short_kalman[i] > short_prev2
            cond_dn = (not is_up) and short_kalman[i] < short_prev2
            candle_col[i] = upper_col if cond_up else (lower_col if cond_dn else "gray")
        else:
            candle_col[i] = "na"
            
        prev_up = trend_up[i - 1] if i > 0 else False
        is_bullish_crossover = is_up and not prev_up
        is_bearish_crossover = prev_up and not is_up
        
        if is_bullish_crossover:
            bullish_transition[i] = True
            label_text[i] = f"🡹 {round(close_vals[i], 1)}"
            curr_low_box = {
                'top': low_vals[i] + (half_atr[i] if not np.isnan(half_atr[i]) else 0),
                'bottom': low_vals[i],
                'left': i,
                'right': i
            }
            
        if is_bearish_crossover:
            bearish_transition[i] = True
            label_text[i] = f"{round(close_vals[i], 1)} 🢃"
            curr_up_box = {
                'top': high_vals[i],
                'bottom': high_vals[i] - (half_atr[i] if not np.isnan(half_atr[i]) else 0),
                'left': i,
                'right': i
            }
            
        if i > 0 and (trend_up[i] == trend_up[i - 1]):
            if curr_low_box is not None:
                curr_low_box['right'] = i
            if curr_up_box is not None:
                curr_up_box['right'] = i
                
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
            
        if retest_sig and i > 0:
            if curr_up_box is not None and not np.isnan(curr_up_box['bottom']):
                box_b = curr_up_box['bottom']
                if high_vals[i] < box_b and high_vals[i - 1] >= box_b:
                    # Support/resistance length must be at least 24 periods
                    if (i - curr_up_box['left']) >= 24:
                        retest_x_signal[i - 1] = True
                    
            if curr_low_box is not None and not np.isnan(curr_low_box['top']):
                box_t = curr_low_box['top']
                if low_vals[i] > box_t and low_vals[i - 1] <= box_t:
                    # Support/resistance length must be at least 24 periods
                    if (i - curr_low_box['left']) >= 24:
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
    
    # 4. Integrate strategy simulation
    result = simulate_strategy(result)
    
    return result
