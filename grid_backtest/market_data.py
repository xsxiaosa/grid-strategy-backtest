"""Yahoo 分钟行情下载、交易时段过滤和 JSON 缓存回退。"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import StrategyConfig
from .storage import JsonStorage


SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))


@dataclass(frozen=True, slots=True)
class MinuteBar:
    """单根一分钟 K 线。"""

    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_dict(self) -> dict[str, Any]:
        """将分钟 K 线转换为可序列化的 JSON 对象。

        Returns:
            包含时间、开高低收和成交量的普通字典。
        """

        return asdict(self)


class YahooMinuteMarketDataProvider:
    """分段取得最近最多 30 天的一分钟行情，并维护本地 JSON 缓存。"""

    CHUNK_DAYS = 6

    def __init__(self, storage: JsonStorage, timeout_seconds: int = 20) -> None:
        """初始化行情提供器。

        Args:
            storage: JSON 缓存存储实现。
            timeout_seconds: 单个行情片段的网络超时秒数。
        """

        self.storage = storage
        self.timeout_seconds = timeout_seconds

    def fetch_recent(self, config: StrategyConfig) -> tuple[dict[str, Any], list[str]]:
        """优先在线获取最近分钟行情，失败时回退到同周期 JSON 缓存。

        Args:
            config: 包含证券代码和自然日周期的策略配置。

        Returns:
            行情元信息与 K 线对象，以及需要展示给用户的警告列表。

        Raises:
            RuntimeError: 在线请求失败且本地没有可用缓存。
        """

        try:
            market = self._download(config.symbol, config.days)
            self.storage.write_market(config.symbol, config.days, market)
            return market, []
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
            cached = self.storage.read_market(config.symbol, config.days)
            if cached and isinstance(cached.get("bars"), list) and len(cached["bars"]) >= 2:
                warning = f"在线行情获取失败，已使用 {cached.get('fetched_at', '未知时间')} 保存的 JSON 缓存：{error}"
                return cached, [warning]
            raise RuntimeError(f"分钟行情获取失败且没有可用 JSON 缓存：{error}") from error

    def fetch_daily_indicators(self, symbol: str) -> dict[str, Any]:
        """获取证券日线并计算页面输入区需要的均线指标。

        该方法使用最近日线收盘价计算 MA5、MA20、MA60 和 EMA60，用于在用户
        输入证券代码后快速展示当前价格位置。EMA60 使用全部返回日线递推，
        平滑系数为 ``2 / (60 + 1)``。

        Args:
            symbol: 六位沪深证券代码。

        Returns:
            包含证券名称、最新交易日、最新价、涨跌幅和各周期均线的 JSON 对象。

        Raises:
            RuntimeError: Yahoo 请求失败或有效日线少于 60 根时抛出。
        """

        yahoo_symbol = self._to_yahoo_symbol(symbol)
        requested_at = datetime.now(timezone.utc)
        # 取 400 个自然日，充分覆盖 60 个交易日及长假、停牌等缺口。
        parameters = urllib.parse.urlencode({
            "interval": "1d",
            "period1": int((requested_at - timedelta(days=400)).timestamp()),
            "period2": int((requested_at + timedelta(days=1)).timestamp()),
            "includePrePost": "false",
            "events": "div,splits",
        })
        payload = self._request_chart(yahoo_symbol, parameters)
        results = payload.get("chart", {}).get("result") or []
        if not results:
            raise RuntimeError("行情源没有返回日线数据")
        item = results[0]
        timestamps = item.get("timestamp") or []
        quotes = item.get("indicators", {}).get("quote") or []
        closes = quotes[0].get("close", []) if quotes else []
        daily_closes = [
            (int(timestamp), float(close))
            for timestamp, close in zip(timestamps, closes)
            if isinstance(timestamp, (int, float)) and isinstance(close, (int, float)) and close > 0
        ]
        if len(daily_closes) < 60:
            raise RuntimeError(f"有效日线仅 {len(daily_closes)} 根，不足以计算 MA60 和 EMA60")
        close_values = [close for _, close in daily_closes]
        latest_price = close_values[-1]
        previous_close = close_values[-2]
        meta = item.get("meta", {})
        ma60 = self._simple_moving_average(close_values, 60)
        return {
            "symbol": symbol,
            "name": meta.get("longName") or meta.get("shortName") or symbol,
            "as_of": datetime.fromtimestamp(daily_closes[-1][0], SHANGHAI_TIMEZONE).date().isoformat(),
            "latest_price": round(latest_price, 4),
            "change_percent": round((latest_price - previous_close) / previous_close * 100, 2),
            "ma5": round(self._simple_moving_average(close_values, 5), 4),
            "ma20": round(self._simple_moving_average(close_values, 20), 4),
            "ma60": round(ma60, 4),
            "ema60": round(self._exponential_moving_average(close_values, 60), 4),
            "ma60_deviation_percent": round((latest_price - ma60) / ma60 * 100, 2),
            "source": "Yahoo Finance Chart API（非交易所官方行情）",
        }

    def _request_chart(self, yahoo_symbol: str, parameters: str) -> dict[str, Any]:
        """使用已编码的参数请求 Yahoo Chart API，并在瞬时故障时重试。

        Args:
            yahoo_symbol: 带沪深市场后缀的 Yahoo 证券代码。
            parameters: 已经通过 URL 编码的 Chart API 查询参数。

        Returns:
            已解析且确认不含 Chart 错误的 JSON 根对象。

        Raises:
            RuntimeError: 三次请求均失败或上游返回业务错误时抛出。
        """

        errors: list[str] = []
        # query1 与 query2 返回相同 Chart 数据，交替请求可绕开单节点瞬时故障。
        for attempt in range(3):
            host = "query1.finance.yahoo.com" if attempt % 2 == 0 else "query2.finance.yahoo.com"
            url = f"https://{host}/v8/finance/chart/{urllib.parse.quote(yahoo_symbol)}?{parameters}"
            request = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 grid-strategy-backtest/1.0",
                "Accept": "application/json",
            })
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.load(response)
                chart_error = payload.get("chart", {}).get("error")
                if chart_error:
                    raise RuntimeError(str(chart_error.get("description") or chart_error))
                return payload
            except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, json.JSONDecodeError) as error:
                errors.append(f"第 {attempt + 1} 次：{error}")
                if attempt < 2:
                    time.sleep(0.4 * (attempt + 1))
        raise RuntimeError("；".join(errors))

    @staticmethod
    def _simple_moving_average(values: list[float], period: int) -> float:
        """计算最近指定周期的简单移动平均值。

        Args:
            values: 按时间升序排列的有效收盘价。
            period: 需要纳入平均的末尾交易日数。

        Returns:
            最近 ``period`` 个收盘价的算术平均值。

        Raises:
            ValueError: 周期非正数或价格数量不足时抛出。
        """

        if period <= 0 or len(values) < period:
            raise ValueError("移动平均周期必须为正数且不超过价格数量")
        return sum(values[-period:]) / period

    @staticmethod
    def _exponential_moving_average(values: list[float], period: int) -> float:
        """计算以首个收盘价为种子值的指数移动平均值。

        Args:
            values: 按时间升序排列的有效收盘价。
            period: EMA 的平滑周期，本页使用 60 个交易日。

        Returns:
            逐点递推至最新交易日的 EMA 值。

        Raises:
            ValueError: 收盘价为空或周期非正数时抛出。
        """

        if not values or period <= 0:
            raise ValueError("EMA 价格数组不得为空且周期必须为正数")
        multiplier = 2 / (period + 1)
        ema = values[0]
        for value in values[1:]:
            ema = (value - ema) * multiplier + ema
        return ema

    def _download(self, symbol: str, days: int) -> dict[str, Any]:
        """将完整周期拆成六天片段并合并去重分钟行情。

        Args:
            symbol: 六位沪深证券代码。
            days: 最近自然日数量。

        Returns:
            包含证券元信息、抓取时间和去重分钟 K 线的 JSON 对象。

        Raises:
            RuntimeError: 上游拒绝请求、响应报错或没有有效分钟数据。
        """

        yahoo_symbol = self._to_yahoo_symbol(symbol)
        requested_at = datetime.now(timezone.utc)
        # 为数据源的“最近 30 天”边界预留两分钟，防止网络耗时导致最早片段越界。
        started_at = requested_at - timedelta(days=days) + timedelta(minutes=2)
        bars_by_timestamp: dict[str, MinuteBar] = {}
        security_name = symbol
        cursor = started_at
        while cursor < requested_at:
            chunk_end = min(cursor + timedelta(days=self.CHUNK_DAYS), requested_at)
            response = self._request_chunk(yahoo_symbol, cursor, chunk_end)
            result = response.get("chart", {}).get("result")
            if not result:
                cursor = chunk_end
                continue
            item = result[0]
            meta = item.get("meta", {})
            security_name = meta.get("longName") or meta.get("shortName") or security_name
            for bar in self._parse_bars(item):
                bars_by_timestamp[bar.timestamp] = bar
            cursor = chunk_end
        bars = sorted(bars_by_timestamp.values(), key=lambda item: item.timestamp)
        if len(bars) < 2:
            raise RuntimeError("行情源没有返回足够的有效分钟数据")
        return {
            "symbol": symbol,
            "name": security_name,
            "source": "Yahoo Finance Chart API（非交易所官方行情）",
            "timezone": "Asia/Shanghai",
            "requested_days": days,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "bars": [bar.to_dict() for bar in bars],
        }

    def _request_chunk(self, yahoo_symbol: str, start: datetime, end: datetime) -> dict[str, Any]:
        """请求一个不超过六天的 Yahoo 分钟行情片段。

        Python urllib 默认读取系统及环境变量代理配置，因此无需额外代理依赖。

        Args:
            yahoo_symbol: 带市场后缀的 Yahoo 证券代码。
            start: 片段包含的 UTC 起始时间。
            end: 片段不包含的 UTC 结束时间。

        Returns:
            已解析的 Yahoo Chart JSON 根对象。

        Raises:
            RuntimeError: HTTP 状态异常或响应携带行情错误。
        """

        parameters = urllib.parse.urlencode({
            "interval": "1m",
            "period1": int(start.timestamp()),
            "period2": int(end.timestamp()),
            "includePrePost": "false",
            "events": "div,splits",
        })
        errors: list[str] = []
        # query1 与 query2 返回相同 Chart 数据，交替请求可以绕开单节点瞬时故障。
        for attempt in range(3):
            host = "query1.finance.yahoo.com" if attempt % 2 == 0 else "query2.finance.yahoo.com"
            url = f"https://{host}/v8/finance/chart/{urllib.parse.quote(yahoo_symbol)}?{parameters}"
            request = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 grid-strategy-backtest/1.0",
                "Accept": "application/json",
            })
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.load(response)
                chart_error = payload.get("chart", {}).get("error")
                if chart_error:
                    raise RuntimeError(str(chart_error.get("description") or chart_error))
                return payload
            except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, json.JSONDecodeError) as error:
                errors.append(f"第 {attempt + 1} 次：{error}")
                if attempt < 2:
                    time.sleep(0.4 * (attempt + 1))
        raise RuntimeError("；".join(errors))

    @staticmethod
    def _parse_bars(item: dict[str, Any]) -> list[MinuteBar]:
        """将 Yahoo 的并行指标数组转换为完整分钟 K 线。

        Args:
            item: Yahoo Chart 单个证券的结果对象。

        Returns:
            只保留上交所交易时段且价格完整的分钟 K 线列表。
        """

        timestamps = item.get("timestamp") or []
        quotes = item.get("indicators", {}).get("quote") or []
        quote = quotes[0] if quotes else {}
        bars: list[MinuteBar] = []
        for index, timestamp in enumerate(timestamps):
            try:
                open_price = quote.get("open", [])[index]
                high_price = quote.get("high", [])[index]
                low_price = quote.get("low", [])[index]
                close_price = quote.get("close", [])[index]
                volume = quote.get("volume", [])[index] or 0
            except IndexError:
                continue
            prices = (open_price, high_price, low_price, close_price)
            if any(not isinstance(value, (int, float)) or value <= 0 for value in prices):
                continue
            moment = datetime.fromtimestamp(timestamp, SHANGHAI_TIMEZONE)
            if not YahooMinuteMarketDataProvider._is_trading_minute(moment):
                continue
            bars.append(MinuteBar(moment.isoformat(), float(open_price), float(high_price), float(low_price), float(close_price), float(volume)))
        return bars

    @staticmethod
    def _is_trading_minute(moment: datetime) -> bool:
        """判断时间是否位于上海市场上午或下午交易时段。

        Args:
            moment: 已转换为上海时区的分钟时间。

        Returns:
            时间位于 09:30-11:30 或 13:00-15:00 时返回 True。
        """

        minute_of_day = moment.hour * 60 + moment.minute
        return 570 <= minute_of_day <= 690 or 780 <= minute_of_day <= 900

    @staticmethod
    def _to_yahoo_symbol(symbol: str) -> str:
        """根据六位代码首位推断沪深市场后缀。

        Args:
            symbol: 六位证券代码。

        Returns:
            Yahoo 使用的 `.SS` 或 `.SZ` 后缀证券代码。
        """

        return f"{symbol}.SS" if symbol.startswith(("5", "6", "9")) else f"{symbol}.SZ"
