import warnings
warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL.*")

# -*- coding: utf-8 -*-
import ccxt
import time
import datetime
import math
import logging
import requests
import sqlite3
import json
import os
import pandas as pd
import numpy as np
from kalman_trend import apply_kalman_trend_indicator, INITIAL_DYNAMIC_STOP_BARS, should_exit_by_dynamic_stop
from trade_executor import AutoTrader, TradingConfig


def load_env_file(path: str) -> list:
    """Load a local .env file before creating the exchange/configuration.

    The file is authoritative for this standalone bot, so it intentionally
    overrides same-named variables inherited from an old shell or service.
    """
    if not os.path.isfile(path):
        return []

    loaded_keys = []
    with open(path, encoding='utf-8') as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:].lstrip()
            key, separator, value = line.partition('=')
            key = key.strip()
            if not separator or not key or not key.replace('_', '').isalnum() or key[0].isdigit():
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            elif ' #' in value:
                value = value.split(' #', 1)[0].rstrip()
            os.environ[key] = value
            loaded_keys.append(key)
    return loaded_keys


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOADED_ENV_KEYS = load_env_file(os.path.join(SCRIPT_DIR, '.env'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('KalmanPersistentAlerts')
if LOADED_ENV_KEYS:
    logger.info('已加载 .env 配置（%d 项），其值会覆盖旧的进程环境变量。', len(LOADED_ENV_KEYS))

DB_PATH = os.path.join(SCRIPT_DIR, 'kalman_state.db')
MONITORED_SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'DOGE/USDT']

exchange = ccxt.binance({
    'apiKey': os.getenv('BINANCE_API_KEY', ''),
    'secret': os.getenv('BINANCE_API_SECRET', ''),
    'options': {'defaultType': 'future', 'adjustForTimeDifference': True},
    'enableRateLimit': True,
})
trading_config = TradingConfig.from_env()
if trading_config.enabled and not trading_config.dry_run:
    if not os.getenv('BINANCE_API_KEY') or not os.getenv('BINANCE_API_SECRET'):
        raise RuntimeError('实盘自动交易需要 BINANCE_API_KEY 和 BINANCE_API_SECRET')
if trading_config.testnet:
    # Binance Futures 已不再支持旧的 Testnet/Sandbox；CCXT 通过 Demo
    # Trading 切换至演示交易端点。必须在任何私有请求（如 load_markets）前调用。
    enable_demo_trading = getattr(exchange, 'enable_demo_trading', None)
    if not callable(enable_demo_trading):
        raise RuntimeError(
            '当前 CCXT 版本不支持 Binance Demo Trading；请升级 CCXT 至 4.5.6 或更高版本。'
        )
    enable_demo_trading(True)
trader = AutoTrader(exchange, trading_config, logger)
TIMEFRAME_LABEL = trading_config.timeframe.upper()
POSITION_CHECK_INTERVAL_SECONDS = 5 * 60

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
            tp1_hit INTEGER,
            entry_time TEXT,
            exchange_entry_order_id TEXT,
            protection_order_ids TEXT,
            margin_budget_usdt REAL,
            position_notional_usdt REAL,
            account_equity_usdt REAL
        )
    ''')
    try:
        cursor.execute("ALTER TABLE position_states ADD COLUMN entry_time TEXT")
    except sqlite3.OperationalError:
        pass
    for column, column_type in (
        ('exchange_entry_order_id', 'TEXT'),
        ('protection_order_ids', 'TEXT'),
        ('margin_budget_usdt', 'REAL'),
        ('position_notional_usdt', 'REAL'),
        ('account_equity_usdt', 'REAL'),
    ):
        try:
            cursor.execute(f"ALTER TABLE position_states ADD COLUMN {column} {column_type}")
        except sqlite3.OperationalError:
            pass
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
        'tp1_hit': 0,
        'entry_time': '',
        'exchange_entry_order_id': '',
        'protection_order_ids': [],
        'margin_budget_usdt': 0.0,
        'position_notional_usdt': 0.0,
        'account_equity_usdt': 0.0
    }
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT symbol, trend, last_cross_time, has_entered_this_phase, position, entry_price, sl_price, tp1_price, tp2_price, tp1_hit, entry_time, exchange_entry_order_id, protection_order_ids, margin_budget_usdt, position_notional_usdt, account_equity_usdt FROM position_states WHERE symbol = ?", (symbol,))
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
            'tp1_hit': row[9],
            'entry_time': row[10] if row[10] is not None else '',
            'exchange_entry_order_id': row[11] if row[11] is not None else '',
            'protection_order_ids': json.loads(row[12]) if row[12] else [],
            'margin_budget_usdt': float(row[13] or 0),
            'position_notional_usdt': float(row[14] or 0),
            'account_equity_usdt': float(row[15] or 0)
        }
    return default_state

def save_symbol_state(state: dict):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO position_states (
            symbol, trend, last_cross_time, has_entered_this_phase,
            position, entry_price, sl_price, tp1_price, tp2_price, tp1_hit, entry_time,
            exchange_entry_order_id, protection_order_ids, margin_budget_usdt,
            position_notional_usdt, account_equity_usdt
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        state['symbol'], state['trend'], state['last_cross_time'], state['has_entered_this_phase'],
        state['position'], state['entry_price'], state['sl_price'], state['tp1_price'], state['tp2_price'], state['tp1_hit'], state.get('entry_time', ''),
        state.get('exchange_entry_order_id', ''), json.dumps(state.get('protection_order_ids', [])),
        state.get('margin_budget_usdt', 0.0), state.get('position_notional_usdt', 0.0),
        state.get('account_equity_usdt', 0.0)
    ))
    conn.commit()
    conn.close()

def fmt_p(val):
    if val is None:
        return "0.00"
    try:
        fval = float(val)
        if fval < 10.0:
            return f"{fval:.4f}"
        else:
            return f"{fval:.2f}"
    except ValueError:
        return str(val)

def calc_exit_details(state, exit_price, current_time_str):
    entry_p = state.get('entry_price', 0.0)
    entry_t_str = state.get('entry_time', '')
    position = state.get('position', 'flat')
    
    pct_change = 0.0
    if entry_p > 0.0:
        if position == 'long':
            pct_change = ((exit_price - entry_p) / entry_p) * 100
        elif position == 'short':
            pct_change = ((entry_p - exit_price) / entry_p) * 100
            
    hold_hours = 0
    if entry_t_str:
        try:
            entry_dt = pd.to_datetime(entry_t_str)
            curr_dt = pd.to_datetime(current_time_str)
            diff = curr_dt - entry_dt
            hold_hours = int(diff.total_seconds() / 3600)
        except Exception:
            pass
            
    return (
        f"\n开仓价格: {fmt_p(entry_p)}"
        f"\n开仓时间: {entry_t_str if entry_t_str else '未知'}"
        f"\n收益变动: {pct_change:+.2f}%"
        f"\n持仓时间: {hold_hours} 小时"
    )

def close_position_state(state: dict, symbol: str, reason: str) -> bool:
    side = state.get('position', 'flat')
    if side not in ('long', 'short'):
        return True
    try:
        trader.close_position(
            symbol,
            side,
            state.get('protection_order_ids', []),
            reason,
        )
    except Exception as exc:
        logger.exception("%s 自动平仓失败", symbol)
        sendMsg(f"⚠️ 【{symbol} 自动平仓失败】\n原因: {reason}\n错误: {exc}\n请立即人工检查交易所持仓和挂单。")
        return False

    state['position'] = 'flat'
    state['exchange_entry_order_id'] = ''
    state['protection_order_ids'] = []
    return True

def open_position_state(state: dict, symbol: str, side: str, entry_price: float,
                        sl_price: float, tp1_price: float, tp2_price: float,
                        entry_time: str) -> bool:
    try:
        execution = trader.open_position(
            symbol, side, entry_price, sl_price, tp1_price, tp2_price
        )
    except Exception as exc:
        logger.exception("%s 自动开仓或保护单创建失败", symbol)
        sendMsg(f"⚠️ 【{symbol} 自动开仓失败】\n方向: {side}\n错误: {exc}\n策略未记录为持仓，请人工检查交易所账户。")
        return False

    state['position'] = side
    state['entry_price'] = float(execution.get('average') or entry_price)
    state['sl_price'] = sl_price
    state['tp1_price'] = tp1_price
    state['tp2_price'] = tp2_price
    state['has_entered_this_phase'] = 1
    state['tp1_hit'] = 0
    state['entry_time'] = entry_time
    state['exchange_entry_order_id'] = execution.get('entry_order_id', '')
    state['protection_order_ids'] = execution.get('order_ids', [])
    state['margin_budget_usdt'] = float(execution.get('margin_budget') or 0)
    state['position_notional_usdt'] = float(execution.get('notional') or 0)
    state['account_equity_usdt'] = float(execution.get('equity') or 0)
    # Persist immediately after the exchange accepts the position and protections.
    save_symbol_state(state)
    return True

def reconcile_position_state(state: dict, symbol: str, alerted_timestamps: set) -> bool:
    exchange_side = trader.position_side(symbol)
    if exchange_side == 'unmanaged':
        return True

    local_side = state.get('position', 'flat')
    if local_side == exchange_side:
        return True
    if local_side in ('long', 'short') and exchange_side == 'flat':
        if not close_position_state(state, symbol, '交易所持仓已关闭，同步本地状态'):
            return False
        save_symbol_state(state)
        alert_id = f"{symbol}_exchange_flat"
        if alert_id not in alerted_timestamps:
            sendMsg(f"ℹ️ 【{symbol} 持仓状态已同步】\n交易所已无持仓，本地状态及本策略保护单已清理。")
            alerted_timestamps.add(alert_id)
        return True

    alert_id = f"{symbol}_position_mismatch_{local_side}_{exchange_side}"
    if alert_id not in alerted_timestamps:
        sendMsg(
            f"⚠️ 【{symbol} 持仓状态不一致】\n"
            f"本地状态: {local_side}\n交易所状态: {exchange_side}\n"
            "系统已跳过该标的，避免误操作非本策略仓位，请人工核对。"
        )
        alerted_timestamps.add(alert_id)
    return False


def entry_bar_index(df: pd.DataFrame, entry_time: str) -> int:
    """Return the entry candle index; older history safely falls back to legacy stop."""
    if not entry_time:
        return -1
    try:
        entry_dt = pd.to_datetime(entry_time)
        if entry_dt.tzinfo is None:
            entry_dt = entry_dt.tz_localize('Asia/Taipei')
        else:
            entry_dt = entry_dt.tz_convert('Asia/Taipei')
        return int(df.index.searchsorted(entry_dt, side='left'))
    except (TypeError, ValueError):
        logger.warning('无法解析开仓时间 %s，动态止损将使用原有快线规则', entry_time)
        return -1


def dynamic_stop_reason(df: pd.DataFrame, state: dict, current_idx: int) -> str:
    """Return a dynamic-stop reason for the current closed candle, if triggered."""
    side = state.get('position')
    entry_idx = entry_bar_index(df, state.get('entry_time', ''))
    is_initial_window = entry_idx >= 0 and 2 <= current_idx - entry_idx <= INITIAL_DYNAMIC_STOP_BARS
    effective_entry_idx = entry_idx if is_initial_window else current_idx - INITIAL_DYNAMIC_STOP_BARS - 1

    if not should_exit_by_dynamic_stop(
        side,
        effective_entry_idx,
        current_idx,
        df['open'].to_numpy(),
        df['close'].to_numpy(),
        df['short_kalman'].to_numpy(),
        df['long_kalman'].to_numpy(),
        df['high'].to_numpy(),
        df['low'].to_numpy(),
    ):
        return ''

    if is_initial_window:
        if side == 'long':
            return '前 36 根 K 线内：连续两根开盘低于收盘、且收盘跌破卡尔曼慢线'
        return '前 36 根 K 线内：连续两根收盘高于开盘'
    if side == 'long':
        return '超过 36 根 K 线：开收盘全面跌穿卡尔曼快线（原有自适应止损）'
    return '超过 36 根 K 线：开收盘全面升破卡尔曼快线（原有自适应止损）'


def run_position_check(alerted_timestamps: set) -> None:
    """Reconcile locally tracked positions with Binance every five minutes.

    This deliberately does not fetch candles or evaluate entry/exit signals.
    It only detects an exchange-side close (for example SL/TP execution) or a
    position-direction mismatch and synchronizes/alerts accordingly.
    """
    for symbol in MONITORED_SYMBOLS:
        try:
            state = get_symbol_state(symbol)
            if state.get('position') not in ('long', 'short'):
                continue
            reconcile_position_state(state, symbol, alerted_timestamps)
        except Exception as exc:
            logger.error('%s 持仓巡检失败: %s', symbol, exc)

def sendTelMsg(msg):
    logger.info(f"Sending Telegram: {msg[:30]}...")
    dd = {"chat_id": -1001693639294, "text": msg}
    pp = 'https://api.telegram.org/bot5537601331:AAEGeHCzX6f735vh2nZvictqixlBq7_MPsQ/sendMessage'
    try:
        response = requests.post(pp, data=dd, timeout=10)
        if response.status_code != 200:
            logger.error(f"Telegram error: {response.text}")
    except Exception as e:
        logger.error(f"Telegram connection error: {e}")

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

    sendTelMsg(msg)

def fetch_symbol_data(symbol: str) -> pd.DataFrame:
    ohlcv = exchange.fetch_ohlcv(symbol, trading_config.timeframe, limit=500)
    cols = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
    df = pd.DataFrame(ohlcv, columns=cols)
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms').dt.tz_localize('UTC').dt.tz_convert('Asia/Taipei')
    df.set_index('datetime', inplace=True)
    df.drop(columns=['timestamp'], inplace=True)
    return df

def run_alert_check(alerted_timestamps):
    for symbol in MONITORED_SYMBOLS:
        try:
            raw_df = fetch_symbol_data(symbol)
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
            if not reconcile_position_state(state, symbol, alerted_timestamps):
                continue

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
                    sendMsg(f"🟢 【{symbol} {TIMEFRAME_LABEL} 多头趋势】\n时间: {time_c_str}\n收盘价: {fmt_p(close_val)}\n系统已产生金叉🡹转向信号。")
                    alerted_timestamps.add(alert_id)
                state['trend'] = 'bullish'
                state['last_cross_time'] = time_c_str
                state['has_entered_this_phase'] = 0
                if state['position'] == 'short':
                    details = calc_exit_details(state, close_val, time_c_str)
                    sendMsg(f"🚪 【{symbol} {TIMEFRAME_LABEL} 跨周期平仓】\n时间: {time_c_str}\n价格: {fmt_p(close_val)}\n原因: 趋势改变 (空单被动防守平仓)。{details}")
                    close_position_state(state, symbol, '趋势反转，空单平仓')

            elif bearish_trans:
                alert_id = f"{symbol}_bearish_{time_c_str}"
                if alert_id not in alerted_timestamps:
                    sendMsg(f"🔴 【{symbol} {TIMEFRAME_LABEL} 空头趋势】\n时间: {time_c_str}\n收盘价: {fmt_p(close_val)}\n系统已产生死叉🢃转向信号。")
                    alerted_timestamps.add(alert_id)
                state['trend'] = 'bearish'
                state['last_cross_time'] = time_c_str
                state['has_entered_this_phase'] = 0
                if state['position'] == 'long':
                    details = calc_exit_details(state, close_val, time_c_str)
                    sendMsg(f"🚪 【{symbol} {TIMEFRAME_LABEL} 跨周期平仓】\n时间: {time_c_str}\n价格: {fmt_p(close_val)}\n原因: 趋势改变 (多单被动防守平仓)。{details}")
                    close_position_state(state, symbol, '趋势反转，多单平仓')

            if state['position'] == 'long':
                dynamic_reason = dynamic_stop_reason(df, state, len(df) - 2)
                if low_val <= state['sl_price']:
                    details = calc_exit_details(state, state['sl_price'], time_c_str)
                    sendMsg(f"🚪 【{symbol} {TIMEFRAME_LABEL} 策略多单出场修正】\n时间: {time_c_str}\n价格: {fmt_p(state['sl_price'])}\n离场原因: 触及原始设定的硬止损点 ({fmt_p(state['sl_price'])})。{details}")
                    close_position_state(state, symbol, '多单触及硬止损')
                elif high_val >= state['tp2_price']:
                    details = calc_exit_details(state, state['tp2_price'], time_c_str)
                    sendMsg(f"🏁 【{symbol} {TIMEFRAME_LABEL} 策略多单终极目标达成 (TP2)】\n时间: {time_c_str}\n价格: {fmt_p(state['tp2_price'])}\n进度说明: 盈亏比 1:2 完美达到，本交易单结清出局！{details}")
                    close_position_state(state, symbol, '多单达到 TP2')
                elif high_val >= state['tp1_price']:
                    if state['tp1_hit'] == 0:
                        details = calc_exit_details(state, state['tp1_price'], time_c_str)
                        sendMsg(f"🎯 【{symbol} {TIMEFRAME_LABEL} 策略多目标TP1抵达】\n时间: {time_c_str}\n目标价格: {fmt_p(state['tp1_price'])}\n进度说明: 1:1 盈亏已达成。建议减半仓并设置盈亏平衡点止损。{details}")
                        state['tp1_hit'] = 1
                    if dynamic_reason:
                        details = calc_exit_details(state, close_val, time_c_str)
                        sendMsg(f"🚪 【{symbol} {TIMEFRAME_LABEL} 策略多单主动离场】\n时间: {time_c_str}\n价格: {fmt_p(close_val)}\n离场原因: {dynamic_reason}。{details}")
                        close_position_state(state, symbol, '多单动态止损')
                else:
                    if dynamic_reason:
                        details = calc_exit_details(state, close_val, time_c_str)
                        sendMsg(f"🚪 【{symbol} {TIMEFRAME_LABEL} 策略多单主动离场】\n时间: {time_c_str}\n价格: {fmt_p(close_val)}\n离场原因: {dynamic_reason}。{details}")
                        close_position_state(state, symbol, '多单动态止损')

            elif state['position'] == 'short':
                dynamic_reason = dynamic_stop_reason(df, state, len(df) - 2)
                if high_val >= state['sl_price']:
                    details = calc_exit_details(state, state['sl_price'], time_c_str)
                    sendMsg(f"🚪 【{symbol} {TIMEFRAME_LABEL} 策略空单出场修正】\n时间: {time_c_str}\n价格: {fmt_p(state['sl_price'])}\n离场原因: 触及原始设定的硬止损点 ({fmt_p(state['sl_price'])})。{details}")
                    close_position_state(state, symbol, '空单触及硬止损')
                elif low_val <= state['tp2_price']:
                    details = calc_exit_details(state, state['tp2_price'], time_c_str)
                    sendMsg(f"🏁 【{symbol} {TIMEFRAME_LABEL} 策略空单终极目标达成 (TP2)】\n时间: {time_c_str}\n价格: {fmt_p(state['tp2_price'])}\n进度说明: 盈亏比 1:2 完美达到，本交易单结清出局！{details}")
                    close_position_state(state, symbol, '空单达到 TP2')
                elif low_val <= state['tp1_price']:
                    if state['tp1_hit'] == 0:
                        details = calc_exit_details(state, state['tp1_price'], time_c_str)
                        sendMsg(f"🎯 【{symbol} {TIMEFRAME_LABEL} 策略空目标TP1抵达】\n时间: {time_c_str}\n目标价格: {fmt_p(state['tp1_price'])}\n进度说明: 1:1 盈亏已达成。建议减半仓并设置盈亏平衡点止损。{details}")
                        state['tp1_hit'] = 1
                    if dynamic_reason:
                        details = calc_exit_details(state, close_val, time_c_str)
                        sendMsg(f"🚪 【{symbol} {TIMEFRAME_LABEL} 策略空单主动离场】\n时间: {time_c_str}\n价格: {fmt_p(close_val)}\n离场原因: {dynamic_reason}。{details}")
                        close_position_state(state, symbol, '空单动态止损')
                else:
                    if dynamic_reason:
                        details = calc_exit_details(state, close_val, time_c_str)
                        sendMsg(f"🚪 【{symbol} {TIMEFRAME_LABEL} 策略空单主动离场】\n时间: {time_c_str}\n价格: {fmt_p(close_val)}\n离场原因: {dynamic_reason}。{details}")
                        close_position_state(state, symbol, '空单动态止损')

            if state['position'] == 'flat' and state['last_cross_time'] != '':
                cross_dt = pd.to_datetime(state['last_cross_time']).tz_localize('Asia/Taipei')
                matching_rows = df[df.index >= cross_dt]
                bars_since_cross = len(matching_rows) - 2
                
                if 0 < bars_since_cross <= 30 and state['has_entered_this_phase'] == 0:
                    if state['trend'] == 'bullish':
                        if low_val <= short_k:
                            sl = max(long_k * 0.98, short_k * 0.96)
                            if sl >= short_k * 0.995:
                                sl = short_k * 0.98
                            risk = short_k - sl
                            tp1 = short_k + risk
                            tp2 = short_k + 2 * risk
                            if open_position_state(state, symbol, 'long', short_k, sl, tp1, tp2, time_c_str):
                                msg = f"🟢 【{symbol} {TIMEFRAME_LABEL} 策略多单进场提醒】\n时间: {time_c_str}\n执行模式: {trader.mode}\n账户权益: {fmt_p(state['account_equity_usdt'])} USDT\n逐仓保证金预算: {fmt_p(state['margin_budget_usdt'])} USDT\n名义仓位: {fmt_p(state['position_notional_usdt'])} USDT\n入场基准点: {fmt_p(state['entry_price'])}\n计划止损 (SL): {fmt_p(state['sl_price'])}\n预计TP1 (1:1): {fmt_p(state['tp1_price'])}\n预计TP2 (1:2): {fmt_p(state['tp2_price'])}"
                                sendMsg(msg)
                            
                    elif state['trend'] == 'bearish':
                        if high_val >= short_k:
                            sl = min(long_k * 1.02, short_k * 1.04)
                            if sl <= short_k * 1.005:
                                sl = short_k * 1.02
                            risk = sl - short_k
                            tp1 = short_k - risk
                            tp2 = short_k - 2 * risk
                            if open_position_state(state, symbol, 'short', short_k, sl, tp1, tp2, time_c_str):
                                msg = f"📊 🟠 【{symbol} {TIMEFRAME_LABEL} 策略空单进场提醒】\n时间: {time_c_str}\n执行模式: {trader.mode}\n账户权益: {fmt_p(state['account_equity_usdt'])} USDT\n逐仓保证金预算: {fmt_p(state['margin_budget_usdt'])} USDT\n入场基准点: {fmt_p(state['entry_price'])}\n计划止损 (SL): {fmt_p(state['sl_price'])}\n预计TP1 (1:1): {fmt_p(state['tp1_price'])}\n预计TP2 (1:2): {fmt_p(state['tp2_price'])}"
                                sendMsg(msg)

            save_symbol_state(state)

        except Exception as e:
            logger.error(f"Error processing {symbol}: {e}")

def main_loop():
    logger.info('带状态存储的卡尔曼自适应策略监听服务已启动！')
    logger.info(
        '交易执行模式: %s, 交易周期: %s, 固定逐仓保证金: %.2f USDT, 杠杆: %sx, 保证金模式: %s, 名义仓位上限: %.2f USDT',
        trader.mode,
        trading_config.timeframe,
        trading_config.margin_usdt,
        trading_config.leverage,
        trading_config.margin_mode,
        trading_config.max_notional_usdt,
    )
    init_db()
    trader.validate_account()
    
    logger.info("=== 当前加载的交易对策略状态 ===")
    for symbol in MONITORED_SYMBOLS:
        state = get_symbol_state(symbol)
        if state['trend'] != 'none' or state['position'] != 'flat':
            logger.info(f"【{symbol}】 趋势: {state['trend']}, 持仓状态: {state['position']}, 止损: {state['sl_price']}, TP1击中?: {state['tp1_hit']}")
        else:
            logger.info(f"【{symbol}】 目前无趋势及持仓。")
    logger.info("=================================")
    
    alerted_timestamps = set()
    
    run_alert_check(alerted_timestamps)
    sendMsg('kalman_trend 策略启动 \n @jp.ora')
    next_position_check = (
        math.floor(time.time() / POSITION_CHECK_INTERVAL_SECONDS) + 1
    ) * POSITION_CHECK_INTERVAL_SECONDS
    
    while True:
        try:
            # 在下一根 K 线收盘后 60 秒执行，确保只使用已闭合的 K 线。
            timeframe_seconds = exchange.parse_timeframe(trading_config.timeframe)
            next_close_timestamp = (math.floor(time.time() / timeframe_seconds) + 1) * timeframe_seconds
            next_signal_run_timestamp = next_close_timestamp + 60
            wake_timestamp = min(next_signal_run_timestamp, next_position_check)
            sleep_seconds = max(0, wake_timestamp - time.time())
            next_run = datetime.datetime.fromtimestamp(wake_timestamp)
            
            logger.info(
                '程序进入休眠，将在 %s（%.1f 分钟后）唤醒；新 K 线信号在 %s 检查，持仓每 5 分钟巡检。',
                next_run.strftime('%Y-%m-%d %H:%M:%S'),
                sleep_seconds / 60,
                datetime.datetime.fromtimestamp(next_signal_run_timestamp).strftime('%Y-%m-%d %H:%M:%S'),
            )
            time.sleep(sleep_seconds)

            now = time.time()
            if now >= next_position_check:
                run_position_check(alerted_timestamps)
                next_position_check = (
                    math.floor(now / POSITION_CHECK_INTERVAL_SECONDS) + 1
                ) * POSITION_CHECK_INTERVAL_SECONDS

            if now >= next_signal_run_timestamp:
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
