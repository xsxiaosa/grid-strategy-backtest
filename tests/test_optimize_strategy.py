"""固定建仓金额和固定交易股数组合优化脚本的单元测试。"""

import unittest

from optimize_strategy import BacktestConditions, StrategyParameters, simulate_combination


def market_bars(prices: list[tuple[float, float]]) -> list[dict]:
    """将开盘价和收盘价序列转换为测试用分钟 K 线。

    Args:
        prices: 每个元素依次包含一分钟的开盘价和收盘价。

    Returns:
        包含开高低收字段且按时间升序排列的分钟行情列表。
    """

    bars = []
    for index, (open_price, close_price) in enumerate(prices):
        # 高低价覆盖开盘价、收盘价和千分位成交偏移，避免测试被错误截价干扰。
        bars.append({
            "timestamp": f"2026-07-01T09:{30 + index:02d}:00+08:00",
            "open": open_price,
            "high": max(open_price, close_price) + 0.01,
            "low": min(open_price, close_price) - 0.01,
            "close": close_price,
            "volume": 1000,
        })
    return bars


class OptimizeStrategyTests(unittest.TestCase):
    """验证首日开盘建仓和后续固定 2000 股成交约束。"""

    def test_initial_position_uses_first_open_and_25000_limit(self) -> None:
        """首笔交易应按第一根开盘价买入且成交金额不得超过 25000 元。"""

        conditions = BacktestConditions(commission_rate=0, minimum_commission=0)
        parameters = StrategyParameters(2.0, 50.0, 2.0, 30.0)
        _, common = simulate_combination(market_bars([(2.347, 2.347), (2.347, 2.347)]), parameters, conditions)
        self.assertEqual(10_600, common["initial_shares"])
        self.assertLessEqual(common["initial_gross"], 25_000)
        self.assertEqual(2.347, common["first_open"])

    def test_subsequent_transaction_sells_exactly_2000_shares(self) -> None:
        """一次回落卖出后持仓应精确减少 2000 股。"""

        conditions = BacktestConditions(commission_rate=0, minimum_commission=0)
        parameters = StrategyParameters(2.0, 50.0, 2.0, 30.0)
        bars = market_bars([(1.0, 1.0), (1.0, 1.03), (1.03, 1.01)])
        result, common = simulate_combination(bars, parameters, conditions)
        self.assertEqual((0, 1), (result.buy_count, result.sell_count))
        self.assertEqual(common["initial_shares"] - 2_000, result.final_shares)


if __name__ == "__main__":
    # 允许直接执行本测试文件，同时兼容 unittest discover。
    unittest.main()
