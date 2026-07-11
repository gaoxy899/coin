import warnings
warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL.*")

# -*- coding: utf-8 -*-
import ccxt
import time
import datetime
import logging
import requests
import sqlite3
import pandas as pd
import numpy as np
from kalman_trend import apply_kalman_trend_indicator

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('KalmanPersistentAlerts')

import os
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, 'kalman_state.db')
MONITORED_SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'DOGE/USDT']

exchange = ccxt.binance({
    'options': {'defaultType': 'future'},
    'enableRateLimit': True,
})

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS position_states (
            symbol TEXT PRIMARY KEY,
            trend TEXT,
            last_cross_time TEXT,
            has_entered_this_phase INTEGER,
            position TEXT,
            entry_price REAL,
            sl_price REAL,
            tp1_price REAL,
            tp2_price REAL,
            tp1_hit INTEGER
        )
    ''')
    conn.commit()
    conn.close()

def get_symbol_state(symbol: str) -> dict:
    default_state = {
        'symbol': symbol,
        'trend': 'none',
        'last_cross_time': '',
        'has_entered_this_phase': 0,
        'position': 'flat',
        'entry_price': 0.0,
        'sl_price': 0.0,
        'tp1_price': 0.0,
        'tp2_price': 0.0,
        'tp1_hit': 0
    }
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM position_states WHERE symbol = ?", (symbol,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            'symbol': row[0],
            'trend': row[1],
            'last_cross_time': row[2],
            'has_entered_this_phase': row[3],
            'position': row[4],
            'entry_price': row[5],
            'sl_price': row[6],
            'tp1_price': row[7],
            'tp2_price': row[8],
            'tp1_hit': row[9]
        }
    return default_state

def save_symbol_state(state: dict):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO position_states (
            symbol, trend, last_cross_time, has_entered_this_phase,
            position, entry_price, sl_price, tp1_price, tp2_price, tp1_hit
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        state['symbol'], state['trend'], state['last_cross_time'], state['has_entered_this_phase'],
        state['position'], state['entry_price'], state['sl_price'], state['tp1_price'], state['tp2_price'], state['tp1_hit']
    ))
    conn.commit()
    conn.close()

def sendMsg(msg):
    import json
    import re
    logger.info(msg)
    
    parts = msg.split('\n', 1)
    if len(parts) == 2:
        raw_title = parts[0].strip()
        body = parts[1].strip()
        match = re.search(r'【(.*?)】', raw_title)
        if match:
            title = match.group(1)
        else:
            title = raw_title
    else:
        title = "系统提示"
        body = msg.strip()

    payload = {
        "title": title,
        "body": body
    }
    url = "http://127.0.0.1:8080/gaVimNrvTu6f6NDgsLvDcH"
    headers = {
        'Content-Type': 'application/json; charset=utf-8'
    }
    
    try:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        response = requests.post(url, headers=headers, data=data, timeout=10)
        if response.status_code != 200:
            logger.error(f"Push service error: {response.text}")
    except Exception as e:
        logger.error(f"Push service connection error: {e}")

def fetch_symbol_1h_data(symbol: str) -> pd.DataFrame:
    ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=500)
    cols = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
    df = pd.DataFrame(ohlcv, columns=cols)
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms').dt.tz_localize('UTC').dt.tz_convert('Asia/Taipei')
    df.set_index('datetime', inplace=True)
    df.drop(columns=['timestamp'], inplace=True)
    return df

def run_alert_check(alerted_timestamps):
    for symbol in MONITORED_SYMBOLS:
        try:
            raw_df = fetch_symbol_1h_data(symbol)
            df = apply_kalman_trend_indicator(
                raw_df,
                short_len=50,
                long_len=150,
                retest_sig=True,
                candle_color_enabled=True
            )
            
            if len(df) < 5:
                continue

            state = get_symbol_state(symbol)

            idx_c = -2
            time_c = df.index[idx_c]
            time_c_str = time_c.strftime('%Y-%m-%d %H:%M:%S')

            is_up = df['trend_up'].iloc[idx_c]
            short_k = df['short_kalman'].iloc[idx_c]
            long_k = df['long_kalman'].iloc[idx_c]
            low_val = df['low'].iloc[idx_c]
            high_val = df['high'].iloc[idx_c]
            open_val = df['open'].iloc[idx_c]
            close_val = df['close'].iloc[idx_c]

            bullish_trans = df['bullish_transition'].iloc[idx_c]
            bearish_trans = df['bearish_transition'].iloc[idx_c]

            if bullish_trans:
                alert_id = f"{symbol}_bullish_{time_c_str}"
                if alert_id not in alerted_timestamps:
                    sendMsg(f"🟢 【{symbol} 1H 多头趋势】\n时间: {time_c_str}\n收盘价: {close_val:.2f}\n系统已产生金叉🡹转向信号。")
                    alerted_timestamps.add(alert_id)
                state['trend'] = 'bullish'
                state['last_cross_time'] = time_c_str
                state['has_entered_this_phase'] = 0
                if state['position'] == 'short':
                    sendMsg(f"🚪 【{symbol} 1H 跨周期平仓】\n时间: {time_c_str}\n价格: {close_val:.2f}\n原因: 趋势改变 (空单被动防守平仓)。")
                    state['position'] = 'flat'

            elif bearish_trans:
                alert_id = f"{symbol}_bearish_{time_c_str}"
                if alert_id not in alerted_timestamps:
                    sendMsg(f"🔴 【{symbol} 1H 空头趋势】\n时间: {time_c_str}\n收盘价: {close_val:.2f}\n系统已产生死叉🢃转向信号。")
                    alerted_timestamps.add(alert_id)
                state['trend'] = 'bearish'
                state['last_cross_time'] = time_c_str
                state['has_entered_this_phase'] = 0
                if state['position'] == 'long':
                    sendMsg(f"🚪 【{symbol} 1H 跨周期平仓】\n时间: {time_c_str}\n价格: {close_val:.2f}\n原因: 趋势改变 (多单被动防守平仓)。")
                    state['position'] = 'flat'

            idx_retest = -3
            time_retest_str = df.index[idx_retest].strftime('%Y-%m-%d %H:%M:%S')
            if df['retest_x_signal'].iloc[idx_retest]:
                alert_id = f"{symbol}_retest_x_{time_retest_str}"
                if alert_id not in alerted_timestamps:
                    sendMsg(f"⚠️ 【{symbol} 1H 阻力测试受阻】\n时间: {time_retest_str}\n价格在压力带之下受阻回落 (标记为 x 信号，表示至少持续24周期)。")
                    alerted_timestamps.add(alert_id)
            if df['retest_plus_signal'].iloc[idx_retest]:
                alert_id = f"{symbol}_retest_plus_{time_retest_str}"
                if alert_id not in alerted_timestamps:
                    sendMsg(f"✅ 【{symbol} 1H 支撑回踩成功】\n时间: {time_retest_str}\n价格在支撑带获得支撑反弹 (标记为 + 信号，表示至少持续24周期)。")
                    alerted_timestamps.add(alert_id)

            if state['position'] == 'long':
                if low_val <= state['sl_price']:
                    sendMsg(f"🚪 【{symbol} 1H 策略多单出场修正】\n时间: {time_c_str}\n价格: {close_val:.2f}\n离场原因: 触及原始设定的硬止损点 ({state['sl_price']:.2f})。")
                    state['position'] = 'flat'
                elif high_val >= state['tp2_price']:
                    sendMsg(f"🏁 【{symbol} 1H 策略多单终极目标达成 (TP2)】\n时间: {time_c_str}\n价格: {state['tp2_price']:.2f}\n进度说明: 盈亏比 1:2 完美达到，本交易单结清出局！")
                    state['position'] = 'flat'
                elif high_val >= state['tp1_price']:
                    if state['tp1_hit'] == 0:
                        sendMsg(f"🎯 【{symbol} 1H 策略多目标TP1抵达】\n时间: {time_c_str}\n目标价格: {state['tp1_price']:.2f}\n进度说明: 1:1 盈亏已达成。建议减半仓并设置盈亏平衡点止损。")
                        state['tp1_hit'] = 1
                    if open_val < short_k and close_val < short_k:
                        sendMsg(f"🚪 【{symbol} 1H 策略多单主动离场】\n时间: {time_c_str}\n价格: {close_val:.2f}\n离场原因: 开收盘全面跌穿卡尔曼快线 (自适应止损)。")
                        state['position'] = 'flat'
                else:
                    if open_val < short_k and close_val < short_k:
                        sendMsg(f"🚪 【{symbol} 1H 策略多单主动离场】\n时间: {time_c_str}\n价格: {close_val:.2f}\n离场原因: 开收盘全面跌穿卡尔曼快线 (自适应止损)。")
                        state['position'] = 'flat'

            elif state['position'] == 'short':
                if high_val >= state['sl_price']:
                    sendMsg(f"🚪 【{symbol} 1H 策略空单出场修正】\n时间: {time_c_str}\n价格: {close_val:.2f}\n离场原因: 触及原始设定的硬止损点 ({state['sl_price']:.2f})。")
                    state['position'] = 'flat'
                elif low_val <= state['tp2_price']:
                    sendMsg(f"🏁 【{symbol} 1H 策略空单终极目标达成 (TP2)】\n时间: {time_c_str}\n价格: {state['tp2_price']:.2f}\n进度说明: 盈亏比 1:2 完美达到，本交易单结清出局！")
                    state['position'] = 'flat'
                elif low_val <= state['tp1_price']:
                    if state['tp1_hit'] == 0:
                        sendMsg(f"🎯 【{symbol} 1H 策略空目标TP1抵达】\n时间: {time_c_str}\n目标价格: {state['tp1_price']:.2f}\n进度说明: 1:1 盈亏已达成。建议减半仓并设置盈亏平衡点止损。")
                        state['tp1_hit'] = 1
                    if open_val > short_k and close_val > short_k:
                        sendMsg(f"🚪 【{symbol} 1H 策略空单主动离场】\n时间: {time_c_str}\n价格: {close_val:.2f}\n离场原因: 开收盘全面上升穿越卡尔曼快线 (自适应止损)。")
                        state['position'] = 'flat'
                else:
                    if open_val > short_k and close_val > short_k:
                        sendMsg(f"🚪 【{symbol} 1H 策略空单主动离场】\n时间: {time_c_str}\n价格: {close_val:.2f}\n离场原因: 开收盘全面上升穿越卡尔曼快线 (自适应止损)。")
                        state['position'] = 'flat'

            if state['position'] == 'flat' and state['last_cross_time'] != '':
                cross_dt = pd.to_datetime(state['last_cross_time']).tz_localize('Asia/Taipei')
                matching_rows = df[df.index >= cross_dt]
                bars_since_cross = len(matching_rows) - 2
                
                if 0 < bars_since_cross <= 30 and state['has_entered_this_phase'] == 0:
                    if state['trend'] == 'bullish':
                        if low_val <= short_k:
                            state['position'] = 'long'
                            state['entry_price'] = short_k
                            state['has_entered_this_phase'] = 1
                            state['tp1_hit'] = 0
                            
                            sl = max(long_k * 0.98, short_k * 0.96)
                            if sl >= short_k * 0.995:
                                sl = short_k * 0.98
                            state['sl_price'] = sl
                            
                            risk = short_k - sl
                            state['tp1_price'] = short_k + risk
                            state['tp2_price'] = short_k + 2 * risk
                            
                            msg = f"🟢 【{symbol} 1H 策略多单进场提醒】\n时间: {time_c_str}\n入场基准点: {state['entry_price']:.2f}\n计划止损 (SL): {state['sl_price']:.2f}\n预计TP1 (1:1): {state['tp1_price']:.2f}\n预计TP2 (1:2): {state['tp2_price']:.2f}"
                            sendMsg(msg)
                            
                    elif state['trend'] == 'bearish':
                        if high_val >= short_k:
                            state['position'] = 'short'
                            state['entry_price'] = short_k
                            state['has_entered_this_phase'] = 1
                            state['tp1_hit'] = 0
                            
                            sl = min(long_k * 1.02, short_k * 1.04)
                            if sl <= short_k * 1.005:
                                sl = short_k * 1.02
                            state['sl_price'] = sl
                            
                            risk = sl - short_k
                            state['tp1_price'] = short_k - risk
                            state['tp2_price'] = short_k - 2 * risk
                            
                            msg = f"📊 🟠 【{symbol} 1H 策略空单进场提醒】\n时间: {time_c_str}\n入场基准点: {state['entry_price']:.2f}\n计划止损 (SL): {state['sl_price']:.2f}\n预计TP1 (1:1): {state['tp1_price']:.2f}\n预计TP2 (1:2): {state['tp2_price']:.2f}"
                            sendMsg(msg)

            save_symbol_state(state)

        except Exception as e:
            logger.error(f"Error processing {symbol}: {e}")

def main_loop():
    logger.info('带状态存储的卡尔曼自适应策略监听服务已启动！')
    init_db()
    alerted_timestamps = set()
    
    run_alert_check(alerted_timestamps)
    
    while True:
        try:
            now = datetime.datetime.now()
            next_hour = (now + datetime.timedelta(hours=1)).replace(minute=1, second=0, microsecond=0)
            sleep_seconds = (next_hour - now).total_seconds()
            
            logger.info(f"程序进入休眠，将在 {next_hour.strftime('%Y-%m-%d %H:%M:%S')} ({round(sleep_seconds/60, 1)} 分钟后) 唤醒以判定新闭合的1H K线。")
            time.sleep(sleep_seconds)
            
            run_alert_check(alerted_timestamps)
            
            if len(alerted_timestamps) > 500:
                to_remove = list(alerted_timestamps)[:-200]
                for key in to_remove:
                    alerted_timestamps.remove(key)
                    
        except KeyboardInterrupt:
            logger.info('监控服务已手动退出。')
            break
        except Exception as e:
            logger.error(f"主监控网络死链异常: {e}")
            time.sleep(60)

if __name__ == '__main__':
    main_loop()
