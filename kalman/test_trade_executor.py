import unittest
import os
from unittest.mock import patch

import trade_executor
from trade_executor import AutoTrader, TradingConfig


class FakeExchange:
    def __init__(self, fail_protection_at=None, position=0, hedged=False, margin_error=None,
                 equity=1000, available=1000, auto_add_margin=False):
        self.calls = []
        self.fail_protection_at = fail_protection_at
        self.position = position
        self.hedged = hedged
        self.margin_error = margin_error
        self.equity = equity
        self.available = available
        self.auto_add_margin = auto_add_margin
        self.order_number = 0
        self.orders = {}

    def milliseconds(self):
        return 1234567890000 + self.order_number

    def load_markets(self):
        self.calls.append(("load_markets",))

    def market(self, symbol):
        return {
            "id": "BTCUSDT",
            "limits": {"amount": {"min": 0.001}, "cost": {"min": 5}},
        }

    def fetch_positions(self, symbols):
        side = "short" if self.position < 0 else "long"
        return [{
            "symbol": "BTC/USDT",
            "contracts": abs(self.position),
            "side": side,
            "info": {"symbol": "BTCUSDT"},
        }]

    def fetch_position_mode(self):
        return {"hedged": self.hedged}

    def fapiPrivateGetPositionSideDual(self):
        return {"dualSidePosition": self.hedged}

    def set_margin_mode(self, mode, symbol):
        self.calls.append(("margin", mode, symbol))
        if self.margin_error:
            raise RuntimeError(self.margin_error)

    def set_leverage(self, leverage, symbol):
        self.calls.append(("leverage", leverage, symbol))

    def fetch_ticker(self, symbol):
        return {"last": 10000}

    def fetch_balance(self, params):
        return {"USDT": {"total": self.equity, "free": self.available}}

    def fapiPrivateGetSymbolConfig(self, params):
        return [{
            "symbol": params["symbol"],
            "marginType": "ISOLATED",
            "isAutoAddMargin": self.auto_add_margin,
            "leverage": 2,
        }]

    def amount_to_precision(self, symbol, amount):
        return f"{amount:.3f}"

    def create_order(self, symbol, order_type, side, amount, price, params):
        self.order_number += 1
        self.calls.append(("create", symbol, order_type, side, amount, params))
        is_protection = "stopLossPrice" in params or "takeProfitPrice" in params
        if is_protection and self.fail_protection_at == self.order_number:
            raise RuntimeError("protection failed")
        if order_type == "market" and not is_protection and amount:
            if params.get("reduceOnly"):
                self.position = 0
            else:
                self.position += amount if side == "buy" else -amount
        order = {"id": str(self.order_number), "status": "closed", "filled": amount, "average": 10010}
        self.orders[order["id"]] = order
        return order

    def fetch_order(self, order_id, symbol):
        self.calls.append(("fetch_order", order_id, symbol))
        return self.orders[order_id]

    def cancel_order(self, order_id, symbol, params=None):
        self.calls.append(("cancel", order_id, symbol, params))


class UnconfirmedEntryExchange(FakeExchange):
    """Returns a filled order while deliberately exposing no futures position."""

    def create_order(self, symbol, order_type, side, amount, price, params):
        order = super().create_order(symbol, order_type, side, amount, price, params)
        if order_type == "market" and not params.get("reduceOnly") and not (
            "stopLossPrice" in params or "takeProfitPrice" in params
        ):
            self.position = 0
        return order


def live_config():
    return TradingConfig(
        enabled=True,
        dry_run=False,
        timeframe="1h",
        leverage=2,
        margin_mode="isolated",
        margin_usdt=50,
        max_notional_usdt=100,
        dry_run_equity_usdt=1000,
        tp1_fraction=0.5,
        testnet=False,
    )


def fixed_margin_config(max_notional_usdt=10000):
    return TradingConfig(
        enabled=True,
        dry_run=False,
        timeframe="1h",
        leverage=32,
        margin_mode="isolated",
        margin_usdt=30,
        max_notional_usdt=max_notional_usdt,
        dry_run_equity_usdt=1000,
        tp1_fraction=0.5,
        testnet=False,
    )


class AutoTraderTest(unittest.TestCase):
    def test_old_ccxt_uses_binance_position_mode_endpoint(self):
        exchange = FakeExchange(hedged=False)
        exchange.fetch_position_mode = None
        AutoTrader(exchange, live_config()).validate_account()

        exchange.hedged = True
        with self.assertRaisesRegex(RuntimeError, "单向持仓模式"):
            AutoTrader(exchange, live_config()).validate_account()

    def test_fixed_30_usdt_margin_at_32x(self):
        budget = AutoTrader(FakeExchange(equity=1000), fixed_margin_config())._usdt_budget()
        self.assertEqual(budget["margin_budget"], 30)
        self.assertEqual(budget["notional"], 960)
        self.assertAlmostEqual(budget["equity_fraction"], 0.03)

    def test_insufficient_balance_or_low_cap_rejects_fixed_margin(self):
        with self.assertRaisesRegex(RuntimeError, "可用余额不足"):
            AutoTrader(FakeExchange(equity=1000, available=10), fixed_margin_config())._usdt_budget()
        with self.assertRaisesRegex(RuntimeError, "超过上限"):
            AutoTrader(FakeExchange(equity=1000), fixed_margin_config(500))._usdt_budget()

    def test_auto_add_margin_configuration_is_rejected(self):
        exchange = FakeExchange(auto_add_margin=True)
        with self.assertRaisesRegex(RuntimeError, "自动追加保证金"):
            AutoTrader(exchange, live_config()).open_position(
                "BTC/USDT", "long", 10000, 9800, 10200, 10400
            )
        self.assertFalse(any(call[0] == "create" for call in exchange.calls))

    def test_defaults_are_isolated_and_32x(self):
        with patch.dict(os.environ, {}, clear=True):
            config = TradingConfig.from_env()
        self.assertEqual(config.margin_mode, "isolated")
        self.assertEqual(config.leverage, 32)
        self.assertEqual(config.margin_usdt, 3)
        self.assertEqual(config.timeframe, "1h")

    def test_timeframe_can_be_configured_and_is_validated(self):
        with patch.dict(os.environ, {"KALMAN_TIMEFRAME": "15m"}, clear=True):
            self.assertEqual(TradingConfig.from_env().timeframe, "15m")
        with patch.dict(os.environ, {"KALMAN_TIMEFRAME": "1M"}, clear=True):
            with self.assertRaisesRegex(ValueError, "KALMAN_TIMEFRAME"):
                TradingConfig.from_env()

    def test_margin_mode_failure_aborts_before_entry(self):
        exchange = FakeExchange(margin_error="permission denied")
        with self.assertRaisesRegex(RuntimeError, "permission denied"):
            AutoTrader(exchange, live_config()).open_position(
                "BTC/USDT", "long", 10000, 9800, 10200, 10400
            )
        self.assertFalse(any(call[0] == "create" for call in exchange.calls))

    def test_already_isolated_response_is_safe_to_ignore(self):
        exchange = FakeExchange(margin_error='{"code":-4046,"msg":"No need to change margin type."}')
        AutoTrader(exchange, live_config()).open_position(
            "BTC/USDT", "long", 10000, 9800, 10200, 10400
        )
        self.assertTrue(any(call[0] == "create" for call in exchange.calls))

    def test_position_side_uses_exchange_position(self):
        self.assertEqual(AutoTrader(FakeExchange(position=0.02), live_config()).position_side("BTC/USDT"), "long")
        self.assertEqual(AutoTrader(FakeExchange(position=-0.02), live_config()).position_side("BTC/USDT"), "short")
        self.assertEqual(AutoTrader(FakeExchange(position=0), live_config()).position_side("BTC/USDT"), "flat")

    def test_live_mode_rejects_hedged_account(self):
        trader = AutoTrader(FakeExchange(hedged=True), live_config())
        with self.assertRaisesRegex(RuntimeError, "单向持仓模式"):
            trader.validate_account()

    def test_open_long_places_entry_stop_and_two_take_profits(self):
        exchange = FakeExchange()
        result = AutoTrader(exchange, live_config()).open_position(
            "BTC/USDT", "long", 10000, 9800, 10200, 10400
        )

        creates = [call for call in exchange.calls if call[0] == "create"]
        first_protection_call = next(index for index, call in enumerate(exchange.calls) if call[0] == "create" and "stopLossPrice" in call[5])
        self.assertTrue(any(call[0] == "fetch_order" for call in exchange.calls[:first_protection_call]))
        self.assertEqual(len(creates), 4)
        self.assertEqual(creates[0][3:5], ("buy", 0.01))
        self.assertEqual(creates[1][3], "sell")
        self.assertTrue(creates[1][5]["closePosition"])
        self.assertEqual(creates[2][4], 0.005)
        self.assertTrue(creates[2][5]["reduceOnly"])
        self.assertTrue(creates[3][5]["closePosition"])
        self.assertEqual(result["order_ids"], ["2", "3", "4"])

    def test_missing_exchange_position_prevents_protection_orders(self):
        exchange = UnconfirmedEntryExchange()
        with patch.object(trade_executor, "ENTRY_CONFIRM_TIMEOUT_SECONDS", 0):
            with self.assertRaisesRegex(RuntimeError, "仓位确认超时"):
                AutoTrader(exchange, live_config()).open_position(
                    "BTC/USDT", "long", 10000, 9800, 10200, 10400
                )

        creates = [call for call in exchange.calls if call[0] == "create"]
        self.assertEqual(len(creates), 1)

    def test_protection_failure_emergency_closes_entry(self):
        exchange = FakeExchange(fail_protection_at=2)
        with self.assertRaisesRegex(RuntimeError, "protection failed"):
            AutoTrader(exchange, live_config()).open_position(
                "BTC/USDT", "short", 10000, 10200, 9800, 9600
            )

        creates = [call for call in exchange.calls if call[0] == "create"]
        self.assertEqual(creates[-1][3], "buy")
        self.assertTrue(creates[-1][5]["reduceOnly"])

    def test_close_is_reduce_only_and_cancels_only_tracked_orders(self):
        exchange = FakeExchange(position=-0.01)
        result = AutoTrader(exchange, live_config()).close_position(
            "BTC/USDT", "short", ["11", "12"], "test"
        )

        cancels = [call for call in exchange.calls if call[0] == "cancel"]
        self.assertEqual([call[1] for call in cancels], ["11", "12"])
        self.assertTrue(all(call[3] == {"trigger": True} for call in cancels))
        close_order = [call for call in exchange.calls if call[0] == "create"][-1]
        self.assertEqual(close_order[3], "buy")
        self.assertTrue(close_order[5]["reduceOnly"])
        self.assertTrue(result["closed"])


if __name__ == "__main__":
    unittest.main()
