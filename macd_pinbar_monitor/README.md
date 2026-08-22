# MACD 背离与 Pin Bar 自动监控

常驻服务会在配置的每个币种、周期收线并等待设定延迟后，获取一次 Binance USD-M K 线，并同时运行 MACD 背离与 Pin Bar 检测。

## 告警模式

在 [`.env`](.env) 用 `ALERT_MODE` 选择：

- `separate`（默认）：MACD 背离、Pin Bar 任一单独出现即分别通知。
- `combined`：仅当 MACD 背离与同方向 Pin Bar 在确认窗口内匹配时通知。
- `both`：发送独立 MACD、独立 Pin Bar，以及组合确认三类通知。

`PINBAR_MAX_BARS_AFTER_DIVERGENCE` 仅适用于 `combined`/`both`：Pin Bar 可出现在背离确认的同一根，或之后最多 N 根已收线 K 线内。

## 配置与运行

编辑 [`.env`](.env)：

```dotenv
SYMBOLS=BTCUSDT,SOLUSDT,ETHUSDT
INTERVALS=1h,4h
CLOSE_DELAY_MINUTES=2
ALERT_MODE=separate
PINBAR_MAX_BARS_AFTER_DIVERGENCE=2
API_RETRY_COUNT=3
API_RETRY_DELAY_SECONDS=2

TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=你的_BotFather_Token
TELEGRAM_CHAT_ID=-1001500906279
TELEGRAM_MENTION=@apm
```

`1h` 加延迟 `2` 会在每小时 `HH:02` 检查前一根已收线 K 线；`4h` 会在 `00:02`、`04:02`、`08:02`… 检查。失败会按 `API_RETRY_COUNT` 立即重试，其他币种/周期不受影响。状态文件记录已通知 K 线，重启或重试不会重复告警。

```bash
cd /Users/gaoxy/dev/cloudflare/macd_pinbar_monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 macd_pinbar_monitor.py
```

立即检查最新已收线 K 线：

```bash
python3 macd_pinbar_monitor.py --once
```

建议通过 systemd（Ubuntu）或其他进程守护工具启动以上常驻命令。Bot Token 只能放在 `.env`，不要提交至 Git。
