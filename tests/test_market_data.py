"""Yahoo 日线指标计算的确定性单元测试。"""

import tempfile
import unittest
from pathlib import Path

from grid_backtest.market_data import YahooMinuteMarketDataProvider
from grid_backtest.storage import JsonStorage


class MarketIndicatorTests(unittest.TestCase):
    """验证页面行情摘要使用正确的 MA 和 EMA 数学口径。"""

    def test_calculates_simple_moving_average_from_latest_period(self) -> None:
        """简单移动平均应只使用价格数组末尾的指定数量。"""

        values = [float(value) for value in range(1, 61)]
        result = YahooMinuteMarketDataProvider._simple_moving_average(values, 5)
        self.assertEqual(58.0, result)

    def test_calculates_exponential_moving_average_with_recent_weight(self) -> None:
        """EMA 应按标准平滑系数为较新收盘价分配更高权重。"""

        values = [10.0, 10.0, 20.0]
        result = YahooMinuteMarketDataProvider._exponential_moving_average(values, 2)
        self.assertAlmostEqual(16.6666666667, result)

    def test_rejects_moving_average_period_larger_than_history(self) -> None:
        """收盘价数量不足时应明确拒绝计算，而不是伪造 MA60。"""

        with tempfile.TemporaryDirectory() as directory:
            # 创建提供器仅用于确认测试环境不依赖网络或额外包。
            YahooMinuteMarketDataProvider(JsonStorage(Path(directory)))
            with self.assertRaisesRegex(ValueError, "不超过价格数量"):
                YahooMinuteMarketDataProvider._simple_moving_average([1.0, 2.0], 60)


if __name__ == "__main__":
    # 允许直接执行本测试文件，同时保持 unittest discover 兼容。
    unittest.main()
