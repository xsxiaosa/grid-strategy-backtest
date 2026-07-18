"""四参数分阶段候选、轻量回测、排名和任务管理单元测试。"""

import tempfile
import unittest
from pathlib import Path

from grid_backtest.config import StrategyConfig
from grid_backtest.engine import GridBacktestEngine
from grid_backtest.optimization import (
    COARSE_VALUE_LIMIT,
    MAX_ESTIMATED_COMBINATIONS,
    OptimizationJob,
    OptimizationManager,
    OptimizationRanges,
    ParameterRange,
    build_coarse_candidates,
    build_coarse_values,
    build_neighborhood_candidates,
    best_sort_key,
    estimate_max_combinations,
    merge_extremes,
    normalize_parameter,
    select_diverse_results,
    select_extremes,
    validate_coarse_value_limit,
)
from grid_backtest.storage import JsonStorage


def optimization_market() -> dict:
    """构造同时能够触发卖出和买入的固定分钟行情。

    Returns:
        可供完整与轻量引擎共同使用的测试行情对象。
    """

    bars = []
    for index, close in enumerate((1.0, 1.03, 1.02, 0.98, 1.0)):
        bars.append({
            "timestamp": f"2026-07-01T09:{30 + index:02d}:00+08:00",
            "open": close,
            "high": close + 0.002,
            "low": close - 0.002,
            "close": close,
            "volume": 1000,
        })
    return {"symbol": "588000", "name": "测试 ETF", "source": "TEST", "bars": bars}


def result(return_percent: float, drawdown: float, trades: int, commission: float, base: float) -> dict:
    """构造包含内部排序字段的最小候选结果。

    Args:
        return_percent: 未舍入最终收益率。
        drawdown: 未舍入最大回撤。
        trades: 成交总数。
        commission: 累计佣金。
        base: 用于区分四个参数的基础数值。

    Returns:
        可直接传给两端排名函数的结果字典。
    """

    return {
        "return_percent_raw": return_percent,
        "max_drawdown_percent_raw": drawdown,
        "trade_count": trades,
        "commission": commission,
        "rise_trigger_percent": base,
        "sell_pullback_percent": base + 1,
        "fall_trigger_percent": base + 2,
        "buy_rebound_percent": base + 3,
    }


class CandidateGenerationTests(unittest.TestCase):
    """验证三轮候选的范围、数量和两位小数去重规则。"""

    def test_parameter_normalization_uses_two_decimal_places(self) -> None:
        """所有优化候选必须舍入为交易软件可以录入的两位小数。"""

        self.assertEqual(0.81, normalize_parameter(0.80533091))
        self.assertEqual(31.99, normalize_parameter(31.99304024))

    def test_ranges_reject_more_than_two_decimal_places(self) -> None:
        """优化范围包含三位及以上小数时必须在任务开始前明确拒绝。"""

        with self.assertRaisesRegex(ValueError, "最多只能保留两位小数"):
            ParameterRange(0.505, 4.0).validate("上涨触发")

    def test_coarse_values_include_bounds_and_current_within_limit(self) -> None:
        """粗采样必须包含上下限和区间内当前值，且不超过指定上限。"""

        values = build_coarse_values(ParameterRange(0.5, 4.0), 2.5)
        self.assertLessEqual(len(values), COARSE_VALUE_LIMIT)
        self.assertEqual((0.5, 4.0), (values[0], values[-1]))
        self.assertIn(2.5, values)
        self.assertEqual(len(values), len(set(values)))

    def test_custom_coarse_value_limit_accepts_up_to_twenty_five(self) -> None:
        """页面自定义粗采样数量必须支持 25 并正确扩大四维组合上限。"""

        self.assertEqual(25, validate_coarse_value_limit(25))
        self.assertEqual(25**4 + 50200, estimate_max_combinations(25))
        values = build_coarse_values(ParameterRange(0.5, 70.0), 10.0, limit=25)
        self.assertLessEqual(len(values), 25)

    def test_custom_coarse_value_limit_rejects_invalid_values(self) -> None:
        """粗采样数量超过 25 或不是整数时必须在任务启动前拒绝。"""

        for value in (1, 26, 15.5, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_coarse_value_limit(value)

    def test_four_dimension_stage_limits_and_range_clipping(self) -> None:
        """四维粗搜和双端邻域必须遵守计划中的组合数量上限与初始范围。"""

        config = StrategyConfig()
        ranges = OptimizationRanges(
            ParameterRange(0.5, 4.0),
            ParameterRange(20.0, 70.0),
            ParameterRange(0.5, 4.0),
            ParameterRange(20.0, 70.0),
        )
        coarse = build_coarse_candidates(ranges, config)
        self.assertLessEqual(len(coarse), COARSE_VALUE_LIMIT**4)
        seeds = [
            {
                "rise_trigger_percent": candidate[0],
                "sell_pullback_percent": candidate[1],
                "fall_trigger_percent": candidate[2],
                "buy_rebound_percent": candidate[3],
            }
            for candidate in coarse[:20]
        ]
        medium = build_neighborhood_candidates(seeds, ranges, 1.05)
        fine = build_neighborhood_candidates(seeds, ranges, 1.01)
        self.assertLessEqual(len(medium), 20 * 5**4)
        self.assertLessEqual(len(fine), 20 * 5**4)
        self.assertLessEqual(len(coarse) + len(medium) + len(fine), MAX_ESTIMATED_COMBINATIONS)
        for candidate in [*medium, *fine]:
            self.assertTrue(0.5 <= candidate[0] <= 4.0)
            self.assertTrue(20.0 <= candidate[1] <= 70.0)
            self.assertTrue(all(value == round(value, 2) for value in candidate))

    def test_diverse_selection_does_not_fill_seeds_with_one_plateau(self) -> None:
        """相邻参数的同收益平台不应挤占全部细化种子名额。"""

        plateau = [result(5.0, 2.0, 4, 1.0, 10 + index * 0.01) for index in range(20)]
        distant = result(4.9, 2.0, 4, 1.0, 30)
        selected = select_diverse_results([*plateau, distant], best_sort_key, limit=3)
        self.assertIn(distant, selected)


class SummaryAndRankingTests(unittest.TestCase):
    """验证轻量结果与正式报告一致，并验证两端确定性排序。"""

    def test_lightweight_summary_matches_full_report(self) -> None:
        """相同配置下轻量摘要的全部排名指标必须与正式回测完全一致。"""

        config = StrategyConfig.from_dict({
            "base_price": 1.0,
            "lower_bound": 0.8,
            "upper_bound": 1.2,
            "initial_capital": 10000,
            "order_amount": 1000,
            "commission_rate": 0.00025,
            "minimum_commission": 0,
        })
        engine = GridBacktestEngine()
        full = engine.run(config, optimization_market())
        summary = engine.run_summary(config, optimization_market())
        self.assertEqual(full["grid"]["final_assets"], summary["final_assets"])
        self.assertEqual(full["grid"]["return_percent"], summary["return_percent"])
        self.assertEqual(full["grid"]["max_drawdown_percent"], summary["max_drawdown_percent"])
        self.assertEqual(full["grid"]["commission"], summary["commission"])
        self.assertEqual(full["grid"]["trade_count"], summary["trade_count"])
        self.assertEqual(full["grid"]["buy_count"], summary["buy_count"])
        self.assertEqual(full["grid"]["sell_count"], summary["sell_count"])
        self.assertEqual(full["comparison"]["excess_return_percent"], summary["excess_return_percent"])

    def test_select_extremes_uses_return_then_tie_breakers(self) -> None:
        """最优和最差排名应先看收益，再按回撤、成交和佣金稳定打破并列。"""

        values = [
            result(5.0, 3.0, 6, 8.0, 10),
            result(5.0, 2.0, 9, 8.0, 20),
            result(-1.0, 4.0, 2, 2.0, 30),
            result(-1.0, 6.0, 8, 9.0, 40),
        ]
        best, worst = select_extremes(values, limit=2)
        self.assertEqual([20, 10], [item["rise_trigger_percent"] for item in best])
        self.assertEqual([40, 30], [item["rise_trigger_percent"] for item in worst])

    def test_select_extremes_returns_all_when_fewer_than_ten(self) -> None:
        """候选不足十组时两端列表都应返回实际存在的全部结果。"""

        best, worst = select_extremes([result(1.0, 1.0, 1, 1.0, 1)], limit=10)
        self.assertEqual(1, len(best))
        self.assertEqual(1, len(worst))

    def test_incremental_extremes_match_full_ranking(self) -> None:
        """分批增量维护的两端排名必须与累计结果全量排序完全一致。"""

        values = [
            result(float(index % 7), float((index * 3) % 11), index % 9, float(index % 5), index)
            for index in range(37)
        ]
        incremental_best: list[dict] = []
        incremental_worst: list[dict] = []
        for batch_start in range(0, len(values), 4):
            incremental_best, incremental_worst = merge_extremes(
                incremental_best,
                incremental_worst,
                values[batch_start:batch_start + 4],
            )

        expected_best, expected_worst = select_extremes(values)
        self.assertEqual(expected_best, incremental_best)
        self.assertEqual(expected_worst, incremental_worst)


class OptimizationManagerTests(unittest.TestCase):
    """验证单任务并发保护、取消信号和已完成 JSON 回读。"""

    def test_rejects_second_active_job_and_sets_cancel_event(self) -> None:
        """存在运行任务时必须拒绝新任务，并允许按编号设置取消信号。"""

        with tempfile.TemporaryDirectory() as directory:
            storage = JsonStorage(Path(directory))
            manager = OptimizationManager(storage)
            ranges = OptimizationRanges.from_dict({
                name: {"minimum": 1, "maximum": 1}
                for name in (
                    "rise_trigger_percent",
                    "sell_pullback_percent",
                    "fall_trigger_percent",
                    "buy_rebound_percent",
                )
            })
            job = OptimizationJob("opt-running", StrategyConfig(), ranges, "2026-07-18T00:00:00+00:00", status="running")
            manager.jobs[job.job_id] = job
            manager.active_job_id = job.job_id
            with self.assertRaisesRegex(RuntimeError, "已有参数优化任务正在运行"):
                manager.start({"config": {}, "ranges": ranges.to_dict()})
            snapshot = manager.cancel(job.job_id)
            self.assertIsNotNone(snapshot)
            self.assertTrue(job.cancel_event.is_set())

    def test_reads_completed_optimization_from_json(self) -> None:
        """内存中没有任务时管理器应从 optimizations 目录读取最终结果。"""

        with tempfile.TemporaryDirectory() as directory:
            storage = JsonStorage(Path(directory))
            storage.write_optimization("opt-saved", {"job_id": "opt-saved", "status": "completed"})
            snapshot = OptimizationManager(storage).get("opt-saved")
            self.assertEqual("completed", snapshot["status"])

    def test_reuses_compatible_historical_best_when_range_expands(self) -> None:
        """固定条件相同且仍在新范围内的旧最优参数必须进入新任务候选。"""

        with tempfile.TemporaryDirectory() as directory:
            storage = JsonStorage(Path(directory))
            config = StrategyConfig()
            historical_best = {
                "rise_trigger_percent": 0.5,
                "sell_pullback_percent": 31.99304024,
                "fall_trigger_percent": 0.78507375,
                "buy_rebound_percent": 69.30693069,
            }
            storage.write_optimization("opt-history", {
                "status": "completed",
                "config": config.to_dict(),
                "best": [historical_best],
                "worst": [],
            })
            ranges = OptimizationRanges.from_dict({
                "rise_trigger_percent": {"minimum": 0.5, "maximum": 8},
                "sell_pullback_percent": {"minimum": 20, "maximum": 70},
                "fall_trigger_percent": {"minimum": 0.5, "maximum": 8},
                "buy_rebound_percent": {"minimum": 20, "maximum": 70},
            })
            job = OptimizationJob("opt-new", config, ranges, "2026-07-18T00:00:00+00:00")
            candidates = OptimizationManager(storage)._compatible_historical_candidates(job)
            expected = tuple(normalize_parameter(historical_best[name]) for name in (
                "rise_trigger_percent",
                "sell_pullback_percent",
                "fall_trigger_percent",
                "buy_rebound_percent",
            ))
            self.assertIn(expected, candidates)


if __name__ == "__main__":
    # 允许直接执行本测试文件，同时保持 unittest discover 兼容。
    unittest.main()
