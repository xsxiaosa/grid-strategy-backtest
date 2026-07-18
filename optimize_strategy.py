"""使用固定初始建仓金额和固定交易股数搜索网格策略参数组合。"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from typing import Any, Iterable

from grid_backtest.runtime_paths import get_data_directory


# 默认搜索范围刻意保持在可解释的规模内，完整组合数为 1764 组。
DEFAULT_RISE_TRIGGERS = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0)
DEFAULT_SELL_PULLBACKS = (20.0, 30.0, 40.0, 50.0, 60.0, 70.0)
DEFAULT_FALL_TRIGGERS = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0)
DEFAULT_BUY_REBOUNDS = (20.0, 30.0, 40.0, 50.0, 60.0, 70.0)


@dataclass(frozen=True, slots=True)
class StrategyParameters:
    """表示一次组合测试使用的四个信号百分比参数。"""

    rise_trigger_percent: float
    sell_pullback_percent: float
    fall_trigger_percent: float
    buy_rebound_percent: float


@dataclass(frozen=True, slots=True)
class BacktestConditions:
    """表示所有参数组合共同使用且不会参与搜索的资金和成交条件。"""

    initial_capital: float = 50_000.0
    initial_investment: float = 25_000.0
    trade_shares: int = 2_000
    lot_size: int = 100
    commission_rate: float = 0.00025
    minimum_commission: float = 0.0
    buy_price_offset: float = 0.001
    sell_price_offset: float = 0.001


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """保存单个参数组合的最终资产、收益率和成交统计。"""

    rise_trigger_percent: float
    sell_pullback_percent: float
    fall_trigger_percent: float
    buy_rebound_percent: float
    final_assets: float
    profit: float
    return_percent: float
    excess_return_percent: float
    buy_count: int
    sell_count: int
    skipped_buy_count: int
    skipped_sell_count: int
    commission: float
    final_cash: float
    final_shares: int


def parse_percentage_list(raw: str) -> tuple[float, ...]:
    """解析命令行中的逗号分隔百分比列表。

    Args:
        raw: 例如 ``"0.5,1,1.5"`` 的逗号分隔文本。

    Returns:
        去重并按升序排列的正百分比元组。

    Raises:
        argparse.ArgumentTypeError: 列表为空、包含非数字或数值超出范围时抛出。
    """

    try:
        # 去掉空白项，避免尾随逗号被误认为有效参数。
        values = sorted({float(item.strip()) for item in raw.split(",") if item.strip()})
    except ValueError as error:
        raise argparse.ArgumentTypeError("百分比列表必须只包含数字和逗号") from error
    if not values or any(value <= 0 or value > 100 for value in values):
        raise argparse.ArgumentTypeError("百分比必须大于 0 且不超过 100")
    return tuple(values)


def load_market_bars(market_path: Path, start_date: str, end_date: str | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """读取并筛选指定自然日期范围内的分钟行情。

    Args:
        market_path: 包含 ``bars`` 数组的行情 JSON 文件路径。
        start_date: 回测开始日期，格式为 ``YYYY-MM-DD``。
        end_date: 可选的回测结束日期，格式为 ``YYYY-MM-DD``。

    Returns:
        按时间升序排列的分钟 K 线列表和原始行情元数据。

    Raises:
        ValueError: 日期格式无效或日期范围内没有行情时抛出。
    """

    # 先显式校验日期，避免字符串比较接受不规范的日期文本。
    datetime.strptime(start_date, "%Y-%m-%d")
    if end_date is not None:
        datetime.strptime(end_date, "%Y-%m-%d")
        if end_date < start_date:
            raise ValueError("结束日期不能早于开始日期")

    market = json.loads(market_path.read_text(encoding="utf-8"))
    bars = []
    for bar in market.get("bars") or []:
        # 时间戳采用 ISO 8601，前十位可以稳定取得本地交易日期。
        trade_date = str(bar.get("timestamp", ""))[:10]
        if trade_date >= start_date and (end_date is None or trade_date <= end_date):
            bars.append(bar)
    bars.sort(key=lambda item: str(item["timestamp"]))
    if not bars:
        raise ValueError(f"{start_date} 至 {end_date or '行情末尾'} 没有可用分钟行情")
    return bars, market


def calculate_commission(gross_amount: float, conditions: BacktestConditions) -> float:
    """计算一笔成交在费率和最低佣金约束下的佣金。

    Args:
        gross_amount: 本笔成交的证券总金额。
        conditions: 包含佣金费率和最低佣金的统一回测条件。

    Returns:
        本笔成交应支付的非负佣金。
    """

    return max(gross_amount * conditions.commission_rate, conditions.minimum_commission)


def floor_to_lot(quantity: float, lot_size: int) -> int:
    """将股数向下取整为完整交易单位。

    Args:
        quantity: 按资金和价格计算出的原始股数。
        lot_size: 每个完整交易单位包含的股数。

    Returns:
        不超过原始股数的最大整手股数。
    """

    return int(quantity // lot_size) * lot_size


def simulate_combination(
    bars: list[dict[str, Any]],
    parameters: StrategyParameters,
    conditions: BacktestConditions,
) -> tuple[SimulationResult, dict[str, float | int]]:
    """按现有回落卖出和反弹买入逻辑模拟一个参数组合。

    首根 K 线按开盘价投入不超过指定初始金额，之后所有买卖都严格使用
    ``trade_shares`` 指定的固定股数。信号计算只使用分钟收盘价，成交价偏移
    仍会被限制在当前分钟 K 线的最高价和最低价范围内。

    Args:
        bars: 已按时间升序筛选的分钟 K 线列表。
        parameters: 本次测试使用的四个信号比例。
        conditions: 所有组合共享的资金、股数、佣金和价格偏移条件。

    Returns:
        单组合模拟结果，以及用于核对初始建仓和持有基准的公共统计。

    Raises:
        ValueError: 行情为空、资金条件无效或初始资金不足以完成建仓时抛出。
    """

    if not bars:
        raise ValueError("至少需要一根分钟 K 线")
    if conditions.initial_capital <= 0 or conditions.initial_investment <= 0:
        raise ValueError("初始资金和初始建仓金额必须大于 0")
    if conditions.trade_shares <= 0 or conditions.trade_shares % conditions.lot_size != 0:
        raise ValueError("固定交易股数必须是正数且能够整除每手股数")

    first_open = float(bars[0]["open"])
    last_close = float(bars[-1]["close"])
    # 初始建仓不超过 25000 元，并按 ETF 常用的 100 股整手向下取整。
    initial_shares = floor_to_lot(conditions.initial_investment / first_open, conditions.lot_size)
    initial_gross = initial_shares * first_open
    initial_commission = calculate_commission(initial_gross, conditions)
    if initial_shares <= 0 or initial_gross + initial_commission > conditions.initial_capital:
        raise ValueError("初始资金不足以完成指定建仓")

    cash = conditions.initial_capital - initial_gross - initial_commission
    shares = initial_shares
    reference_price = first_open
    armed_rise_peak: float | None = None
    armed_fall_trough: float | None = None
    commission = initial_commission
    buy_count = 0
    sell_count = 0
    skipped_buy_count = 0
    skipped_sell_count = 0

    for bar in bars:
        close = float(bar["close"])
        rise_trigger = reference_price * (1 + parameters.rise_trigger_percent / 100)
        fall_trigger = reference_price * (1 - parameters.fall_trigger_percent / 100)

        # 达到上涨触发线后持续追踪最高分钟收盘价。
        if close >= rise_trigger:
            armed_rise_peak = max(armed_rise_peak if armed_rise_peak is not None else close, close)
        if armed_rise_peak is not None:
            armed_rise_peak = max(armed_rise_peak, close)
            sell_confirmation = armed_rise_peak - (armed_rise_peak - reference_price) * parameters.sell_pullback_percent / 100
            if close <= sell_confirmation:
                sell_price = max(float(bar["low"]), close - conditions.sell_price_offset)
                gross_amount = conditions.trade_shares * sell_price
                trade_commission = calculate_commission(gross_amount, conditions)
                if shares >= conditions.trade_shares:
                    # 卖出成功后以实际成交价作为下一轮网格基准，并清空两侧信号。
                    cash += gross_amount - trade_commission
                    shares -= conditions.trade_shares
                    commission += trade_commission
                    reference_price = sell_price
                    armed_rise_peak = None
                    armed_fall_trough = None
                    sell_count += 1
                else:
                    # 持仓不足时取消本轮卖出信号，保持与当前引擎行为一致。
                    armed_rise_peak = None
                    skipped_sell_count += 1
                continue

        # 达到下跌触发线后持续追踪最低分钟收盘价。
        if close <= fall_trigger:
            armed_fall_trough = min(armed_fall_trough if armed_fall_trough is not None else close, close)
        if armed_fall_trough is not None:
            armed_fall_trough = min(armed_fall_trough, close)
            buy_confirmation = armed_fall_trough + (reference_price - armed_fall_trough) * parameters.buy_rebound_percent / 100
            if close >= buy_confirmation:
                buy_price = min(float(bar["high"]), close + conditions.buy_price_offset)
                gross_amount = conditions.trade_shares * buy_price
                trade_commission = calculate_commission(gross_amount, conditions)
                if gross_amount + trade_commission <= cash:
                    # 买入成功后以实际成交价作为下一轮网格基准，并清空两侧信号。
                    cash -= gross_amount + trade_commission
                    shares += conditions.trade_shares
                    commission += trade_commission
                    reference_price = buy_price
                    armed_rise_peak = None
                    armed_fall_trough = None
                    buy_count += 1
                else:
                    # 现金不足时取消本轮买入信号，避免同一谷值被无限重复尝试。
                    armed_fall_trough = None
                    skipped_buy_count += 1

    final_assets = cash + shares * last_close
    profit = final_assets - conditions.initial_capital
    return_percent = profit / conditions.initial_capital * 100
    hold_final_assets = conditions.initial_capital - initial_gross - initial_commission + initial_shares * last_close
    hold_return_percent = (hold_final_assets - conditions.initial_capital) / conditions.initial_capital * 100
    result = SimulationResult(
        rise_trigger_percent=parameters.rise_trigger_percent,
        sell_pullback_percent=parameters.sell_pullback_percent,
        fall_trigger_percent=parameters.fall_trigger_percent,
        buy_rebound_percent=parameters.buy_rebound_percent,
        final_assets=round(final_assets, 2),
        profit=round(profit, 2),
        return_percent=round(return_percent, 4),
        excess_return_percent=round(return_percent - hold_return_percent, 4),
        buy_count=buy_count,
        sell_count=sell_count,
        skipped_buy_count=skipped_buy_count,
        skipped_sell_count=skipped_sell_count,
        commission=round(commission, 2),
        final_cash=round(cash, 2),
        final_shares=shares,
    )
    common = {
        "first_open": round(first_open, 6),
        "last_close": round(last_close, 6),
        "initial_shares": initial_shares,
        "initial_gross": round(initial_gross, 2),
        "initial_commission": round(initial_commission, 2),
        "hold_final_assets": round(hold_final_assets, 2),
        "hold_return_percent": round(hold_return_percent, 4),
    }
    return result, common


def build_parameter_combinations(
    rise_triggers: Iterable[float],
    sell_pullbacks: Iterable[float],
    fall_triggers: Iterable[float],
    buy_rebounds: Iterable[float],
) -> Iterable[StrategyParameters]:
    """生成四个参数维度的笛卡尔积。

    Args:
        rise_triggers: 上涨触发比例集合。
        sell_pullbacks: 回落卖出比例集合。
        fall_triggers: 下跌触发比例集合。
        buy_rebounds: 反弹买入比例集合。

    Returns:
        可逐项迭代的策略参数对象序列。
    """

    # product 保证每个参数组合恰好被测试一次。
    return (
        StrategyParameters(rise, pullback, fall, rebound)
        for rise, pullback, fall, rebound in product(rise_triggers, sell_pullbacks, fall_triggers, buy_rebounds)
    )


def write_results_csv(output_path: Path, results: list[SimulationResult]) -> None:
    """按收益率排名将全部组合结果写入 CSV。

    Args:
        output_path: 需要生成的 CSV 文件路径。
        results: 已按目标排名规则排序的全部模拟结果。

    Returns:
        无返回值；成功时 CSV 文件已完整写入磁盘。
    """

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(asdict(results[0]).keys())
    with output_path.open("w", encoding="utf-8-sig", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(asdict(result) for result in results)


def create_argument_parser() -> argparse.ArgumentParser:
    """创建组合优化脚本的命令行参数解析器。

    Returns:
        包含行情路径、日期、搜索范围和输出设置的解析器。
    """

    data_directory = get_data_directory()
    parser = argparse.ArgumentParser(description="搜索回落卖出和反弹买入的最优参数组合")
    parser.add_argument("--market", type=Path, default=data_directory / "market" / "588000_1m_30d.json", help="分钟行情 JSON 路径")
    parser.add_argument("--start-date", default="2026-07-01", help="回测开始日期，默认 2026-07-01")
    parser.add_argument("--end-date", default=None, help="回测结束日期，默认使用行情最后一天")
    parser.add_argument("--rise-triggers", type=parse_percentage_list, default=DEFAULT_RISE_TRIGGERS, help="上涨触发比例列表")
    parser.add_argument("--sell-pullbacks", type=parse_percentage_list, default=DEFAULT_SELL_PULLBACKS, help="回落卖出比例列表")
    parser.add_argument("--fall-triggers", type=parse_percentage_list, default=DEFAULT_FALL_TRIGGERS, help="下跌触发比例列表")
    parser.add_argument("--buy-rebounds", type=parse_percentage_list, default=DEFAULT_BUY_REBOUNDS, help="反弹买入比例列表")
    parser.add_argument("--top", type=int, default=20, help="终端显示的前 N 个组合，默认 20")
    parser.add_argument("--output", type=Path, default=None, help="CSV 输出路径，默认写入 data/optimization")
    return parser


def main() -> int:
    """运行全部参数组合、保存排名并在终端显示最优结果。

    Returns:
        正常完成时返回进程退出码 0。
    """

    parser = create_argument_parser()
    args = parser.parse_args()
    if args.top <= 0:
        parser.error("--top 必须大于 0")

    bars, market = load_market_bars(args.market, args.start_date, args.end_date)
    conditions = BacktestConditions()
    results: list[SimulationResult] = []
    common: dict[str, float | int] | None = None
    combinations = build_parameter_combinations(args.rise_triggers, args.sell_pullbacks, args.fall_triggers, args.buy_rebounds)
    for parameters in combinations:
        result, common = simulate_combination(bars, parameters, conditions)
        results.append(result)

    # 先按收益率降序，再以成交次数和佣金较少者优先，最后按参数值保证并列排名可复现。
    results.sort(key=lambda item: (
        -item.return_percent,
        item.buy_count + item.sell_count,
        item.commission,
        item.rise_trigger_percent,
        item.sell_pullback_percent,
        item.fall_trigger_percent,
        item.buy_rebound_percent,
    ))
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_path = args.output or get_data_directory() / "optimization" / f"grid_search_{args.start_date.replace('-', '')}_{timestamp}.csv"
    write_results_csv(output_path, results)

    assert common is not None
    print(f"证券：{market.get('symbol', '未知')} {market.get('name', '')}")
    print(f"行情：{bars[0]['timestamp']} 至 {bars[-1]['timestamp']}，共 {len(bars)} 根分钟 K 线")
    print(f"统一条件：初始资金 {conditions.initial_capital:.2f} 元，首日开盘投入不超过 {conditions.initial_investment:.2f} 元，后续每笔 {conditions.trade_shares} 股")
    print(f"初始成交：开盘价 {common['first_open']:.6f}，买入 {common['initial_shares']} 股，金额 {common['initial_gross']:.2f} 元")
    print(f"直接持有：最终资产 {common['hold_final_assets']:.2f} 元，收益率 {common['hold_return_percent']:.4f}%")
    print(f"已测试 {len(results)} 个组合，完整结果：{output_path.resolve()}")
    print("\n排名  上涨触发  回落卖出  下跌触发  反弹买入  收益率     超额收益率  买/卖次数  最终资产")
    for rank, result in enumerate(results[: min(args.top, len(results))], start=1):
        print(
            f"{rank:>4}  {result.rise_trigger_percent:>8.2f}%  {result.sell_pullback_percent:>8.2f}%  "
            f"{result.fall_trigger_percent:>8.2f}%  {result.buy_rebound_percent:>8.2f}%  "
            f"{result.return_percent:>8.4f}%  {result.excess_return_percent:>10.4f}%  "
            f"{result.buy_count:>2}/{result.sell_count:<2}      {result.final_assets:>10.2f}"
        )
    return 0


if __name__ == "__main__":
    # 允许直接使用 python optimize_strategy.py 启动组合测试。
    raise SystemExit(main())
