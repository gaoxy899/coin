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
- 支持多个永续合约标的列表轮询监测（默认周期为 1h，可配置）。
- 通过阻力支撑箱体算法，自动捕捉趋势反转点（金叉/死叉）以及通道回测阻力/支撑有效信号。
- 集成 Telegram 警报，发现有效闭合信号后即时推送通知并带有防重复推送机制。
- 可选 Binance USDT 永续自动交易：信号触发后市价开仓，并立即挂出基于标记价格的硬止损、TP1（默认减仓 50%）与 TP2（平掉剩余仓位）。
- 趋势反转或 K 线跌破/升破卡尔曼快线时，自动撤销本策略保护单并市价平仓。

安装依赖：
```bash
python3 -m pip install -r kalman/requirements.txt
```

先使用模拟执行确认信号和下单参数（不会提交私有订单）：
```bash
cd kalman
KALMAN_AUTO_TRADE=true KALMAN_DRY_RUN=true python3 main.py
```

启用实盘自动交易：
```bash
cd kalman
BINANCE_API_KEY=your_key \
BINANCE_API_SECRET=your_secret \
KALMAN_AUTO_TRADE=true \
KALMAN_DRY_RUN=false \
KALMAN_TIMEFRAME=1h \
KALMAN_MARGIN_USDT=3 \
KALMAN_MAX_NOTIONAL_USDT=10000 \
KALMAN_LEVERAGE=32 \
KALMAN_MARGIN_MODE=isolated \
python3 main.py
```

账户须使用单向持仓模式。建议 API Key 只开启合约交易权限、关闭提现并绑定服务器 IP。Binance Futures 的旧 Testnet/Sandbox 已停用；首次接入应使用 Binance Demo Trading（设置 `KALMAN_TESTNET=true`，变量名为兼容旧配置保留），并填写 Demo Trading 专用 API Key。默认 `KALMAN_AUTO_TRADE=false`，未显式开启时仅发送信号。

默认使用逐仓（`isolated`）和 32 倍初始杠杆。单笔保证金由 `KALMAN_MARGIN_USDT` 固定控制，默认 `3 USDT`，因此默认名义仓位是 `3 × 32 = 96 USDT`，不会随账户余额变化。若可用余额不足以覆盖固定保证金及 5% 手续费缓冲，或名义仓位超过 `KALMAN_MAX_NOTIONAL_USDT=10000` 上限，程序会拒绝开仓而不会缩小仓位。系统会验证逐仓、杠杆和“自动追加保证金已关闭”后才允许开仓。

配置说明：`KALMAN_TIMEFRAME` 为交易周期，默认 `1h`，可选 `1m`、`3m`、`5m`、`15m`、`30m`、`1h`、`2h`、`4h`、`6h`、`8h`、`12h`、`1d`、`3d`、`1w`。`KALMAN_TP1_FRACTION`（默认 `0.5`）控制 TP1 减仓比例；模拟模式使用 `KALMAN_DRY_RUN_EQUITY_USDT`（默认 `1000`）作为假设账户权益，用于校验固定保证金是否足够及展示占比，不影响固定开仓金额。旧参数 `KALMAN_ORDER_NOTIONAL_USDT` 仍可作为名义仓位上限使用，但建议改用 `KALMAN_MAX_NOTIONAL_USDT`。

全部配置及中文注释见 [kalman/.env.example](kalman/.env.example)。注意：该文件是配置参考，不会被程序自动加载；请通过系统环境变量、部署平台的 Secret 或手动 `source` 后再启动程序。

| 配置项 | 中文说明 | 默认值 |
| --- | --- | --- |
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | Binance API 凭证；实盘或 Demo Trading 自动交易必填，两者的 Key 不可混用。 | 无 |
| `KALMAN_AUTO_TRADE` | 是否允许自动下单；关闭时仅生成信号。 | `false` |
| `KALMAN_DRY_RUN` | 是否模拟执行；开启时不会读取私有账户或提交订单。 | `true` |
| `KALMAN_TESTNET` | 是否使用 Binance Demo Trading（为兼容保留的旧变量名）。 | `false` |
| `KALMAN_TIMEFRAME` | 交易信号所使用的 K 线周期。程序会在 K 线收盘 60 秒后计算。 | `1h` |
| `KALMAN_MARGIN_USDT` | 单笔固定逐仓保证金，单位 USDT；不随账户余额变化。 | `3` |
| `KALMAN_MAX_NOTIONAL_USDT` | 单笔名义仓位的硬上限，单位 USDT。 | `10000` |
| `KALMAN_LEVERAGE` | 逐仓初始杠杆。 | `32` |
| `KALMAN_MARGIN_MODE` | 保证金模式；自动交易固定为逐仓。 | `isolated` |
| `KALMAN_TP1_FRACTION` | TP1 成交时减仓的仓位比例。 | `0.5` |
| `KALMAN_DRY_RUN_EQUITY_USDT` | 模拟执行的假设账户权益，用于校验固定保证金是否足够及展示占比；不影响固定仓位。 | `1000` |
| `KALMAN_ORDER_NOTIONAL_USDT` | 旧兼容参数，作为名义仓位上限；优先使用新参数。 | 无 |

运行方式：
```bash
cd kalman
nohup python3 -u main.py > alert_runtime.log 2>&1 &
```
