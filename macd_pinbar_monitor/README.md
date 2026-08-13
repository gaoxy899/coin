# MACD 背离 + Pin Bar 组合监控

独立组合监控器，复用相邻目录中的 `macd_divergence_detector` 与 `pinbar_detector` 检测规则，不修改它们。

如果三个文件放到同一目录：
把这段删掉：
```
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "macd_divergence_detector"))
sys.path.insert(0, str(PROJECT_ROOT / "pinbar_detector"))
```
只有满足以下条件才通知：

- 顶背离 + `bearish_pinbar`；或
- 底背离 + `bullish_pinbar`；
- Pin Bar 出现在 MACD 背离确认的同一根 K 线，或之后最多 `PINBAR_MAX_BARS_AFTER_DIVERGENCE` 根已收线 K 线内。

它只处理每次检查时**最新的已收线 K 线**。过往 MACD 背离不会在启动时补报。检测到新的 MACD 背离后才会将其保存为待确认状态；状态写入 `STATE_FILE`，重启后仍能在剩余确认窗口内等待 Pin Bar，且已发送信号不会重复发送。

## 配置与运行

编辑 [`.env`](.env)：

```dotenv
SYMBOLS=SOLUSDT,ETHUSDT
INTERVALS=1h,4h
CLOSE_DELAY_MINUTES=2
PINBAR_MAX_BARS_AFTER_DIVERGENCE=2
```

`1h` 且延迟 `2` 会在每小时 `HH:02` 检查刚收完的前一根 K 线。`4h` 在 `00:02`、`04:02` 等检查。

```bash
cd /Users/gaoxy/dev/cloudflare/macd_pinbar_monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 macd_pinbar_monitor.py
```

立即检查一次：

```bash
python3 macd_pinbar_monitor.py --once
```

Telegram 使用本目录 `.env` 的 `TELEGRAM_*` 设置；Token 只能填写在部署机器的 `.env`，不要放进代码或提交到 Git。
