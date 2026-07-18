"""基于普通 JSON 文件的配置、行情缓存和回测报告存储。"""

import json
import os
from pathlib import Path
from typing import Any


class JsonStorage:
    """以原子替换方式管理项目运行期的全部 JSON 数据。"""

    def __init__(self, data_directory: Path) -> None:
        """初始化数据目录并创建配置、行情和报告所需子目录。

        Args:
            data_directory: 所有运行期 JSON 文件的根目录。
        """

        self.data_directory = data_directory.resolve()
        self.market_directory = self.data_directory / "market"
        self.report_directory = self.data_directory / "reports"
        self.optimization_directory = self.data_directory / "optimizations"
        self.market_directory.mkdir(parents=True, exist_ok=True)
        self.report_directory.mkdir(parents=True, exist_ok=True)
        self.optimization_directory.mkdir(parents=True, exist_ok=True)

    def read_config(self) -> dict[str, Any] | None:
        """读取最近一次保存的策略配置。

        Returns:
            已解析的配置字典；文件尚不存在时返回 None。
        """

        return self._read_json(self.data_directory / "config.json")

    def write_config(self, value: dict[str, Any]) -> None:
        """原子保存最近一次使用的策略配置。

        Args:
            value: 已通过业务校验的策略配置字典。

        Returns:
            无返回值；写入完成后目标文件是完整 JSON。
        """

        self._write_json(self.data_directory / "config.json", value)

    def market_path(self, symbol: str, days: int) -> Path:
        """生成指定证券和周期对应的行情缓存路径。

        Args:
            symbol: 六位证券代码。
            days: 最近自然日数量。

        Returns:
            位于 market 子目录且不含用户路径片段的 JSON 路径。
        """

        safe_symbol = "".join(character for character in symbol if character.isdigit())
        return self.market_directory / f"{safe_symbol}_1m_{days}d.json"

    def read_market(self, symbol: str, days: int) -> dict[str, Any] | None:
        """读取指定证券和周期的分钟行情缓存。

        Args:
            symbol: 六位证券代码。
            days: 最近自然日数量。

        Returns:
            已解析的行情缓存；缓存不存在时返回 None。
        """

        return self._read_json(self.market_path(symbol, days))

    def write_market(self, symbol: str, days: int, value: dict[str, Any]) -> None:
        """原子保存指定证券和周期的分钟行情缓存。

        Args:
            symbol: 六位证券代码。
            days: 最近自然日数量。
            value: 包含元信息和分钟 K 线的数据。

        Returns:
            无返回值；写入成功后旧缓存被完整替换。
        """

        self._write_json(self.market_path(symbol, days), value)

    def write_report(self, report_id: str, value: dict[str, Any]) -> Path:
        """保存完整回测报告并返回生成文件路径。

        Args:
            report_id: 服务端生成的安全报告编号。
            value: 完整回测结果。

        Returns:
            已成功写入的绝对 JSON 文件路径。
        """

        path = self.report_directory / f"{report_id}.json"
        self._write_json(path, value)
        return path.resolve()

    def read_report(self, report_id: str) -> dict[str, Any] | None:
        """按安全报告编号读取单个完整报告。

        Args:
            report_id: 只允许字母、数字、连字符和下划线的报告编号。

        Returns:
            已解析的报告；编号非法或文件不存在时返回 None。
        """

        if not report_id or any(not (character.isalnum() or character in "-_") for character in report_id):
            return None
        return self._read_json(self.report_directory / f"{report_id}.json")

    def list_reports(self, limit: int = 20) -> list[dict[str, Any]]:
        """按文件名倒序列出最近回测报告的轻量摘要。

        Args:
            limit: 最多读取和返回的报告数量。

        Returns:
            包含报告编号、时间、证券和收益对比的摘要列表。
        """

        summaries: list[dict[str, Any]] = []
        for path in sorted(self.report_directory.glob("*.json"), reverse=True)[: max(1, min(limit, 100))]:
            report = self._read_json(path)
            if not report:
                continue
            summaries.append({
                "report_id": report.get("report_id"),
                "created_at": report.get("created_at"),
                "symbol": report.get("market_data", {}).get("symbol"),
                "grid_profit": report.get("grid", {}).get("profit"),
                "hold_profit": report.get("buy_and_hold", {}).get("profit"),
                "winner": report.get("comparison", {}).get("winner"),
            })
        return summaries

    def write_optimization(self, job_id: str, value: dict[str, Any]) -> Path:
        """原子保存完成的四参数优化任务摘要。

        Args:
            job_id: 仅由服务端生成的安全优化任务编号。
            value: 包含任务条件、统计和两端各十组结果的 JSON 对象。

        Returns:
            已成功写入的优化 JSON 绝对路径。
        """

        path = self.optimization_directory / f"{job_id}.json"
        self._write_json(path, value)
        return path.resolve()

    def read_optimization(self, job_id: str) -> dict[str, Any] | None:
        """按安全任务编号读取已持久化的优化结果。

        Args:
            job_id: 只允许字母、数字、连字符和下划线的任务编号。

        Returns:
            已解析的优化结果；编号非法或文件不存在时返回 ``None``。
        """

        if not job_id or any(not (character.isalnum() or character in "-_") for character in job_id):
            return None
        return self._read_json(self.optimization_directory / f"{job_id}.json")

    def list_optimizations(self, limit: int = 10) -> list[dict[str, Any]]:
        """读取最近完成的优化结果供后续扩大范围时复用候选。

        Args:
            limit: 最多读取的历史优化文件数量，限制在 1 至 50 之间。

        Returns:
            按文件名倒序排列且能够正常解析的优化结果列表。
        """

        results: list[dict[str, Any]] = []
        paths = sorted(self.optimization_directory.glob("*.json"), reverse=True)
        for path in paths[: max(1, min(limit, 50))]:
            result = self._read_json(path)
            if result:
                results.append(result)
        return results

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        """读取单个 UTF-8 JSON 对象文件。

        Args:
            path: 已由存储层构造的目标文件路径。

        Returns:
            JSON 根对象；文件不存在时返回 None。

        Raises:
            ValueError: 文件存在但根节点不是 JSON 对象。
        """

        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError(f"JSON 文件根节点必须是对象：{path}")
        return value

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        """先写临时文件再原子替换，避免中断留下半个 JSON。

        Args:
            path: 最终目标文件路径。
            value: 需要序列化的 JSON 对象。

        Returns:
            无返回值；替换成功时目标文件已完整落盘。
        """

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        temporary.replace(path)
