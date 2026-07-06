import ccxt
import time
import datetime
import logging
import requests
import pandas as pd
import numpy as np
from kalman_trend import apply_kalman_trend_indicator

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('KalmanAlerts')

# 初始化交易所 (公用实例)
exchange = ccxt.binance({
    'options': {
        'defaultType': 'future', 
    },
    'enableRateLimit': True,
})

# 定义标的轮询列表
SYMBOLS = [
    'BTC/USDT',
    'ETH/USDT',
    'SOL/USDT',
    'XRP/USDT',
    'DOGE/USDT'
]

def sendMsg(msg):
    logger.info(msg)
    dd = {'chat_id': -1001693639294, 'text': msg}
    pp = 'https://api.telegram.org/bot5537601331:AAEGeHCzX6f735vh2nZvictqixlBq7_MPsQ/sendMessage'
    try:
        response = requests.post(pp, data=dd, timeout=10)
        if response.status_code != 200:
            logger.error(f'Telegram 发送失败: {response.text}')
    except Exception as e:
        logger.error(f'Telegram 发送请求异常: {e}')

def fetch_symbol_1h_data(symbol: str) -> pd.DataFrame:
    timeframe = '1h'
    limit = 500  
    
    logger.info(f'正在连接交易所并获取 {symbol} 合约 {timeframe} 数据...')
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    
    cols = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
    df = pd.DataFrame(ohlcv, columns=cols)
    
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('datetime', inplace=True)
    df.drop(columns=['timestamp'], inplace=True)
    
    return df

def run_alert_check(alerted_timestamps):
    for symbol in SYMBOLS:
        try:
            raw_df = fetch_symbol_1h_data(symbol)
            df = apply_kalman_trend_indicator(
                raw_df,
                short_len=50,
                long_len=150,
                retest_sig=True,
                candle_color_enabled=True
            )
            
            # --- 趋势反转信号检查 ---
            idx_crossover = -2
            time_crossover = df.index[idx_crossover]
            
            if df['bullish_transition'].iloc[idx_crossover]:
                alert_id = f'{symbol}_bullish_{time_crossover}'
                if alert_id not in alerted_timestamps:
                    msg = (f'🟢 【{symbol} 1H 趋势偏多】\n'
                           f'时间: {time_crossover}\n'
                           f'收盘价: {df["close"].iloc[idx_crossover]}\n'
                           f'系统已产生金叉🡹转向信号。')
                    sendMsg(msg)
                    alerted_timestamps.add(alert_id)

            elif df['bearish_transition'].iloc[idx_crossover]:
                alert_id = f'{symbol}_bearish_{time_crossover}'
                if alert_id not in alerted_timestamps:
                    msg = (f'🔴 【{symbol} 1H 趋势偏空】\n'
                           f'时间: {time_crossover}\n'
                           f'收盘价: {df["close"].iloc[idx_crossover]}\n'
                           f'系统已产生死叉🢃转向信号。')
                    sendMsg(msg)
                    alerted_timestamps.add(alert_id)

            # --- 阻力与支撑回测点信号检查 ---
            idx_retest = -3
            time_retest = df.index[idx_retest]
            
            if df['retest_x_signal'].iloc[idx_retest]:
                alert_id = f'{symbol}_retest_x_{time_retest}'
                if alert_id not in alerted_timestamps:
                    msg = (f'⚠️ 【{symbol} 1H 阻力测试受阻】\n'
                           f'时间: {time_retest}\n'
                           f'价格在阻力带下方遇阻回落，回测不破 (标记为 x 信号)。')
                    sendMsg(msg)
                    alerted_timestamps.add(alert_id)
                    
            if df['retest_plus_signal'].iloc[idx_retest]:
                alert_id = f'{symbol}_retest_plus_{time_retest}'
                if alert_id not in alerted_timestamps:
                    msg = (f'✅ 【{symbol} 1H 支撑回踩成功】\n'
                           f'时间: {time_retest}\n'
                           f'价格在支撑带上方获得回踩支撑，跌而不破 (标记为 + 信号)。')
                    sendMsg(msg)
                    alerted_timestamps.add(alert_id)

        except Exception as e:
            logger.error(f'处理标的 {symbol} 数据或提取信号时出错: {e}')

def main_loop():
    logger.info('卡尔曼多标的指标监测警报启动！')
    alerted_timestamps = set()
    
    # 启动时先预运行一次检查
    run_alert_check(alerted_timestamps)
    
    while True:
        try:
            # 计算距离下一个整点的分秒延迟（对齐到每小时的 01 分 00 秒）
            now = datetime.datetime.now()
            next_hour = (now + datetime.timedelta(hours=1)).replace(minute=1, second=0, microsecond=0)
            sleep_seconds = (next_hour - now).total_seconds()
            
            logger.info(f'进入每小时轮询等待... 距离下一次唤醒： {round(sleep_seconds/60, 1)} 分钟以后 ({next_hour.strftime("%Y-%m-%d %H:%M:%S")})')
            time.sleep(sleep_seconds)
            
            # 进行判定
            run_alert_check(alerted_timestamps)
            
            # 清理过于陈旧的报警记录，防内存溢出
            if len(alerted_timestamps) > 1000:
                to_remove = list(alerted_timestamps)[:-500]
                for key in to_remove:
                    alerted_timestamps.remove(key)
                    
        except KeyboardInterrupt:
            logger.info('监控服务已手动退出。')
            break
        except Exception as e:
            logger.error(f'监控系统严重异常: {e}')
            time.sleep(60)

if __name__ == '__main__':
    main_loop()
