"""网格策略配置和纯计算引擎单元测试。"""

import unittest

from grid_backtest.config import StrategyConfig
from grid_backtest.engine import GridBacktestEngine


def market_from_closes(closes: list[float]) -> dict:
    """将收盘价序列转换为测试用分钟行情对象。

    Args:
        closes: 按时间顺序排列的分钟收盘价。

    Returns:
        包含窄幅高低价的完整行情 JSON 对象。
    """

    bars = []
    for index, close in enumerate(closes):
        bars.append({
            "timestamp": f"2026-07-01T09:{30 + index:02d}:00+08:00",
            "open": close,
            "high": close + 0.002,
            "low": close - 0.002,
            "close": close,
            "volume": 1000,
        })
    return {"symbol": "588000", "name": "测试 ETF", "source": "TEST", "timezone": "Asia/Shanghai", "bars": bars}


class StrategyConfigTests(unittest.TestCase):
    """验证默认方案、类型转换和业务边界。"""

    def test_default_plan_matches_requested_values(self) -> None:
        """默认配置应与用户给定的 588000 方案一致。"""

        config = StrategyConfig.from_dict(None)
        self.assertEqual("588000", config.symbol)
        self.assertEqual((1.45, 2.05), (config.lower_bound, config.upper_bound))
        self.assertEqual(1.74, config.base_price)
        self.assertEqual((2.5, 20.0, 2.5, 30.0), (config.rise_trigger_percent, config.sell_pullback_percent, config.fall_trigger_percent, config.buy_rebound_percent))
        self.assertEqual(3000.0, config.order_amount)
        self.assertEqual(30, config.days)
        self.assertEqual(20.0, config.minimum_position_percent)

    def test_rejects_base_outside_monitoring_range(self) -> None:
        """基准价位于监控区间之外时应给出明确错误。"""

        with self.assertRaisesRegex(ValueError, "基准价必须位于监控区间内"):
            StrategyConfig.from_dict({"base_price": 2.10})

    def test_rejects_minimum_position_above_initial_position(self) -> None:
        """最低仓位不得高于初始底仓。"""

        with self.assertRaisesRegex(ValueError, "最低仓位比例不得高于初始底仓比例"):
            StrategyConfig.from_dict({"initial_position_percent": 20, "minimum_position_percent": 30})

    def test_legacy_config_with_zero_initial_position_defaults_to_zero_floor(self) -> None:
        """缺少新字段的旧配置仍应允许零初始底仓。"""

        config = StrategyConfig.from_dict({"initial_position_percent": 0})

        self.assertEqual(0.0, config.minimum_position_percent)


class GridBacktestEngineTests(unittest.TestCase):
    """验证网格信号成交和公平基准比较。"""

    def test_sells_after_pullback_and_buys_after_rebound(self) -> None:
        """上涨回落应卖出，随后下跌反弹应买回。"""

        config = StrategyConfig.from_dict({
            "base_price": 1.0,
            "lower_bound": 0.8,
            "upper_bound": 1.2,
            "initial_capital": 10000,
            "order_amount": 1000,
            "commission_rate": 0,
            "minimum_commission": 0,
        })
        result = GridBacktestEngine().run(config, market_from_closes([1.0, 1.03, 1.02, 0.98, 1.0]))
        self.assertEqual(["SELL", "BUY"], [trade["side"] for trade in result["trades"]])
        self.assertEqual(result["initial"]["shares"], result["grid"]["shares"])
        self.assertEqual(result["initial"]["shares"] - result["trades"][0]["quantity"], result["trades"][0]["shares_after"])
        self.assertIn("position_value_after", result["trades"][0])
        self.assertIn("total_assets_after", result["trades"][0])
        self.assertIn("profit_after", result["trades"][0])
        self.assertIn("return_percent_after", result["trades"][0])
        first_trade = result["trades"][0]
        self.assertAlmostEqual(first_trade["cash_after"] + first_trade["position_value_after"], first_trade["total_assets_after"], places=2)
        self.assertAlmostEqual(first_trade["total_assets_after"] - result["initial"]["assets"], first_trade["profit_after"], places=2)
        self.assertEqual(6, len(result["assumptions"]))
        self.assertEqual(5, len(result["chart_data"]))
        self.assertIn("grid_profit", result["chart_data"][-1])
        self.assertIn("hold_profit", result["chart_data"][-1])
        self.assertIn("final_position_percent", result["grid"])
        self.assertIn("average_position_percent", result["grid"])

    def test_minimum_position_caps_sell_quantity(self) -> None:
        """卖出信号只能减少超过最低保留仓位的股数。"""

        config = StrategyConfig.from_dict({
            "base_price": 1.0,
            "lower_bound": 0.8,
            "upper_bound": 1.2,
            "initial_capital": 10000,
            "initial_position_percent": 100,
            "minimum_position_percent": 50,
            "order_amount": 10000,
            "commission_rate": 0,
            "minimum_commission": 0,
        })
        result = GridBacktestEngine().run(config, market_from_closes([1.0, 1.03, 1.02]))

        self.assertEqual(10000, result["initial"]["shares"])
        self.assertEqual(5000, result["initial"]["minimum_shares"])
        self.assertEqual(5000, result["trades"][0]["quantity"])
        self.assertEqual(5000, result["grid"]["shares"])
        self.assertEqual(0, result["grid"]["skipped_for_minimum_position"])

    def test_full_minimum_position_blocks_sell(self) -> None:
        """最低仓位等于初始仓位时应阻止卖出并记录跳过原因。"""

        config = StrategyConfig.from_dict({
            "base_price": 1.0,
            "lower_bound": 0.8,
            "upper_bound": 1.2,
            "initial_capital": 10000,
            "initial_position_percent": 100,
            "minimum_position_percent": 100,
            "order_amount": 1000,
            "commission_rate": 0,
            "minimum_commission": 0,
        })
        result = GridBacktestEngine().run(config, market_from_closes([1.0, 1.03, 1.02]))

        self.assertEqual(0, result["grid"]["trade_count"])
        self.assertEqual(1, result["grid"]["skipped_for_minimum_position"])
        self.assertEqual(result["initial"]["shares"], result["grid"]["shares"])

    def test_no_trade_matches_buy_and_hold(self) -> None:
        """没有网格成交时两种组合的最终资产应完全相同。"""

        config = StrategyConfig.from_dict({"base_price": 1.0, "lower_bound": 0.8, "upper_bound": 1.2, "commission_rate": 0, "minimum_commission": 0})
        result = GridBacktestEngine().run(config, market_from_closes([1.0, 1.001, 1.002]))
        self.assertEqual(0, result["grid"]["trade_count"])
        self.assertEqual(result["grid"]["final_assets"], result["buy_and_hold"]["final_assets"])
        self.assertEqual("TIE", result["comparison"]["winner"])


if __name__ == "__main__":
    # 允许直接执行单个测试文件，同时保持 unittest discover 兼容。
    unittest.main()
