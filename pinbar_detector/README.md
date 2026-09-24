# Binance Pin Bar 工具 + FVG 检测

原有 Pin Bar 检测工具中新增独立的 FVG 形态检测，使用 Binance USD-M 永续合约 K 线，默认只检查最新一根已收线 K 线。默认只输出 Pin Bar；传入 `--detect-fvg` 才额外输出独立的 FVG 结果，两者互不作为过滤条件。

```bash
cd /Users/gaoxy/dev/cloudflare/pinbar_detector
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 pinbar_detector.py --symbol ETHUSDT --interval 1h
```

## 常驻 Pin Bar 监测

`main.py` 是独立的监测入口，不修改 `pinbar_detector.py` 内的检测规则。复制
[`.env.example`](.env.example) 为 `.env`，设置币种、周期及 Telegram 后，默认检查一次最新已收盘 K 线并退出：

```bash
python3 main.py
```

需要常驻、在每次收盘后自动检查时，使用：

```bash
python3 main.py --monitor
```

## Ubuntu 计划任务（cron）

由于 `python3 main.py` 默认只检查一次并退出，适合由 cron 在每次收盘后的延迟分钟启动。`--interval` 会只覆盖本次运行的 `INTERVALS`，而 `--symbol` 可选地只覆盖本次运行的 `SYMBOLS`：

```bash
# 仅检查 .env 中全部币种的 1h 最新已收盘 K 线
python3 /opt/pinbar_detector/main.py --interval 1h

# 仅检查 ETHUSDT 的 4h 最新已收盘 K 线
python3 /opt/pinbar_detector/main.py --symbol ETHUSDT --interval 4h
```

例如 `.env` 设置 `CLOSE_DELAY_MINUTES=1` 时，编辑 `crontab -e`，加入：

```cron
# 每个 15 分钟 K 线收盘后的第 1 分钟：01、16、31、46 分检查 15m
1,16,31,46 * * * * /usr/bin/python3 /opt/pinbar_detector/main.py --interval 15m >> /var/log/pinbar-monitor.log 2>&1

# 每小时的第 1 分钟：检查 1h
1 * * * * /usr/bin/python3 /opt/pinbar_detector/main.py --interval 1h >> /var/log/pinbar-monitor.log 2>&1

# 每 4 小时的第 1 分钟：检查 4h
1 */4 * * * /usr/bin/python3 /opt/pinbar_detector/main.py --interval 4h >> /var/log/pinbar-monitor.log 2>&1
```

将 `/opt/pinbar_detector` 换成实际目录，且让 cron 所用用户具备 `.env` 与状态文件的读写权限。若改变 `CLOSE_DELAY_MINUTES`，相应修改 cron 的分钟数；cron 模式不会自行等待延迟。

`15m`、`1h`、`4h` 都可写入 `INTERVALS` 或通过 `--interval` 指定。默认 `CLOSE_DELAY_MINUTES=1` 时，15m 在 `:01`、`:16`、`:31`、`:46` 检查刚收盘的 K 线；1h 在每个整点后的第 1 分钟检查；4h 在 `00:01`、`04:01`、`08:01`（UTC 对齐，东八区同为 `08:01`、`12:01`、`16:01`）检查。每个 `SYMBOLS × INTERVALS` 组合独立检测，发现 Pin Bar 就单独发送 Telegram；同一根 K 线会由既有状态文件去重。

配置中的主要项目：

- `SYMBOLS=BTCUSDT,ETHUSDT`、`INTERVALS=1h,4h`：可同时监测多个币种和周期。
- `CHECK_ON_START=false`：默认仅等下一次收盘检查，避免程序重启时补发旧信号；设为 `true` 才会在启动时检查最近一根已收线 K 线。
- `API_RETRY_COUNT=3`、`API_RETRY_DELAY_SECONDS=2`：Binance 临时请求失败时重试，单个品种失败不会停止其余监测。
- `PINBAR_*`：可从 `.env` 调整 Pin Bar 筛选条件；未配置时采用当前默认规则。

如本机运行 iPhone 通知转发服务，可额外启用：

```dotenv
IPHONE_ENABLED=true
IPHONE_NOTIFICATION_URL=http://127.0.0.1:8080/gaVimNrvTu6f6NDgsLvDcH
```

它与 Telegram 独立：任一服务失败不会阻止另一服务或下一次 K 线检查；同一信号也会使用 `IPHONE_STATE_FILE` 去重。

部署前可不发消息地测试一次：

```bash
python3 main.py --no-notify
```

也可指定其他配置文件：`python3 main.py --env /path/to/pinbar.env`。

## Telegram 单独 Pin Bar 通知

单独的 Pin Bar 检测也支持 Telegram，不需要 MACD 背离确认。复制 [`.env.example`](.env.example) 为 `.env`，然后填写：

```dotenv
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=你的_BotFather_Token
TELEGRAM_CHAT_ID=-1001500906279
TELEGRAM_MENTION=@apm
```

实时运行时，只要最新已收线 K 线满足 Pin Bar 条件，就会发消息。同一币种、周期、方向、K 线时间仅发一次，已通知记录写到 `.pinbar_telegram_state.json`。`--as-of` 和 `--backtest-offset` 仅用于历史测试，绝不会发送消息；临时禁止实时消息可加 `--no-notify`。

```bash
python3 pinbar_detector.py --symbol BTCUSDT --interval 4h
```

Bot Token 是密钥，只能放在 `.env`，不要写入代码或提交到 Git。

默认规则：

- 看跌 Pin Bar：上影线 ≥ 实体 3 倍；下影线 ≤ 实体 1.5 倍；`High - Low` ≥ 开盘价的 0.8%。
- 看涨 Pin Bar：下影线 ≥ 实体 3 倍；上影线 ≤ 实体 1.5 倍；`High - Low` ≥ 开盘价的 0.8%。
- 实体必须至少占整根 K 线 5%，避免纯十字星及报价精度造成的影线比例失真。
- 振幅还必须 ≥ 前一根可用 `ATR(14)` 的 1 倍；ATR 不使用当前被检测 K 线，避免条件自我影响。
- 看涨 K 线收盘必须位于整根振幅的上方 40%（即从低点起至少 60%），且低点为前 12 根 K 线中的最低点；看跌条件对称（收盘下方 40%、高点为前 12 根最高点）。
- 不使用成交量确认。

FVG 确认规则（与 Pin Bar 无关）：

1. FVG 使用三根 K 线的实体（`Open/Close`）计算，影线可以重叠：第三根实体下沿高于第一根实体上沿为看涨 FVG；第三根实体上沿低于第一根实体下沿为看跌 FVG。
2. 看涨 FVG 开始前的 5 根 K 线中，最低 `Low` 必须等于该 FVG 开始前回看 96 根 K 线的最低 `Low`；看跌 FVG 对称，5 根 K 线中的最高 `High` 必须等于回看 96 根 K 线最高 `High`。极值判断包含影线。
3. FVG 第 1、3 根实体都必须至少为前一 ATR(14) 的 0.10 倍；第 2 根（中间位移 K 线）必须至少为前一 ATR(14) 的 0.25 倍。可用 `--min-fvg-outer-body-atr` 与 `--min-fvg-middle-body-atr` 分别调整。
4. FVG 的上下宽度均须至少为价格的 0.15%，且至少为前一 ATR(14) 的 0.20 倍，过滤过窄区间。

可按品种和周期调节宽度与确认窗口：

```bash
python3 pinbar_detector.py --symbol ETHUSDT --interval 1h \
  --detect-fvg \
  --min-fvg-width-pct 0.002 --min-fvg-width-atr 0.25 \
  --min-fvg-outer-body-atr 0.10 --min-fvg-middle-body-atr 0.30
```

同时检测 Pin Bar 和 FVG：

```bash
python3 pinbar_detector.py --symbol ETHUSDT --interval 1h --detect-fvg
```

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
