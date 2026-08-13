# Binance Pin Bar 检测

独立于 `macd_divergence_detector` 的 Pin Bar 检测工具，使用 Binance USD-M 永续合约 K 线，默认只检查最新一根已收线 K 线。

```bash
cd /Users/gaoxy/dev/cloudflare/pinbar_detector
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 pinbar_detector.py --symbol ETHUSDT --interval 1h
```

默认规则：

- 看跌 Pin Bar：上影线 ≥ 实体 3 倍；下影线 ≤ 实体 1.5 倍；`High - Low` ≥ 开盘价的 0.8%。
- 看涨 Pin Bar：下影线 ≥ 实体 3 倍；上影线 ≤ 实体 1.5 倍；`High - Low` ≥ 开盘价的 0.8%。
- 实体必须至少占整根 K 线 5%，避免纯十字星及报价精度造成的影线比例失真。
- 振幅还必须 ≥ 前一根可用 `ATR(14)` 的 1 倍；ATR 不使用当前被检测 K 线，避免条件自我影响。
- 看涨 K 线收盘必须位于整根振幅的上方 40%（即从低点起至少 60%），且低点为前 12 根 K 线中的最低点；看跌条件对称（收盘下方 40%、高点为前 12 根最高点）。
- 不使用成交量确认。

历史截图验证可回退最近 K 线：

```bash
python3 pinbar_detector.py --symbol ETHUSDT --interval 1h --backtest-offset 33
```

`--backtest-offset 9` 表示忽略最新 9 根已收线 K 线，检测倒数第 10 根。例如，若最新已收线为 `08-13 15:00 UTC`，它检测的就是 `08-13 06:00 UTC`。

若要避免数据拉取时点变化导致的歧义，推荐精确指定图中 K 线的开盘时间，并在时间中写上时区：

```bash
# 图表为东八区 2026-08-13 06:00
python3 pinbar_detector.py --symbol DOGEUSDT --interval 1h --as-of 2026-08-13T06:00:00+08:00

# 同一根 K 线的 UTC 时间为 2026-08-12 22:00
python3 pinbar_detector.py --symbol DOGEUSDT --interval 1h --as-of 2026-08-12T22:00:00Z
```

运行测试：

```bash
python3 -m pytest -q
```
