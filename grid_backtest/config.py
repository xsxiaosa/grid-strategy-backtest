"""网格策略配置、默认方案和输入校验。"""

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """一次网格回测使用的完整参数集合。"""

    symbol: str = "588000"
    days: int = 30
    lower_bound: float = 1.45
    upper_bound: float = 2.05
    base_price: float = 1.74
    rise_trigger_percent: float = 2.5
    sell_pullback_percent: float = 20.0
    fall_trigger_percent: float = 2.5
    buy_rebound_percent: float = 30.0
    order_amount: float = 3000.0
    buy_price_offset: float = 0.001
    sell_price_offset: float = 0.001
    initial_capital: float = 30000.0
    initial_position_percent: float = 50.0
    minimum_position_percent: float = 20.0
    commission_rate: float = 0.00025
    minimum_commission: float = 5.0
    lot_size: int = 100

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "StrategyConfig":
        """从网页 JSON 构造配置，并统一转换数字字段类型。

        Args:
            raw: 网页或配置文件传入的字段；缺失字段采用默认方案。

        Returns:
            已完成类型转换和业务校验的不可变策略配置。

        Raises:
            ValueError: 字段类型、数值范围或监控区间不合法。
        """

        values = asdict(cls())
        if raw:
            unknown = sorted(set(raw) - set(values))
            if unknown:
                raise ValueError(f"存在未知策略字段：{', '.join(unknown)}")
            values.update(raw)
            # 旧配置可能只设置了较低的初始底仓，缺少新字段时不能凭空要求更高最低仓位。
            if "minimum_position_percent" not in raw:
                values["minimum_position_percent"] = min(
                    float(values["minimum_position_percent"]),
                    float(values["initial_position_percent"]),
                )
        try:
            config = cls(
                symbol=str(values["symbol"]).strip(),
                days=int(values["days"]),
                lower_bound=float(values["lower_bound"]),
                upper_bound=float(values["upper_bound"]),
                base_price=float(values["base_price"]),
                rise_trigger_percent=float(values["rise_trigger_percent"]),
                sell_pullback_percent=float(values["sell_pullback_percent"]),
                fall_trigger_percent=float(values["fall_trigger_percent"]),
                buy_rebound_percent=float(values["buy_rebound_percent"]),
                order_amount=float(values["order_amount"]),
                buy_price_offset=float(values["buy_price_offset"]),
                sell_price_offset=float(values["sell_price_offset"]),
                initial_capital=float(values["initial_capital"]),
                initial_position_percent=float(values["initial_position_percent"]),
                minimum_position_percent=float(values["minimum_position_percent"]),
                commission_rate=float(values["commission_rate"]),
                minimum_commission=float(values["minimum_commission"]),
                lot_size=int(values["lot_size"]),
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f"策略字段必须是有效数字：{error}") from error
        config.validate()
        return config

    def validate(self) -> None:
        """验证证券代码、价格、比例、资金和整手单位的业务边界。

        Returns:
            无返回值；全部字段合法时正常结束。

        Raises:
            ValueError: 任一策略字段不符合回测约束。
        """

        if len(self.symbol) != 6 or not self.symbol.isdigit():
            raise ValueError("证券代码必须是六位数字")
        if not 1 <= self.days <= 30:
            raise ValueError("分钟行情有效期必须在 1 至 30 个自然日之间")
        if self.lower_bound <= 0 or self.upper_bound <= self.lower_bound:
            raise ValueError("监控下限必须大于 0 且小于监控上限")
        if not self.lower_bound <= self.base_price <= self.upper_bound:
            raise ValueError("基准价必须位于监控区间内")
        positive_percentages = (self.rise_trigger_percent, self.sell_pullback_percent, self.fall_trigger_percent, self.buy_rebound_percent)
        if any(value <= 0 or value > 100 for value in positive_percentages):
            raise ValueError("触发、回落和反弹比例必须大于 0 且不超过 100%")
        if self.order_amount <= 0 or self.initial_capital <= 0:
            raise ValueError("单次金额和初始资金必须大于 0")
        if not 0 <= self.initial_position_percent <= 100:
            raise ValueError("初始底仓比例必须位于 0% 至 100% 之间")
        if not 0 <= self.minimum_position_percent <= 100:
            raise ValueError("最低仓位比例必须位于 0% 至 100% 之间")
        if self.minimum_position_percent > self.initial_position_percent:
            raise ValueError("最低仓位比例不得高于初始底仓比例")
        if self.buy_price_offset < 0 or self.sell_price_offset < 0:
            raise ValueError("委托价格偏移不得为负数")
        if self.commission_rate < 0 or self.minimum_commission < 0:
            raise ValueError("佣金费率和最低佣金不得为负数")
        if self.lot_size <= 0:
            raise ValueError("每手数量必须是正整数")

    def to_dict(self) -> dict[str, Any]:
        """将配置转换为可直接写入 JSON 的普通字典。

        Returns:
            字段名称稳定、只含 JSON 基础类型的策略字典。
        """

        return asdict(self)
