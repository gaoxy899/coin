# Binance Pin Bar 工具 + IFVG/FVG 检测

原有 Pin Bar 检测工具中新增独立的 IFVG → FVG 形态检测，使用 Binance USD-M 永续合约 K 线，默认只检查最新一根已收线 K 线。默认只输出 Pin Bar；传入 `--detect-ifvg-fvg` 才额外输出独立的 IFVG → FVG 结果，两者互不作为过滤条件。

```bash
cd /Users/gaoxy/dev/cloudflare/pinbar_detector
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 pinbar_detector.py --symbol ETHUSDT --interval 1h
```

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

IFVG → FVG 确认规则（与 Pin Bar 无关）：

1. 看跌：先有看涨 FVG；一根 K 线**收盘跌破其下边界**后，它成为看跌 IFVG（压力）；随后最多 8 根 K 线内形成看跌 FVG，输出 `bearish_ifvg_fvg`。
2. 看涨：先有看跌 FVG；一根 K 线**收盘突破其上边界**后，它成为看涨 IFVG（支撑）；随后最多 8 根 K 线内形成看涨 FVG，输出 `bullish_ifvg_fvg`。
3. FVG 使用三根 K 线的实体（`Open/Close`）计算，影线可以重叠：第三根实体下沿高于第一根实体上沿为看涨 FVG；第三根实体上沿低于第一根实体下沿为看跌 FVG。
4. FVG 的中间位移 K 线与第 3 根确认 K 线，其实体都必须至少为前一 ATR(14) 的 0.25 倍，过滤十字星与弱实体。
5. IFVG 与后续 FVG 的上下宽度均须至少为价格的 0.15%，且至少为前一 ATR(14) 的 0.20 倍，过滤过窄区间。

可按品种和周期调节宽度与确认窗口：

```bash
python3 pinbar_detector.py --symbol ETHUSDT --interval 1h \
  --min-fvg-width-pct 0.002 --min-fvg-width-atr 0.25 --min-fvg-body-atr 0.30 \
  --fvg-max-bars-after-ifvg 8
```

只测试 IFVG → FVG、隐藏 Pin Bar 输出时，加上 `--only-ifvg-fvg`：

```bash
python3 pinbar_detector.py --symbol ETHUSDT --interval 1h --only-ifvg-fvg
```

同时检测 Pin Bar 和 IFVG → FVG：

```bash
python3 pinbar_detector.py --symbol ETHUSDT --interval 1h --detect-ifvg-fvg
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
