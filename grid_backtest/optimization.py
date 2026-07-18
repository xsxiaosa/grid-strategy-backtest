"""四参数分阶段优化、后台任务状态和多进程轻量回测。"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from itertools import product
from typing import Any, Iterable
from uuid import uuid4

from .config import StrategyConfig
from .engine import GridBacktestEngine
from .market_data import YahooMinuteMarketDataProvider
from .storage import JsonStorage


PARAMETER_NAMES = (
    "rise_trigger_percent",
    "sell_pullback_percent",
    "fall_trigger_percent",
    "buy_rebound_percent",
)
PARAMETER_DECIMAL_PLACES = 2
COARSE_VALUE_LIMIT = 15
MAX_COARSE_VALUE_LIMIT = 25
REFINEMENT_SEED_LIMIT = 20
HISTORICAL_JOB_LIMIT = 10
MAX_HISTORICAL_CANDIDATES = HISTORICAL_JOB_LIMIT * 20
MAX_ESTIMATED_COMBINATIONS = COARSE_VALUE_LIMIT**4 + REFINEMENT_SEED_LIMIT * 2 * 5**4 * 2 + MAX_HISTORICAL_CANDIDATES
EVALUATION_CHUNK_SIZE = 128
PROGRESS_LOG_PERCENT_STEP = 5
PROGRESS_LOG_INTERVAL_SECONDS = 10.0
LOGGER = logging.getLogger(__name__)
_WORKER_CONFIG: StrategyConfig | None = None
_WORKER_MARKET: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ParameterRange:
    """表示单个正百分比参数允许搜索的闭区间。"""

    minimum: float
    maximum: float

    def validate(self, display_name: str) -> None:
        """校验百分比搜索范围满足交易软件和策略配置的业务边界。

        Args:
            display_name: 用于中文错误消息的参数名称。

        Returns:
            无返回值；合法范围直接结束。

        Raises:
            ValueError: 最小值、最大值、两位小数精度或二者顺序不合法时抛出。
        """

        if self.minimum < 0.01 or self.maximum > 100 or self.minimum > self.maximum:
            raise ValueError(f"{display_name}范围必须满足 0.01 ≤ 最小值 ≤ 最大值 ≤ 100")
        # 实际交易软件只接受两位小数，因此从搜索范围入口阻止更高精度。
        if any(
            not math.isclose(value, round(value, PARAMETER_DECIMAL_PLACES), abs_tol=1e-9)
            for value in (self.minimum, self.maximum)
        ):
            raise ValueError(f"{display_name}范围最多只能保留两位小数")


@dataclass(frozen=True, slots=True)
class OptimizationRanges:
    """保存四个参与迭代搜索的百分比闭区间。"""

    rise_trigger_percent: ParameterRange
    sell_pullback_percent: ParameterRange
    fall_trigger_percent: ParameterRange
    buy_rebound_percent: ParameterRange

    @classmethod
    def from_dict(cls, raw: Any) -> "OptimizationRanges":
        """从 HTTP JSON 构造并校验四参数范围。

        Args:
            raw: 键名为四个策略字段、值含 ``minimum`` 和 ``maximum`` 的对象。

        Returns:
            已完成数值转换和业务校验的不可变范围对象。

        Raises:
            ValueError: 结构缺失、字段未知或数值范围非法时抛出。
        """

        if not isinstance(raw, dict):
            raise ValueError("ranges 必须是包含四个参数范围的对象")
        if set(raw) != set(PARAMETER_NAMES):
            raise ValueError("ranges 必须且只能包含四个可优化参数")
        parsed: dict[str, ParameterRange] = {}
        display_names = {
            "rise_trigger_percent": "上涨触发",
            "sell_pullback_percent": "回落卖出",
            "fall_trigger_percent": "下跌触发",
            "buy_rebound_percent": "反弹买入",
        }
        for name in PARAMETER_NAMES:
            value = raw[name]
            if not isinstance(value, dict) or set(value) != {"minimum", "maximum"}:
                raise ValueError(f"{display_names[name]}范围必须包含 minimum 和 maximum")
            try:
                parameter_range = ParameterRange(float(value["minimum"]), float(value["maximum"]))
            except (TypeError, ValueError) as error:
                raise ValueError(f"{display_names[name]}范围必须是有效数字") from error
            parameter_range.validate(display_names[name])
            parsed[name] = parameter_range
        return cls(**parsed)

    def to_dict(self) -> dict[str, dict[str, float]]:
        """转换为可直接写入 JSON 的普通字典。

        Returns:
            四个参数名称及其最小值、最大值对象。
        """

        return {name: asdict(getattr(self, name)) for name in PARAMETER_NAMES}


@dataclass(slots=True)
class OptimizationJob:
    """保存一个后台优化任务的可变进度、排名和取消信号。"""

    job_id: str
    config: StrategyConfig
    ranges: OptimizationRanges
    created_at: str
    coarse_value_limit: int = COARSE_VALUE_LIMIT
    status: str = "queued"
    round_number: int = 0
    round_name: str = "等待开始"
    completed: int = 0
    estimated_total: int = MAX_ESTIMATED_COMBINATIONS
    elapsed_seconds: float = 0.0
    best: list[dict[str, Any]] = field(default_factory=list)
    worst: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    result_file: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        """按照本任务的粗采样数量初始化理论组合数。

        Returns:
            无返回值；仅在任务仍使用默认估算值时更新进度分母。
        """

        if self.estimated_total == MAX_ESTIMATED_COMBINATIONS:
            self.estimated_total = estimate_max_combinations(self.coarse_value_limit)

    def snapshot(self) -> dict[str, Any]:
        """在线程锁保护下复制可供 HTTP 返回的任务状态。

        Returns:
            不包含锁、线程事件等进程内对象的 JSON 兼容字典。
        """

        with self.lock:
            return {
                "job_id": self.job_id,
                "created_at": self.created_at,
                "status": self.status,
                "round_number": self.round_number,
                "round_name": self.round_name,
                "completed": self.completed,
                "estimated_total": self.estimated_total,
                "elapsed_seconds": round(self.elapsed_seconds, 2),
                "best": self.best,
                "worst": self.worst,
                "error": self.error,
                "result_file": self.result_file,
                "config": self.config.to_dict(),
                "ranges": self.ranges.to_dict(),
                "coarse_value_limit": self.coarse_value_limit,
                "is_heuristic": True,
            }


class OptimizationCancelled(Exception):
    """表示用户主动取消仍在运行的优化任务。"""


def validate_coarse_value_limit(value: Any) -> int:
    """校验并返回用户指定的每维等比粗采样数量。

    Args:
        value: 来自优化任务 JSON 的原始采样数量。

    Returns:
        位于 2 至 25 之间的整数采样数量。

    Raises:
        ValueError: 输入不是整数或超出页面允许范围时抛出。
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("等比粗采样值必须是 2 至 25 之间的整数")
    if value < 2 or value > MAX_COARSE_VALUE_LIMIT:
        raise ValueError("等比粗采样值必须满足 2 ≤ 采样值 ≤ 25")
    return value


def estimate_max_combinations(coarse_value_limit: int) -> int:
    """计算指定粗采样密度下三轮搜索的理论组合数上限。

    Args:
        coarse_value_limit: 每个参数最多生成的等比粗采样数量。

    Returns:
        粗搜索、两轮邻域搜索和历史候选数量的理论总上限。
    """

    return coarse_value_limit**4 + REFINEMENT_SEED_LIMIT * 2 * 5**4 * 2 + MAX_HISTORICAL_CANDIDATES


def normalize_parameter(value: float) -> float:
    """将候选百分比统一舍入到交易软件支持的两位小数。

    Args:
        value: 计算得到的原始正百分比。

    Returns:
        保留两位小数且可直接录入交易软件的浮点候选值。
    """

    return round(float(value), PARAMETER_DECIMAL_PLACES)


def build_coarse_values(
    parameter_range: ParameterRange,
    current: float,
    limit: int = COARSE_VALUE_LIMIT,
) -> tuple[float, ...]:
    """在正数闭区间内生成包含边界和当前值的等比粗采样。

    Args:
        parameter_range: 本参数允许搜索的最小值和最大值。
        current: 当前普通回测配置中的参数值。
        limit: 最多返回的候选数量，默认十五个。

    Returns:
        已按升序排列、两位小数归一化且不超过上限的候选元组。
    """

    minimum = parameter_range.minimum
    maximum = parameter_range.maximum
    if math.isclose(minimum, maximum):
        return (normalize_parameter(minimum),)
    include_current = minimum < current < maximum
    geometric_slots = max(2, limit - 1 if include_current else limit)
    ratio = (maximum / minimum) ** (1 / (geometric_slots - 1))
    values = {normalize_parameter(minimum * ratio**index) for index in range(geometric_slots)}
    values.update((normalize_parameter(minimum), normalize_parameter(maximum)))
    if include_current:
        values.add(normalize_parameter(current))
    return tuple(sorted(values))


def build_coarse_candidates(
    ranges: OptimizationRanges,
    config: StrategyConfig,
    limit: int = COARSE_VALUE_LIMIT,
) -> list[tuple[float, float, float, float]]:
    """构造第一轮四维等比粗采样笛卡尔积。

    Args:
        ranges: 四个参数的初始搜索范围。
        config: 用于补入当前参数值的固定策略配置。
        limit: 每个参数最多生成的等比粗采样数量。

    Returns:
        最多 ``limit`` 的四次方个、顺序稳定且互不重复的四参数元组。
    """

    dimensions = [
        build_coarse_values(getattr(ranges, name), float(getattr(config, name)), limit)
        for name in PARAMETER_NAMES
    ]
    return [tuple(values) for values in product(*dimensions)]


def build_neighborhood_candidates(
    seeds: Iterable[dict[str, Any]],
    ranges: OptimizationRanges,
    ratio: float,
) -> list[tuple[float, float, float, float]]:
    """围绕排名种子生成五点相对倍率的四维邻域组合。

    Args:
        seeds: 当前最好和最差结果的合集。
        ranges: 用于裁剪所有候选的初始闭区间。
        ratio: 本轮相邻候选倍率，例如 1.05 或 1.01。

    Returns:
        已裁剪、两位小数归一化并去重排序的候选列表。
    """

    candidates: set[tuple[float, float, float, float]] = set()
    exponents = (-2, -1, 0, 1, 2)
    for seed in seeds:
        dimensions: list[list[float]] = []
        for name in PARAMETER_NAMES:
            parameter_range = getattr(ranges, name)
            base_value = float(seed[name])
            values = {
                normalize_parameter(base_value * ratio**exponent)
                for exponent in exponents
                if parameter_range.minimum <= base_value * ratio**exponent <= parameter_range.maximum
            }
            dimensions.append(sorted(values))
        candidates.update(tuple(values) for values in product(*dimensions))
    return sorted(candidates)


def best_sort_key(result: dict[str, Any]) -> tuple[Any, ...]:
    """生成最优排名的确定性比较键。

    Args:
        result: 单个候选参数的轻量回测结果。

    Returns:
        依次体现高收益、低回撤、少成交、低佣金和参数字典序的元组。
    """

    return (
        -float(result["return_percent_raw"]),
        float(result["max_drawdown_percent_raw"]),
        int(result["trade_count"]),
        float(result["commission"]),
        *(float(result[name]) for name in PARAMETER_NAMES),
    )


def worst_sort_key(result: dict[str, Any]) -> tuple[Any, ...]:
    """生成最差排名的确定性比较键。

    Args:
        result: 单个候选参数的轻量回测结果。

    Returns:
        依次体现低收益、高回撤、多成交、高佣金和参数字典序的元组。
    """

    return (
        float(result["return_percent_raw"]),
        -float(result["max_drawdown_percent_raw"]),
        -int(result["trade_count"]),
        -float(result["commission"]),
        *(float(result[name]) for name in PARAMETER_NAMES),
    )


def select_extremes(results: Iterable[dict[str, Any]], limit: int = 10) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """从所有已评估结果中选择最好和最差的指定数量。

    Args:
        results: 任意可迭代的候选结果集合。
        limit: 两端各自最多保留的结果数量。

    Returns:
        按各自排名规则排序的最优列表和最差列表。
    """

    values = list(results)
    return sorted(values, key=best_sort_key)[:limit], sorted(values, key=worst_sort_key)[:limit]


def select_diverse_results(
    results: Iterable[dict[str, Any]],
    sort_key: Any,
    limit: int = REFINEMENT_SEED_LIMIT,
    separation_ratio: float = 1.05,
) -> list[dict[str, Any]]:
    """从有序收益结果中挑选参数位置分散的细化种子。

    单纯选择前二十名时，收益平台上的细小参数变化会占满全部名额，导致其他
    潜在优质区域没有机会进入下一轮。本方法优先保留排名靠前的结果，并要求新
    种子至少在一个参数上与所有已选种子相差指定比例。

    Args:
        results: 所有已完成轻量回测的候选结果。
        sort_key: 最优或最差方向的确定性排序键函数。
        limit: 最多选择的细化种子数量。
        separation_ratio: 判断两个候选是否属于同一局部区域的相对倍率。

    Returns:
        兼顾收益排名和参数空间覆盖度的候选结果列表。
    """

    ordered = sorted(results, key=sort_key)
    selected: list[dict[str, Any]] = []
    for candidate in ordered:
        is_separated = all(
            any(
                max(float(candidate[name]), float(existing[name]))
                / min(float(candidate[name]), float(existing[name]))
                >= separation_ratio
                for name in PARAMETER_NAMES
            )
            for existing in selected
        )
        if not selected or is_separated:
            selected.append(candidate)
        if len(selected) >= limit:
            return selected
    # 候选很少或参数完全相同时，用剩余排名结果补齐，不重复加入同一对象。
    selected_ids = {id(result) for result in selected}
    selected.extend(result for result in ordered if id(result) not in selected_ids)
    return selected[:limit]


def select_refinement_seeds(results: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """组合最优与最差方向的分散候选，作为下一轮邻域中心。

    Args:
        results: 截至当前轮次的全部唯一回测结果。

    Returns:
        两端各最多二十组且按参数元组去重的细化种子列表。
    """

    values = list(results)
    seeds = [
        *select_diverse_results(values, best_sort_key),
        *select_diverse_results(values, worst_sort_key),
    ]
    unique: dict[tuple[float, ...], dict[str, Any]] = {}
    for seed in seeds:
        key = tuple(float(seed[name]) for name in PARAMETER_NAMES)
        unique.setdefault(key, seed)
    return list(unique.values())


def public_result(result: dict[str, Any]) -> dict[str, Any]:
    """移除仅供内部精确排序使用的未舍入指标。

    Args:
        result: 含公开字段和 ``*_raw`` 排名字段的候选结果。

    Returns:
        可直接通过 API 展示和写入最终 JSON 的结果对象。
    """

    return {key: value for key, value in result.items() if not key.endswith("_raw")}


def _initialize_worker(config: StrategyConfig, market: dict[str, Any]) -> None:
    """初始化优化工作进程复用的固定配置与行情。

    Args:
        config: 所有组合共用、仅四个比例会被候选覆盖的配置。
        market: 本任务只获取一次的分钟行情。

    Returns:
        无返回值；数据写入当前工作进程的模块级只读引用。
    """

    global _WORKER_CONFIG, _WORKER_MARKET
    _WORKER_CONFIG = config
    _WORKER_MARKET = market


def _evaluate_chunk(candidates: list[tuple[float, float, float, float]]) -> list[dict[str, Any]]:
    """在单个工作进程中顺序评估一批候选组合。

    Args:
        candidates: 四个百分比依照 ``PARAMETER_NAMES`` 排列的候选元组。

    Returns:
        已附加四参数字段的轻量回测摘要列表。

    Raises:
        RuntimeError: 工作进程未通过初始化器获得固定数据时抛出。
    """

    if _WORKER_CONFIG is None or _WORKER_MARKET is None:
        raise RuntimeError("优化工作进程尚未初始化")
    engine = GridBacktestEngine()
    results: list[dict[str, Any]] = []
    for candidate in candidates:
        overrides = dict(zip(PARAMETER_NAMES, candidate, strict=True))
        config = replace(_WORKER_CONFIG, **overrides)
        result = engine.run_summary(config, _WORKER_MARKET)
        result.update(overrides)
        results.append(result)
    return results


class OptimizationManager:
    """协调单任务后台搜索、行情复用、进度查询、取消和结果持久化。"""

    def __init__(self, storage: JsonStorage, market_provider: YahooMinuteMarketDataProvider | None = None) -> None:
        """创建绑定指定 JSON 存储的优化任务管理器。

        Args:
            storage: 最终优化 JSON 与行情缓存使用的存储对象。
            market_provider: 可选行情提供器，测试时用于注入固定数据。
        """

        self.storage = storage
        self.market_provider = market_provider or YahooMinuteMarketDataProvider(storage)
        self.jobs: dict[str, OptimizationJob] = {}
        self.active_job_id: str | None = None
        self.lock = threading.Lock()

    def start(self, raw_request: dict[str, Any]) -> dict[str, Any]:
        """校验请求并启动唯一的后台优化任务。

        Args:
            raw_request: 包含 ``config`` 和 ``ranges`` 的 HTTP JSON 对象。

        Returns:
            新任务的初始状态快照。

        Raises:
            ValueError: 请求结构或策略字段不合法时抛出。
            RuntimeError: 已有排队或运行中的优化任务时抛出。
        """

        allowed_fields = {"config", "ranges", "coarse_value_limit"}
        if not isinstance(raw_request, dict) or not {"config", "ranges"}.issubset(raw_request) or not set(raw_request).issubset(allowed_fields):
            raise ValueError("优化请求必须包含 config 和 ranges，且只能额外包含 coarse_value_limit")
        config = StrategyConfig.from_dict(raw_request["config"])
        ranges = OptimizationRanges.from_dict(raw_request["ranges"])
        coarse_value_limit = validate_coarse_value_limit(raw_request.get("coarse_value_limit", COARSE_VALUE_LIMIT))
        now = datetime.now(timezone.utc)
        job_id = f"opt-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
        job = OptimizationJob(job_id, config, ranges, now.isoformat(), coarse_value_limit=coarse_value_limit)
        with self.lock:
            if self.active_job_id is not None:
                active = self.jobs.get(self.active_job_id)
                if active is not None and active.status in {"queued", "running"}:
                    raise RuntimeError("已有参数优化任务正在运行，请等待完成或先取消")
            self.jobs[job_id] = job
            self.active_job_id = job_id
        LOGGER.info(
            "优化任务已创建：任务=%s，证券=%s，行情天数=%d，粗搜索每维采样上限=%d",
            job.job_id,
            job.config.symbol,
            job.config.days,
            job.coarse_value_limit,
        )
        thread = threading.Thread(target=self._run_job, args=(job,), daemon=True, name=f"optimization-{job_id}")
        thread.start()
        return job.snapshot()

    def get(self, job_id: str) -> dict[str, Any] | None:
        """读取内存任务或已完成持久化任务的状态。

        Args:
            job_id: 创建任务时返回的安全编号。

        Returns:
            找到时返回状态快照，否则返回 ``None``。
        """

        job = self.jobs.get(job_id)
        if job is not None:
            return job.snapshot()
        return self.storage.read_optimization(job_id)

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        """请求取消指定的排队或运行任务。

        Args:
            job_id: 需要取消的优化任务编号。

        Returns:
            找到任务时返回最新快照，否则返回 ``None``。
        """

        job = self.jobs.get(job_id)
        if job is None:
            return None
        if job.status in {"queued", "running"}:
            job.cancel_event.set()
            LOGGER.info("优化任务收到取消请求：任务=%s，当前状态=%s", job.job_id, job.status)
        return job.snapshot()

    def _run_job(self, job: OptimizationJob) -> None:
        """在后台线程中执行三轮候选生成和分块多进程回测。

        Args:
            job: 已登记且包含固定配置、范围和取消事件的任务。

        Returns:
            无返回值；所有状态、排名和结果文件直接更新到任务对象。
        """

        started = time.perf_counter()
        all_results: dict[tuple[float, float, float, float], dict[str, Any]] = {}
        executor: ProcessPoolExecutor | None = None
        try:
            with job.lock:
                job.status = "running"
                job.round_name = "正在获取行情"
            LOGGER.info("优化任务开始运行：任务=%s，正在获取并准备分钟行情", job.job_id)
            market, _ = self.market_provider.fetch_recent(job.config)
            worker_count = max(1, min(8, (os.cpu_count() or 2) - 1))
            LOGGER.info(
                "优化任务行情准备完成：任务=%s，行情条数=%d，工作进程=%d，耗时=%.2f秒",
                job.job_id,
                len(market.get("bars", [])),
                worker_count,
                time.perf_counter() - started,
            )
            executor = ProcessPoolExecutor(
                max_workers=worker_count,
                initializer=_initialize_worker,
                initargs=(job.config, market),
            )

            coarse = build_coarse_candidates(job.ranges, job.config, job.coarse_value_limit)
            # 相同固定条件的旧结果是已经付出计算成本的有效样本；扩大范围时必须保留。
            historical_candidates = self._compatible_historical_candidates(job)
            LOGGER.info(
                "优化任务粗搜索候选已生成：任务=%s，新生成=%d，兼容历史候选=%d",
                job.job_id,
                len(coarse),
                len(historical_candidates),
            )
            coarse.extend(historical_candidates)
            coarse = list(dict.fromkeys(coarse))
            self._evaluate_stage(job, executor, 1, "全范围等比粗搜索", coarse, all_results, started)

            medium = build_neighborhood_candidates(select_refinement_seeds(all_results.values()), job.ranges, 1.05)
            self._evaluate_stage(job, executor, 2, "两端 5% 邻域搜索", medium, all_results, started)

            fine = build_neighborhood_candidates(select_refinement_seeds(all_results.values()), job.ranges, 1.01)
            self._evaluate_stage(job, executor, 3, "两端 1% 精细搜索", fine, all_results, started)
            best, worst = select_extremes(all_results.values())
            with job.lock:
                job.round_name = "正在保存优化结果"
                job.elapsed_seconds = time.perf_counter() - started
                job.best = [public_result(result) for result in best]
                job.worst = [public_result(result) for result in worst]
            result_path = self.storage.write_optimization(job.job_id, job.snapshot())
            with job.lock:
                job.result_file = str(result_path)
                job.status = "completed"
                job.round_name = "迭代搜索完成"
            # 第二次原子替换确保最终 JSON 包含完成状态和自身结果路径。
            self.storage.write_optimization(job.job_id, job.snapshot())
            LOGGER.info(
                "优化任务完成：任务=%s，唯一组合=%d，总耗时=%.2f秒，结果=%s",
                job.job_id,
                job.completed,
                job.elapsed_seconds,
                result_path,
            )
        except OptimizationCancelled:
            with job.lock:
                job.status = "cancelled"
                job.round_name = "已取消"
                job.elapsed_seconds = time.perf_counter() - started
            LOGGER.info(
                "优化任务已取消：任务=%s，已完成=%d，总耗时=%.2f秒",
                job.job_id,
                job.completed,
                job.elapsed_seconds,
            )
        except Exception as error:  # noqa: BLE001 - 后台边界必须把失败转换为可查询状态。
            with job.lock:
                job.status = "failed"
                job.round_name = "计算失败"
                job.elapsed_seconds = time.perf_counter() - started
                job.error = str(error)
            LOGGER.exception(
                "优化任务失败：任务=%s，已完成=%d，总耗时=%.2f秒",
                job.job_id,
                job.completed,
                job.elapsed_seconds,
            )
        finally:
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
            with self.lock:
                if self.active_job_id == job.job_id:
                    self.active_job_id = None

    def _compatible_historical_candidates(
        self,
        job: OptimizationJob,
    ) -> list[tuple[float, float, float, float]]:
        """读取固定条件相同且位于当前范围内的历史两端参数。

        Args:
            job: 当前优化任务，其配置和范围用于兼容性判断与候选裁剪。

        Returns:
            最近十个兼容任务中最好、最差结果对应的唯一四参数元组。
        """

        current_fixed = {
            name: value
            for name, value in job.config.to_dict().items()
            if name not in PARAMETER_NAMES
        }
        candidates: set[tuple[float, float, float, float]] = set()
        for historical in self.storage.list_optimizations(HISTORICAL_JOB_LIMIT):
            historical_config = historical.get("config")
            if not isinstance(historical_config, dict):
                continue
            historical_fixed = {
                name: value
                for name, value in historical_config.items()
                if name not in PARAMETER_NAMES
            }
            if historical_fixed != current_fixed:
                continue
            for result in [*(historical.get("best") or []), *(historical.get("worst") or [])]:
                if not isinstance(result, dict) or any(name not in result for name in PARAMETER_NAMES):
                    continue
                candidate = tuple(normalize_parameter(float(result[name])) for name in PARAMETER_NAMES)
                if all(
                    getattr(job.ranges, name).minimum <= candidate[index] <= getattr(job.ranges, name).maximum
                    for index, name in enumerate(PARAMETER_NAMES)
                ):
                    candidates.add(candidate)
        return sorted(candidates)

    def _evaluate_stage(
        self,
        job: OptimizationJob,
        executor: ProcessPoolExecutor,
        round_number: int,
        round_name: str,
        candidates: list[tuple[float, float, float, float]],
        all_results: dict[tuple[float, float, float, float], dict[str, Any]],
        started: float,
    ) -> None:
        """过滤已算候选、分块提交进程池并持续发布本轮进度。

        Args:
            job: 接收进度和排名更新的后台任务。
            executor: 已用本任务固定配置和行情初始化的进程池。
            round_number: 当前搜索轮次编号。
            round_name: 面向页面展示的中文轮次名称。
            candidates: 本轮生成的全部四参数候选。
            all_results: 跨轮次复用的候选到结果映射。
            started: 整个任务的单调计时起点。

        Returns:
            无返回值；新结果写入 ``all_results`` 并同步更新任务状态。

        Raises:
            OptimizationCancelled: 用户请求取消时在批次边界抛出。
        """

        new_candidates = [candidate for candidate in candidates if candidate not in all_results]
        skipped_candidates = len(candidates) - len(new_candidates)
        stage_started = time.perf_counter()
        with job.lock:
            job.round_number = round_number
            job.round_name = round_name
            job.estimated_total = job.completed + len(new_candidates)
        LOGGER.info(
            "优化阶段开始：任务=%s，轮次=%d/3，阶段=%s，待计算=%d，跳过已计算=%d，累计已完成=%d",
            job.job_id,
            round_number,
            round_name,
            len(new_candidates),
            skipped_candidates,
            len(all_results),
        )
        if job.cancel_event.is_set():
            raise OptimizationCancelled()
        chunks = [
            new_candidates[index:index + EVALUATION_CHUNK_SIZE]
            for index in range(0, len(new_candidates), EVALUATION_CHUNK_SIZE)
        ]
        futures = [executor.submit(_evaluate_chunk, chunk) for chunk in chunks]
        stage_completed = 0
        next_log_percent = PROGRESS_LOG_PERCENT_STEP
        last_log_time = stage_started
        for future in as_completed(futures):
            if job.cancel_event.is_set():
                for pending in futures:
                    pending.cancel()
                raise OptimizationCancelled()
            chunk_results = future.result()
            for result in chunk_results:
                key = tuple(float(result[name]) for name in PARAMETER_NAMES)
                all_results[key] = result
            stage_completed += len(chunk_results)
            best, worst = select_extremes(all_results.values())
            with job.lock:
                job.completed = len(all_results)
                job.elapsed_seconds = time.perf_counter() - started
                job.best = [public_result(result) for result in best]
                job.worst = [public_result(result) for result in worst]
            progress_percent = 100 if not new_candidates else stage_completed / len(new_candidates) * 100
            current_time = time.perf_counter()
            should_log_progress = (
                progress_percent >= next_log_percent
                or current_time - last_log_time >= PROGRESS_LOG_INTERVAL_SECONDS
                or stage_completed == len(new_candidates)
            )
            if should_log_progress:
                LOGGER.info(
                    "优化阶段进度：任务=%s，轮次=%d/3，阶段=%s，进度=%d/%d（%.1f%%），阶段耗时=%.2f秒，总耗时=%.2f秒",
                    job.job_id,
                    round_number,
                    round_name,
                    stage_completed,
                    len(new_candidates),
                    progress_percent,
                    current_time - stage_started,
                    current_time - started,
                )
                while next_log_percent <= progress_percent:
                    next_log_percent += PROGRESS_LOG_PERCENT_STEP
                last_log_time = current_time
        LOGGER.info(
            "优化阶段完成：任务=%s，轮次=%d/3，阶段=%s，本轮完成=%d，累计完成=%d，阶段耗时=%.2f秒",
            job.job_id,
            round_number,
            round_name,
            stage_completed,
            len(all_results),
            time.perf_counter() - stage_started,
        )
