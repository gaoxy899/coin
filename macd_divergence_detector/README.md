# Binance MACD 背离监控

从 Binance USD-M 永续合约公开接口获取 K 线，并在每个周期收线后检查**刚刚收完的那一根 K 线**是否产生 MACD 背离。不会补报或打印旧背离。

## 配置

在项目目录编辑 [`.env`](.env)：

```dotenv
SYMBOLS=SOLUSDT,ETHUSDT
INTERVALS=1h,4h
CLOSE_DELAY_MINUTES=2
KLINE_LIMIT=1000
DISPLAY_TIMEZONE=Asia/Taipei
CHECK_ON_START=false
```

- `SYMBOLS`：多个 USD-M 合约，逗号分隔。
- `INTERVALS`：多个 Binance 周期，逗号分隔，例如 `1h,4h`。
- `CLOSE_DELAY_MINUTES`：收线后等待分钟数。`1h` + `2` 会在每个整点后 `02` 分检查前一根已收线 K 线；`4h` + `2` 则在 `00:02`、`04:02`、`08:02`… 检查。
- `KLINE_LIMIT`：只用于计算 MACD、ATR 和前一个结构峰/低点；不会显示其中的旧信号。
- `CHECK_ON_START=true`：启动后立即检查当前已收线 K 线一次；默认等待下一个收线计划。

### Telegram 信号通知

只有实时检查发现最新已收线 K 线触发背离时，程序才会发送 Telegram；历史回测不会发送。修改 `.env`：

```dotenv
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=你的_BotFather_Token
TELEGRAM_CHAT_ID=-1001500906279
TELEGRAM_MENTION=@apm
```

Bot Token 是密钥：只保留在部署机器的 `.env`，不要写入 Python 代码、README 或提交到 Git。发送失败时程序会记录不含 Token 的错误，并继续检查其余币种和周期。

`.env.example` 是可复制的配置模板。

## 运行监控

```bash
cd /Users/gaoxy/dev/cloudflare/macd_divergence_detector
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 macd_divergence.py
```

Ubuntu 20.04 的默认 Python 通常为 3.8；依赖文件会自动安装 `backports.zoneinfo` 以支持时区功能。使用 Python 3.9+ 时则直接使用标准库，无需额外安装。

程序会常驻运行。每个币种和周期在计划时间运行一次；只有确认时间等于最新已收线 K 线的背离才会打印。没有新背离时会打印简短状态，不会重复打印过去的背离。

手动立即检查一次（同样只看最新已收线 K 线）：

```bash
python3 macd_divergence.py --once
```

临时只检查一个币种和周期：

```bash
python3 macd_divergence.py --once --symbol ETHUSDT --interval 1h
```

## 历史时点验证

`--backtest-offset` 专用于验证历史截图，会隐藏最新 N 根已收线 K 线，输出该历史时点的最近一条背离；它不用于常驻监控。

```bash
python3 macd_divergence.py --symbol ETHUSDT --interval 1h --backtest-offset 33
```

也支持和 Pin Bar 相同的精确历史时间验证。`--as-of` 与 `--backtest-offset` 互斥，时间必须带时区：

```bash
# UTC
python3 macd_divergence.py --symbol DOGEUSDT --interval 1h --as-of 2026-08-12T22:00:00Z

# 等价的东八区时间
python3 macd_divergence.py --symbol DOGEUSDT --interval 1h --as-of 2026-08-13T06:00:00+08:00
```

选中 K 线会保留，其后的 K 线均会排除后再计算 MACD、ATR 和背离，因而不使用未来数据。

## 检测规则

- 两个价格极值至少间隔 20 根 K 线、至多 120 根；
- 价格极值至少相差 0.5% 且 1.25 ATR，MACD 极值幅度也需要通过 ATR 过滤；
- 顶背离要求 MACD 两峰间位于零轴上方，并以死叉确认；底背离对称地使用零轴下方与金叉；
- 支持 MACD 已在零轴下金叉、价格随后才创更低低点的价格主导型底背离；
- 第二个价格极值必须是整个比较结构的最高/最低点，避免跨越更高/更低结构点。

macOS 自带 Python 使用 LibreSSL 时，`urllib3` 会打印一个不影响本程序请求的兼容性警告；脚本已只关闭这一条警告，不会隐藏其他 HTTPS、网络或证书错误。

运行单元测试：

```bash
python3 -m pytest -q
```
