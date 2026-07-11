## BTC 交易策略

### go 
获取btc 价格、OI、CVD，按分钟存入数据库
运行
```
DB_HOST=dbhost DB_USER=dbuser DB_PASSWORD=password  DATA_SOURCE=binance ./run.sh
```

### api 
前端页面显示提供接口

```
DB_HOST=dbhost DB_USER=dbuser DB_PASSWORD=password ./run.sh
```

### html
前端页面， 支持部署到cloudflare pages


### kalman
基于卡尔曼滤波均线交叉以及波动率（ATR）区间的回测警报监控。
主要功能：
- 使用卡尔曼滤波器计算短线（short\_kalman）与长线（long\_kalman）的平滑均值。
- 支持多个永续合约标的列表轮询监测（主网周期为 1h）。
- 通过阻力支撑箱体算法，自动捕捉趋势反转点（金叉/死叉）以及通道回测阻力/支撑有效信号。
- 集成 Telegram 警报，发现有效闭合信号后即时推送通知并带有防重复推送机制。

运行方式：
```bash
nohup python3 -u fetch_and_calculate.py > alert_runtime.log 2>&1 &
```
