"""协调配置、行情、回测引擎和 JSON 报告持久化的应用服务。"""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .config import StrategyConfig
from .engine import GridBacktestEngine
from .market_data import YahooMinuteMarketDataProvider
from .storage import JsonStorage


class BacktestService:
    """为 HTTP 层提供无数据库依赖的网格回测用例。"""

    def __init__(self, storage: JsonStorage) -> None:
        """组装 JSON 存储、行情提供器和纯计算回测引擎。

        Args:
            storage: 配置、行情缓存和报告共用的 JSON 存储。
        """

        self.storage = storage
        self.market_provider = YahooMinuteMarketDataProvider(storage)
        self.engine = GridBacktestEngine()

    def get_config(self) -> dict[str, Any]:
        """读取最近配置，首次启动时返回内置的 588000 默认方案。

        Returns:
            已完成校验并补齐默认字段的策略 JSON 对象。
        """

        saved = self.storage.read_config()
        return StrategyConfig.from_dict(saved).to_dict()

    def run(self, raw_config: dict[str, Any] | None) -> dict[str, Any]:
        """校验方案、获取行情、计算结果并保存完整 JSON 报告。

        Args:
            raw_config: HTTP 请求提交的策略字段；None 表示使用默认方案。

        Returns:
            已带报告编号和创建时间的完整回测结果。
        """

        config = StrategyConfig.from_dict(raw_config)
        market, warnings = self.market_provider.fetch_recent(config)
        report = self.engine.run(config, market, warnings)
        now = datetime.now(timezone.utc)
        report_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
        report = {"report_id": report_id, "created_at": now.isoformat(), **report}
        self.storage.write_config(config.to_dict())
        report_path = self.storage.write_report(report_id, report)
        report["report_file"] = str(report_path)
        return report

    def list_reports(self, limit: int = 20) -> list[dict[str, Any]]:
        """读取最近回测报告摘要。

        Args:
            limit: 最多返回的报告数量。

        Returns:
            按创建时间倒序排列的报告摘要列表。
        """

        return self.storage.list_reports(limit)

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        """按编号读取历史完整报告。

        Args:
            report_id: 报告文件名中不含扩展名的安全编号。

        Returns:
            完整报告；不存在或编号非法时返回 None。
        """

        return self.storage.read_report(report_id)

