import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


# 保护单只能在交易所已确认实际仓位后提交，避免条件单系统尚未同步仓位。
ENTRY_CONFIRM_TIMEOUT_SECONDS = 5.0
ENTRY_CONFIRM_POLL_SECONDS = 0.2


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class TradingConfig:
    enabled: bool
    dry_run: bool
    timeframe: str
    leverage: int
    margin_mode: str
    margin_usdt: float
    max_notional_usdt: float
    dry_run_equity_usdt: float
    tp1_fraction: float
    testnet: bool

    @classmethod
    def from_env(cls) -> "TradingConfig":
        # 是否执行自动交易；false 时仅生成信号和提醒，不发送交易所订单。
        enabled = env_bool("KALMAN_AUTO_TRADE", False)
        # 是否模拟执行；true 时不读取私有账户、不提交真实订单。
        dry_run = env_bool("KALMAN_DRY_RUN", True)
        # 交易周期：仅允许 Binance 支持且能按固定秒数对齐的 K 线周期。
        timeframe = os.getenv("KALMAN_TIMEFRAME", "1h").strip()
        supported_timeframes = {
            "1m", "3m", "5m", "15m", "30m",
            "1h", "2h", "4h", "6h", "8h", "12h",
            "1d", "3d", "1w",
        }
        if timeframe not in supported_timeframes:
            supported = ", ".join(sorted(supported_timeframes))
            raise ValueError(f"KALMAN_TIMEFRAME 不支持 {timeframe!r}，可选值: {supported}")

        # 保证金模式：自动交易固定使用逐仓，避免一个标的影响全账户保证金。
        margin_mode = os.getenv("KALMAN_MARGIN_MODE", "isolated").strip().lower()
        if margin_mode != "isolated":
            raise ValueError("自动交易仅允许 KALMAN_MARGIN_MODE=isolated")

        # 逐仓初始杠杆，实际下单前会再次向交易所校验。
        leverage = int(os.getenv("KALMAN_LEVERAGE", "32"))
        # 单笔固定逐仓保证金，单位 USDT；每次开仓使用相同金额。
        margin_usdt = float(os.getenv("KALMAN_MARGIN_USDT", "3"))
        # 单笔名义仓位的硬上限；兼容旧的 KALMAN_ORDER_NOTIONAL_USDT 配置。
        legacy_cap = os.getenv("KALMAN_ORDER_NOTIONAL_USDT")
        max_notional = float(os.getenv("KALMAN_MAX_NOTIONAL_USDT", legacy_cap or "10000"))
        # 模拟执行的假设账户权益；用于校验固定保证金是否充足并展示保证金占比，实盘会读取实际余额。
        dry_run_equity = float(os.getenv("KALMAN_DRY_RUN_EQUITY_USDT", "1000"))
        # TP1 成交时减仓的比例，例如 0.5 表示平掉 50% 仓位。
        tp1_fraction = float(os.getenv("KALMAN_TP1_FRACTION", "0.5"))
        if not 1 <= leverage <= 125:
            raise ValueError("KALMAN_LEVERAGE must be between 1 and 125")
        if margin_usdt <= 0:
            raise ValueError("KALMAN_MARGIN_USDT must be positive")
        if max_notional <= 0:
            raise ValueError("KALMAN_MAX_NOTIONAL_USDT must be positive")
        if dry_run_equity <= 0:
            raise ValueError("KALMAN_DRY_RUN_EQUITY_USDT must be positive")
        if not 0 <= tp1_fraction < 1:
            raise ValueError("KALMAN_TP1_FRACTION must be in [0, 1)")

        return cls(
            enabled=enabled,
            dry_run=dry_run,
            timeframe=timeframe,
            leverage=leverage,
            margin_mode=margin_mode,
            margin_usdt=margin_usdt,
            max_notional_usdt=max_notional,
            dry_run_equity_usdt=dry_run_equity,
            tp1_fraction=tp1_fraction,
            # 是否连接 Binance Demo Trading（沿用旧 KALMAN_TESTNET 变量名以兼容既有配置）。
            testnet=env_bool("KALMAN_TESTNET", False),
        )


class AutoTrader:
    """Executes one-way Binance futures positions and their protective orders."""

    def __init__(self, exchange: Any, config: TradingConfig, logger: Optional[logging.Logger] = None):
        self.exchange = exchange
        self.config = config
        self.logger = logger or logging.getLogger(__name__)

    @property
    def mode(self) -> str:
        if not self.config.enabled:
            return "signal-only"
        return "dry-run" if self.config.dry_run else "live"

    def validate_account(self) -> None:
        if not self.config.enabled or self.config.dry_run:
            return
        self.exchange.load_markets()
        # 新版 CCXT 提供统一接口；旧版回退至 Binance USDⓈ-M 原生接口。
        fetch_position_mode = getattr(self.exchange, "fetch_position_mode", None)
        if callable(fetch_position_mode):
            position_mode = fetch_position_mode()
            hedged = position_mode.get("hedged")
        else:
            raw_endpoint = getattr(self.exchange, "fapiPrivateGetPositionSideDual", None)
            if not callable(raw_endpoint):
                raw_endpoint = getattr(self.exchange, "fapiPrivateV1GetPositionSideDual", None)
            if not callable(raw_endpoint):
                raise RuntimeError(
                    "当前 CCXT 版本不支持查询 Binance 持仓模式；请升级 CCXT 至较新版本"
                )
            position_mode = raw_endpoint()
            hedged = position_mode.get("dualSidePosition")

        if isinstance(hedged, str):
            hedged = hedged.strip().lower() == "true"
        if bool(hedged):
            raise RuntimeError("自动交易仅支持 Binance 单向持仓模式，请先关闭双向持仓模式")

    def _client_id(self, symbol: str, purpose: str) -> str:
        compact_symbol = symbol.replace("/", "").replace(":", "")[:10]
        timestamp = self.exchange.milliseconds()
        return f"kalman-{compact_symbol}-{purpose}-{timestamp}"[:36]

    def _position_amount(self, symbol: str) -> float:
        market_id = str(self.exchange.market(symbol).get("id") or "")
        positions = self.exchange.fetch_positions([symbol])
        for position in positions:
            info_symbol = str(position.get("info", {}).get("symbol") or "")
            if position.get("symbol") != symbol and info_symbol != market_id:
                continue
            contracts = position.get("contracts")
            if contracts is None:
                contracts = position.get("info", {}).get("positionAmt", 0)
            amount = float(contracts or 0)
            side = position.get("side")
            if side == "short" and amount > 0:
                amount = -amount
            return amount
        return 0.0

    def position_side(self, symbol: str) -> str:
        if not self.config.enabled or self.config.dry_run:
            return "unmanaged"
        amount = self._position_amount(symbol)
        if amount > 0:
            return "long"
        if amount < 0:
            return "short"
        return "flat"

    def _cancel_orders(self, symbol: str, order_ids: List[str]) -> None:
        for order_id in order_ids:
            if not order_id:
                continue
            try:
                self.exchange.cancel_order(str(order_id), symbol, {"trigger": True})
            except Exception as exc:
                self.logger.warning("取消保护单 %s 失败（可能已成交/取消）: %s", order_id, exc)

    def _usdt_budget(self) -> Dict[str, float]:
        if not self.config.enabled or self.config.dry_run:
            equity = self.config.dry_run_equity_usdt
            available = equity
        else:
            balance = self.exchange.fetch_balance({"type": "future"})
            usdt = balance.get("USDT") or {}
            equity = float(usdt.get("total") or (balance.get("total") or {}).get("USDT") or 0)
            available = float(usdt.get("free") or (balance.get("free") or {}).get("USDT") or 0)
            if equity <= 0:
                raise RuntimeError("无法读取正数 USDT 合约账户权益，拒绝开仓")
            if available <= 0:
                raise RuntimeError("USDT 合约账户无可用保证金，拒绝开仓")

        margin_budget = self.config.margin_usdt
        # 余额不足时拒绝下单，保证每次交易都使用固定保证金而非悄悄缩小仓位。
        if available < margin_budget * 1.05:
            raise RuntimeError(
                f"USDT 可用余额不足：固定逐仓保证金需 {margin_budget:.2f} USDT，"
                f"建议可用余额至少 {margin_budget * 1.05:.2f} USDT"
            )
        notional = margin_budget * self.config.leverage
        if notional > self.config.max_notional_usdt:
            raise RuntimeError(
                f"固定保证金对应名义仓位 {notional:.2f} USDT 超过上限 "
                f"{self.config.max_notional_usdt:.2f} USDT"
            )
        return {
            "equity": equity,
            "available": available,
            "margin_budget": margin_budget,
            "notional": notional,
            "equity_fraction": margin_budget / equity,
        }

    def _verify_symbol_config(self, symbol: str) -> None:
        market_id = self.exchange.market(symbol)["id"]
        response = self.exchange.fapiPrivateGetSymbolConfig({"symbol": market_id})
        configs = response if isinstance(response, list) else [response]
        config = next((item for item in configs if item.get("symbol") == market_id), None)
        if config is None:
            raise RuntimeError(f"无法验证 {symbol} 的保证金和杠杆配置")
        if str(config.get("marginType", "")).upper() != "ISOLATED":
            raise RuntimeError(f"{symbol} 未处于逐仓模式，拒绝开仓")
        if int(config.get("leverage") or 0) != self.config.leverage:
            raise RuntimeError(f"{symbol} 杠杆不是 {self.config.leverage} 倍，拒绝开仓")
        if bool(config.get("isAutoAddMargin")):
            raise RuntimeError(f"{symbol} 已开启逐仓自动追加保证金，拒绝开仓")

    def _await_filled_position(
        self,
        symbol: str,
        side: str,
        entry: Dict[str, Any],
    ) -> float:
        """Wait until both the market order and the exchange position are confirmed."""
        order_id = str(entry.get("id") or "")
        if not order_id:
            raise RuntimeError(f"{symbol} 开仓单未返回订单 ID，无法安全创建保护单")

        expected_sign = 1 if side == "long" else -1
        deadline = time.monotonic() + ENTRY_CONFIRM_TIMEOUT_SECONDS
        last_status = str(entry.get("status") or "unknown")
        last_filled = float(entry.get("filled") or 0)
        last_position = 0.0
        last_error = ""
        fetch_order = getattr(self.exchange, "fetch_order", None)

        while True:
            try:
                order = fetch_order(order_id, symbol) if callable(fetch_order) else entry
                last_status = str(order.get("status") or "unknown").lower()
                last_filled = float(order.get("filled") or 0)
                last_position = self._position_amount(symbol)
                order_filled = last_status in {"closed", "filled"} and last_filled > 0
                position_matches = last_position * expected_sign > 0
                if order_filled and position_matches:
                    return abs(last_position)
                if last_status in {"canceled", "cancelled", "rejected", "expired"}:
                    raise RuntimeError(
                        f"{symbol} 开仓单未成交（状态: {last_status}），拒绝创建保护单"
                    )
            except RuntimeError:
                raise
            except Exception as exc:
                last_error = str(exc)

            if time.monotonic() >= deadline:
                detail = (
                    f"订单状态={last_status}, 已成交数量={last_filled}, "
                    f"交易所持仓={last_position}"
                )
                if last_error:
                    detail += f", 最近查询错误={last_error}"
                raise RuntimeError(f"{symbol} 等待开仓成交及仓位确认超时（{detail}）")
            time.sleep(ENTRY_CONFIRM_POLL_SECONDS)

    def _emergency_close_confirmed_position(self, symbol: str, side: str) -> None:
        """Close only the verified position created by this attempted entry."""
        amount = self._position_amount(symbol)
        if amount == 0:
            return
        expected_sign = 1 if side == "long" else -1
        if amount * expected_sign < 0:
            raise RuntimeError(f"{symbol} 仓位方向异常，拒绝紧急平仓非本策略仓位")
        exit_side = "sell" if amount > 0 else "buy"
        self.exchange.create_order(
            symbol,
            "market",
            exit_side,
            abs(amount),
            None,
            {"positionSide": "BOTH", "reduceOnly": True, "clientOrderId": self._client_id(symbol, "emergency")},
        )

    def _cancel_entry_order(self, symbol: str, entry: Dict[str, Any]) -> None:
        """Cancel a still-open entry before flattening any partial fill."""
        order_id = str(entry.get("id") or "")
        if not order_id:
            return
        try:
            self.exchange.cancel_order(order_id, symbol)
        except Exception as exc:
            self.logger.warning("取消开仓单 %s 失败（可能已全部成交）: %s", order_id, exc)

    def open_position(
        self,
        symbol: str,
        side: str,
        reference_price: float,
        sl_price: float,
        tp1_price: float,
        tp2_price: float,
    ) -> Dict[str, Any]:
        if side not in {"long", "short"}:
            raise ValueError("side must be long or short")

        budget = self._usdt_budget()
        preview_amount = budget["notional"] / reference_price
        if not self.config.enabled or self.config.dry_run:
            self.logger.info(
                "[%s] %s %s: 权益=%.2f, 固定逐仓保证金=%.2f (权益 %.2f%%), 名义仓位=%.2f, 预计数量 %.8f, SL=%s, TP1=%s, TP2=%s",
                self.mode,
                symbol,
                side,
                budget["equity"],
                budget["margin_budget"],
                budget["equity_fraction"] * 100,
                budget["notional"],
                preview_amount,
                sl_price,
                tp1_price,
                tp2_price,
            )
            return {"mode": self.mode, "amount": preview_amount, "order_ids": [], **budget}

        self.exchange.load_markets()
        if self._position_amount(symbol) != 0:
            raise RuntimeError(f"{symbol} 交易所账户已有持仓，拒绝重复开仓")

        try:
            self.exchange.set_margin_mode(self.config.margin_mode, symbol)
        except Exception as exc:
            # Binance -4046 means the requested margin mode is already active.
            # Any other failure must stop the entry to avoid accidentally using cross margin.
            if "-4046" not in str(exc):
                raise
            self.logger.info("%s 已是 %s 保证金模式", symbol, self.config.margin_mode)
        self.exchange.set_leverage(self.config.leverage, symbol)
        self._verify_symbol_config(symbol)

        ticker = self.exchange.fetch_ticker(symbol)
        market_price = float(ticker.get("last") or reference_price)
        amount = float(self.exchange.amount_to_precision(symbol, budget["notional"] / market_price))
        market = self.exchange.market(symbol)
        min_amount = float((((market.get("limits") or {}).get("amount") or {}).get("min")) or 0)
        min_cost = float((((market.get("limits") or {}).get("cost") or {}).get("min")) or 0)
        if amount <= 0 or amount < min_amount or amount * market_price < min_cost:
            raise ValueError(
                f"{symbol} 下单金额过小: amount={amount}, min_amount={min_amount}, min_cost={min_cost}"
            )

        entry_side = "buy" if side == "long" else "sell"
        exit_side = "sell" if side == "long" else "buy"
        entry = self.exchange.create_order(
            symbol,
            "market",
            entry_side,
            amount,
            None,
            {"positionSide": "BOTH", "clientOrderId": self._client_id(symbol, "entry")},
        )

        order_ids: List[str] = []
        try:
            # Binance 的条件单系统可能比市价开仓单晚一步同步仓位；先同时确认
            # 开仓成交与实际持仓，避免 closePosition/reduceOnly 保护单被拒绝。
            filled_amount = self._await_filled_position(symbol, side, entry)
            common = {"positionSide": "BOTH", "workingType": "MARK_PRICE", "priceProtect": True}
            stop = self.exchange.create_order(
                symbol,
                "market",
                exit_side,
                None,
                None,
                {
                    **common,
                    "stopLossPrice": sl_price,
                    "closePosition": True,
                    "clientOrderId": self._client_id(symbol, "sl"),
                },
            )
            order_ids.append(str(stop.get("id") or ""))

            tp1_amount = float(
                self.exchange.amount_to_precision(symbol, filled_amount * self.config.tp1_fraction)
            )
            if tp1_amount > 0:
                tp1 = self.exchange.create_order(
                    symbol,
                    "market",
                    exit_side,
                    tp1_amount,
                    None,
                    {
                        **common,
                        "takeProfitPrice": tp1_price,
                        "reduceOnly": True,
                        "clientOrderId": self._client_id(symbol, "tp1"),
                    },
                )
                order_ids.append(str(tp1.get("id") or ""))

            tp2 = self.exchange.create_order(
                symbol,
                "market",
                exit_side,
                None,
                None,
                {
                    **common,
                    "takeProfitPrice": tp2_price,
                    "closePosition": True,
                    "clientOrderId": self._client_id(symbol, "tp2"),
                },
            )
            order_ids.append(str(tp2.get("id") or ""))
        except Exception:
            self.logger.exception("%s 保护单创建失败，执行紧急平仓", symbol)
            self._cancel_entry_order(symbol, entry)
            self._cancel_orders(symbol, order_ids)
            try:
                self._emergency_close_confirmed_position(symbol, side)
            except Exception:
                self.logger.exception("%s 紧急平仓失败，请立即人工检查账户", symbol)
            raise

        return {
            "mode": self.mode,
            "amount": filled_amount,
            "entry_order_id": str(entry.get("id") or ""),
            "order_ids": order_ids,
            "average": entry.get("average"),
            **budget,
        }

    def close_position(self, symbol: str, side: str, order_ids: List[str], reason: str) -> Dict[str, Any]:
        if not self.config.enabled or self.config.dry_run:
            self.logger.info("[%s] %s %s 平仓: %s", self.mode, symbol, side, reason)
            return {"mode": self.mode, "closed": True}

        amount = self._position_amount(symbol)
        self._cancel_orders(symbol, order_ids)
        if amount == 0:
            return {"mode": self.mode, "closed": True, "already_flat": True}

        expected_sign = 1 if side == "long" else -1
        if amount * expected_sign < 0:
            raise RuntimeError(f"{symbol} 交易所持仓方向与本地状态不一致，拒绝自动平仓")
        exit_side = "sell" if amount > 0 else "buy"
        order = self.exchange.create_order(
            symbol,
            "market",
            exit_side,
            abs(amount),
            None,
            {"positionSide": "BOTH", "reduceOnly": True, "clientOrderId": self._client_id(symbol, "exit")},
        )
        return {"mode": self.mode, "closed": True, "order_id": str(order.get("id") or "")}
