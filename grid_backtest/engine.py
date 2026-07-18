"""分钟收盘价驱动的网格策略模拟和直接底仓持有比较。"""

from dataclasses import dataclass, field
from typing import Any

from .config import StrategyConfig


@dataclass(slots=True)
class SimulationState:
    """回测过程中持续更新的账户资产、网格基准和触发极值。"""

    reference_price: float
    cash: float
    shares: int
    initial_assets: float
    minimum_shares: int = 0
    armed_rise_peak: float | None = None
    armed_fall_trough: float | None = None
    trades: list[dict[str, Any]] = field(default_factory=list)
    commissions: float = 0.0
    skipped_for_funds: int = 0
    skipped_for_shares: int = 0
    skipped_for_minimum_position: int = 0
    buy_count: int = 0
    sell_count: int = 0
    record_trade_details: bool = True
    bars_processed: int = 0
    position_percent_sum: float = 0.0
    max_cash_percent: float = 0.0
    minimum_position_bar_count: int = 0
    current_minimum_position_bars: int = 0
    longest_minimum_position_bars: int = 0


class GridBacktestEngine:
    """使用确定性分钟收盘价规则执行网格和基准组合回测。"""

    # 正收益且长期低仓位时，结果更接近择时避险，不应只按收益率解读。
    LOW_EXPOSURE_WARNING_PERCENT = 30.0
    # 基准价与首根行情偏差过大时，首次信号可能不代表真实建仓成本。
    BASE_PRICE_WARNING_PERCENT = 5.0

    def run(self, config: StrategyConfig, market: dict[str, Any], inherited_warnings: list[str] | None = None) -> dict[str, Any]:
        """执行网格回测并生成可直接保存为 JSON 的结构化结果。

        Args:
            config: 已校验的策略参数。
            market: 包含按时间升序分钟 K 线的行情对象。
            inherited_warnings: 行情缓存回退等上游警告。

        Returns:
            包含行情覆盖、初始组合、两种收益、成交和假设的报告。

        Raises:
            ValueError: 有效分钟 K 线不足两根。
        """

        bars = sorted(market.get("bars") or [], key=lambda item: str(item.get("timestamp", "")))
        if len(bars) < 2:
            raise ValueError("可用分钟行情不足，无法进行回测")
        first_price = float(bars[0]["close"])
        last_price = float(bars[-1]["close"])
        target_position_value = config.initial_capital * config.initial_position_percent / 100
        initial_shares = self._floor_to_lot(target_position_value / first_price, config.lot_size)
        minimum_shares = self._calculate_minimum_shares(initial_shares, config)
        initial_cash = config.initial_capital - initial_shares * first_price
        initial_assets = initial_cash + initial_shares * first_price
        state = SimulationState(config.base_price, initial_cash, initial_shares, initial_assets, minimum_shares)
        grid_peak_assets = initial_assets
        hold_peak_assets = initial_assets
        grid_max_drawdown = 0.0
        hold_max_drawdown = 0.0
        chart_data: list[dict[str, Any]] = []

        for bar in bars:
            self._process_bar(config, state, bar)
            close = float(bar["close"])
            grid_assets, position_percent, cash_percent = self._update_exposure_stats(state, close)
            hold_assets = initial_cash + initial_shares * close
            grid_peak_assets = max(grid_peak_assets, grid_assets)
            hold_peak_assets = max(hold_peak_assets, hold_assets)
            grid_max_drawdown = max(grid_max_drawdown, self._percentage_drop(grid_peak_assets, grid_assets))
            hold_max_drawdown = max(hold_max_drawdown, self._percentage_drop(hold_peak_assets, hold_assets))
            chart_data.append({
                "timestamp": bar["timestamp"],
                "open": self._round_price(float(bar["open"])),
                "high": self._round_price(float(bar["high"])),
                "low": self._round_price(float(bar["low"])),
                "close": self._round_price(close),
                "volume": float(bar.get("volume", 0)),
                "grid_profit": self._round_money(grid_assets - initial_assets),
                "hold_profit": self._round_money(hold_assets - initial_assets),
                "position_percent": self._round_percent(position_percent),
                "cash_percent": self._round_percent(cash_percent),
            })

        grid_final_assets = state.cash + state.shares * last_price
        hold_final_assets = initial_cash + initial_shares * last_price
        grid_profit = grid_final_assets - initial_assets
        hold_profit = hold_final_assets - initial_assets
        grid_return = grid_profit / initial_assets * 100
        hold_return = hold_profit / initial_assets * 100
        exposure_metrics = self._build_exposure_metrics(state, grid_final_assets, last_price, grid_return)
        warnings = list(inherited_warnings or [])
        if state.skipped_for_funds:
            warnings.append(f"{state.skipped_for_funds} 次买入信号因现金不足未成交。")
        if state.skipped_for_shares:
            warnings.append(f"{state.skipped_for_shares} 次卖出信号因底仓不足未成交。")
        if state.skipped_for_minimum_position:
            warnings.append(f"{state.skipped_for_minimum_position} 次卖出信号因最低仓位限制未成交。")
        if exposure_metrics["low_exposure_warning"]:
            warnings.append("本次正收益伴随较低平均或期末仓位，收益可能主要来自回避下跌，不能单独视为网格波动收益。")
        base_price_deviation = abs(first_price - config.base_price) / config.base_price * 100
        if base_price_deviation > self.BASE_PRICE_WARNING_PERCENT:
            warnings.append(f"网格基准价与首根行情价格偏差 {base_price_deviation:.2f}%，首次信号可能与实际建仓成本不一致。")
        if not state.trades:
            warnings.append("回测区间内没有产生网格成交，请检查基准价、触发比例与监控区间。")

        return {
            "strategy": config.to_dict(),
            "market_data": {
                "symbol": market.get("symbol", config.symbol),
                "name": market.get("name", config.symbol),
                "source": market.get("source", "未知来源"),
                "timezone": market.get("timezone", "Asia/Shanghai"),
                "fetched_at": market.get("fetched_at"),
                "bar_count": len(bars),
                "started_at": bars[0]["timestamp"],
                "ended_at": bars[-1]["timestamp"],
                "first_price": self._round_price(first_price),
                "last_price": self._round_price(last_price),
            },
            "initial": {
                "assets": self._round_money(initial_assets),
                "cash": self._round_money(initial_cash),
                "shares": initial_shares,
                "position_value": self._round_money(initial_shares * first_price),
                "minimum_shares": minimum_shares,
                "minimum_position_percent": self._round_percent(config.minimum_position_percent),
            },
            "grid": {
                "final_assets": self._round_money(grid_final_assets),
                "cash": self._round_money(state.cash),
                "shares": state.shares,
                "profit": self._round_money(grid_profit),
                "return_percent": self._round_percent(grid_return),
                "commission": self._round_money(state.commissions),
                "trade_count": state.buy_count + state.sell_count,
                "buy_count": state.buy_count,
                "sell_count": state.sell_count,
                "max_drawdown_percent": self._round_percent(grid_max_drawdown),
                **exposure_metrics,
            },
            "buy_and_hold": {
                "final_assets": self._round_money(hold_final_assets),
                "cash": self._round_money(initial_cash),
                "shares": initial_shares,
                "profit": self._round_money(hold_profit),
                "return_percent": self._round_percent(hold_return),
                "max_drawdown_percent": self._round_percent(hold_max_drawdown),
            },
            "comparison": {
                "excess_profit": self._round_money(grid_profit - hold_profit),
                "excess_return_percent": self._round_percent(grid_return - hold_return),
                "winner": "TIE" if abs(grid_profit - hold_profit) < 0.005 else "GRID" if grid_profit > hold_profit else "BUY_AND_HOLD",
            },
            "trades": state.trades,
            "chart_data": chart_data,
            "warnings": warnings,
            "assumptions": [
                "上涨、下跌触发及回落、反弹确认均使用分钟收盘价，避免猜测同一分钟最高价和最低价的先后顺序。",
                "回落卖出比例表示从上涨峰值向当前网格基准回撤的比例；反弹买入比例表示从下跌谷值向当前网格基准反弹的比例。",
                "分钟 K 线不含真实买一卖一，程序以收盘价近似盘口，并将偏移后的成交价限制在该分钟高低价内。",
                "网格和直接持有使用完全相同的初始现金与底仓，直接持有组合不再交易。",
                "最低仓位按初始建仓股数计算，卖出信号不会使持仓低于该股数；设置为 0% 时允许完全空仓。",
                "默认按 ETF 规则不计印花税；佣金取成交金额乘费率与单笔最低佣金中的较高值。",
            ],
        }

    def run_summary(self, config: StrategyConfig, market: dict[str, Any]) -> dict[str, float | int]:
        """使用正式成交规则执行不生成图表和逐笔明细的轻量回测。

        该方法专门服务于参数优化。它与 :meth:`run` 共用信号判断、成交价格、
        整手数量、资金限制和佣金计算，仅省略体积较大的图表点与成交明细，从而
        在大量参数组合下显著降低对象创建和进程间传输开销。

        Args:
            config: 已完成校验且包含本次候选参数的策略配置。
            market: 包含按时间排序前原始分钟 K 线的行情对象。

        Returns:
            包含最终资产、收益、超额收益、最大回撤、成交统计和佣金的轻量摘要。

        Raises:
            ValueError: 有效分钟 K 线不足两根时抛出。
        """

        bars = sorted(market.get("bars") or [], key=lambda item: str(item.get("timestamp", "")))
        if len(bars) < 2:
            raise ValueError("可用分钟 K 线不足，无法进行回测")

        first_price = float(bars[0]["close"])
        last_price = float(bars[-1]["close"])
        target_position_value = config.initial_capital * config.initial_position_percent / 100
        initial_shares = self._floor_to_lot(target_position_value / first_price, config.lot_size)
        minimum_shares = self._calculate_minimum_shares(initial_shares, config)
        initial_cash = config.initial_capital - initial_shares * first_price
        initial_assets = initial_cash + initial_shares * first_price
        state = SimulationState(
            config.base_price,
            initial_cash,
            initial_shares,
            initial_assets,
            minimum_shares,
            record_trade_details=False,
        )
        grid_peak_assets = initial_assets
        grid_max_drawdown = 0.0

        for bar in bars:
            self._process_bar(config, state, bar)
            grid_assets, _, _ = self._update_exposure_stats(state, float(bar["close"]))
            grid_peak_assets = max(grid_peak_assets, grid_assets)
            grid_max_drawdown = max(
                grid_max_drawdown,
                self._percentage_drop(grid_peak_assets, grid_assets),
            )

        grid_final_assets = state.cash + state.shares * last_price
        hold_final_assets = initial_cash + initial_shares * last_price
        grid_profit = grid_final_assets - initial_assets
        hold_profit = hold_final_assets - initial_assets
        grid_return = grid_profit / initial_assets * 100
        hold_return = hold_profit / initial_assets * 100
        exposure_metrics = self._build_exposure_metrics(state, grid_final_assets, last_price, grid_return)
        return {
            # 排名使用未舍入值，展示层再按普通报告口径格式化。
            "final_assets_raw": grid_final_assets,
            "return_percent_raw": grid_return,
            "max_drawdown_percent_raw": grid_max_drawdown,
            "final_assets": self._round_money(grid_final_assets),
            "profit": self._round_money(grid_profit),
            "return_percent": self._round_percent(grid_return),
            "excess_return_percent": self._round_percent(grid_return - hold_return),
            "max_drawdown_percent": self._round_percent(grid_max_drawdown),
            "commission": self._round_money(state.commissions),
            "trade_count": state.buy_count + state.sell_count,
            "buy_count": state.buy_count,
            "sell_count": state.sell_count,
            "skipped_for_funds": state.skipped_for_funds,
            "skipped_for_shares": state.skipped_for_shares,
            "skipped_for_minimum_position": state.skipped_for_minimum_position,
            **exposure_metrics,
        }

    @classmethod
    def _calculate_minimum_shares(cls, initial_shares: int, config: StrategyConfig) -> int:
        """根据初始建仓股数计算回测期间必须保留的最小持仓。
        Args:
            initial_shares: 回测开始时按首根行情价格建立的初始股数。
            config: 包含最低仓位比例和每手数量的策略配置。
        Returns:
            已按每手数量向下取整、且不超过初始股数的最低持仓股数。
        """

        target_shares = initial_shares * config.minimum_position_percent / 100
        return min(initial_shares, cls._floor_to_lot(target_shares, config.lot_size))

    @staticmethod
    def _update_exposure_stats(state: SimulationState, close: float) -> tuple[float, float, float]:
        """按当前收盘价累计仓位、现金和最低仓位持续时间统计。
        Args:
            state: 当前回测账户状态及其累计统计字段。
            close: 当前分钟 K 线收盘价。
        Returns:
            当前网格组合总资产、持仓占比和现金占比。
        """

        position_value = state.shares * close
        total_assets = state.cash + position_value
        if total_assets > 0:
            position_percent = position_value / total_assets * 100
            cash_percent = max(0.0, state.cash / total_assets * 100)
        else:
            position_percent = 0.0
            cash_percent = 0.0
        state.bars_processed += 1
        state.position_percent_sum += position_percent
        state.max_cash_percent = max(state.max_cash_percent, cash_percent)
        if state.shares <= state.minimum_shares:
            state.minimum_position_bar_count += 1
            state.current_minimum_position_bars += 1
            state.longest_minimum_position_bars = max(
                state.longest_minimum_position_bars,
                state.current_minimum_position_bars,
            )
        else:
            state.current_minimum_position_bars = 0
        return total_assets, position_percent, cash_percent

    def _build_exposure_metrics(
        self,
        state: SimulationState,
        final_assets: float,
        last_price: float,
        grid_return: float,
    ) -> dict[str, float | int | bool]:
        """整理完整回测和轻量回测共同使用的仓位利用率指标。
        Args:
            state: 已经处理完全部行情的账户状态。
            final_assets: 以最后收盘价估值后的网格组合总资产。
            last_price: 回测区间最后一根 K 线的收盘价。
            grid_return: 网格组合最终收益率，用于判断低仓位正收益提示。
        Returns:
            可直接合并到报告或优化摘要中的仓位统计字典。
        """

        final_position_value = state.shares * last_price
        if final_assets > 0:
            final_position_percent = final_position_value / final_assets * 100
            final_cash_percent = max(0.0, state.cash / final_assets * 100)
        else:
            final_position_percent = 0.0
            final_cash_percent = 0.0
        average_position_percent = state.position_percent_sum / max(1, state.bars_processed)
        low_exposure_warning = grid_return > 0 and (
            average_position_percent < self.LOW_EXPOSURE_WARNING_PERCENT
            or final_position_percent < self.LOW_EXPOSURE_WARNING_PERCENT
        )
        return {
            "minimum_shares": state.minimum_shares,
            "average_position_percent": self._round_percent(average_position_percent),
            "final_position_percent": self._round_percent(final_position_percent),
            "final_cash_percent": self._round_percent(final_cash_percent),
            "max_cash_percent": self._round_percent(state.max_cash_percent),
            "minimum_position_bar_count": state.minimum_position_bar_count,
            "longest_minimum_position_bars": state.longest_minimum_position_bars,
            "skipped_for_minimum_position": state.skipped_for_minimum_position,
            "low_exposure_warning": low_exposure_warning,
        }

    def _process_bar(self, config: StrategyConfig, state: SimulationState, bar: dict[str, Any]) -> None:
        """处理一根分钟 K 线并在满足确认条件时模拟成交。

        Args:
            config: 策略参数。
            state: 当前可变模拟账户状态。
            bar: 当前分钟 K 线 JSON 对象。

        Returns:
            无返回值；信号、资产和成交直接写入状态。
        """

        close = float(bar["close"])
        if close < config.lower_bound or close > config.upper_bound:
            return
        rise_trigger = state.reference_price * (1 + config.rise_trigger_percent / 100)
        fall_trigger = state.reference_price * (1 - config.fall_trigger_percent / 100)
        if close >= rise_trigger:
            state.armed_rise_peak = max(state.armed_rise_peak or close, close)
        if state.armed_rise_peak is not None:
            state.armed_rise_peak = max(state.armed_rise_peak, close)
            sell_confirmation = state.armed_rise_peak - (state.armed_rise_peak - state.reference_price) * config.sell_pullback_percent / 100
            if close <= sell_confirmation:
                self._execute(config, state, bar, "SELL", state.armed_rise_peak)
                return
        if close <= fall_trigger:
            state.armed_fall_trough = min(state.armed_fall_trough if state.armed_fall_trough is not None else close, close)
        if state.armed_fall_trough is not None:
            state.armed_fall_trough = min(state.armed_fall_trough, close)
            buy_confirmation = state.armed_fall_trough + (state.reference_price - state.armed_fall_trough) * config.buy_rebound_percent / 100
            if close >= buy_confirmation:
                self._execute(config, state, bar, "BUY", state.armed_fall_trough)

    def _execute(self, config: StrategyConfig, state: SimulationState, bar: dict[str, Any], side: str, trigger_extreme: float) -> None:
        """应用整手、佣金、现金与底仓约束记录一笔模拟成交。

        Args:
            config: 策略参数。
            state: 当前模拟账户状态。
            bar: 产生确认信号的分钟 K 线。
            side: `BUY` 或 `SELL` 方向。
            trigger_extreme: 本轮下跌谷值或上涨峰值。

        Returns:
            无返回值；成功成交会更新资金、持仓、基准和成交列表。
        """

        close = float(bar["close"])
        estimated_price = close + config.buy_price_offset if side == "BUY" else close - config.sell_price_offset
        price = min(float(bar["high"]), estimated_price) if side == "BUY" else max(float(bar["low"]), estimated_price)
        quantity = self._floor_to_lot(config.order_amount / price, config.lot_size)
        if quantity <= 0:
            return
        if side == "SELL":
            if state.minimum_shares > 0:
                # 启用最低仓位时，把订单数量截断到可卖出的战术仓位。
                sellable_shares = self._floor_to_lot(state.shares - state.minimum_shares, config.lot_size)
                quantity = min(quantity, sellable_shares)
                if quantity <= 0:
                    state.skipped_for_minimum_position += 1
                    state.armed_rise_peak = None
                    return
            elif quantity > state.shares:
                # 最低仓位为 0% 时保留旧行为：固定订单无法完整成交则跳过。
                state.skipped_for_shares += 1
                state.armed_rise_peak = None
                return
        if side == "BUY":
            while quantity > 0 and quantity * price + self._commission(quantity * price, config) > state.cash:
                quantity -= config.lot_size
            if quantity <= 0:
                state.skipped_for_funds += 1
                state.armed_fall_trough = None
                return
        gross_amount = quantity * price
        commission = self._commission(gross_amount, config)
        reference_before = state.reference_price
        if side == "BUY":
            state.cash -= gross_amount + commission
            state.shares += quantity
            state.buy_count += 1
        else:
            state.cash += gross_amount - commission
            state.shares -= quantity
            state.sell_count += 1
        state.commissions += commission
        state.reference_price = price
        state.armed_rise_peak = None
        state.armed_fall_trough = None
        position_value_after = state.shares * price
        total_assets_after = state.cash + position_value_after
        profit_after = total_assets_after - state.initial_assets
        if not state.record_trade_details:
            return
        state.trades.append({
            "timestamp": bar["timestamp"],
            "side": side,
            "price": self._round_price(price),
            "quantity": quantity,
            "gross_amount": self._round_money(gross_amount),
            "commission": self._round_money(commission),
            "reference_price_before": self._round_price(reference_before),
            "trigger_extreme": self._round_price(trigger_extreme),
            "cash_after": self._round_money(state.cash),
            "shares_after": state.shares,
            "position_value_after": self._round_money(position_value_after),
            "total_assets_after": self._round_money(total_assets_after),
            "profit_after": self._round_money(profit_after),
            "return_percent_after": self._round_percent(profit_after / state.initial_assets * 100),
        })

    @staticmethod
    def _floor_to_lot(quantity: float, lot_size: int) -> int:
        """将证券数量向下取整到完整交易单位。

        Args:
            quantity: 原始证券数量。
            lot_size: 每手证券数量。

        Returns:
            不超过原数量的整手证券数量。
        """

        return int(quantity // lot_size) * lot_size

    @staticmethod
    def _commission(gross_amount: float, config: StrategyConfig) -> float:
        """计算应用单笔最低值后的交易佣金。

        Args:
            gross_amount: 本笔成交金额。
            config: 包含费率和最低佣金的策略参数。

        Returns:
            本笔应计佣金。
        """

        return max(gross_amount * config.commission_rate, config.minimum_commission)

    @staticmethod
    def _percentage_drop(peak_assets: float, current_assets: float) -> float:
        """计算资产从历史峰值到当前值的非负回撤百分比。

        Args:
            peak_assets: 截至当前的资产峰值。
            current_assets: 当前资产值。

        Returns:
            非负回撤百分比。
        """

        return 0.0 if peak_assets <= 0 else max(0.0, (peak_assets - current_assets) / peak_assets * 100)

    @staticmethod
    def _round_money(value: float) -> float:
        """将金额四舍五入到人民币分。

        Args:
            value: 原始浮点金额。

        Returns:
            保留两位小数的金额。
        """

        return round(value + 1e-10, 2)

    @staticmethod
    def _round_percent(value: float) -> float:
        """将百分数四舍五入到四位小数。

        Args:
            value: 原始百分数。

        Returns:
            保留四位小数的百分数。
        """

        return round(value + 1e-10, 4)

    @staticmethod
    def _round_price(value: float) -> float:
        """将证券价格四舍五入到六位小数。

        Args:
            value: 原始浮点价格。

        Returns:
            保留六位小数的证券价格。
        """

        return round(value + 1e-12, 6)
