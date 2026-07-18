"""JSON 存储与独立 HTTP 服务集成测试。"""

import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from grid_backtest.storage import JsonStorage
from grid_backtest.web_server import create_server


class FixedMarketProvider:
    """为 HTTP 集成测试提供不访问网络的固定分钟行情。"""

    def fetch_recent(self, config) -> tuple[dict, list[str]]:
        """返回能够产生一次卖出信号的固定行情。

        Args:
            config: HTTP 层解析后的策略配置。

        Returns:
            固定行情对象和空警告列表。
        """

        bars = []
        for index, close in enumerate((1.74, 1.79, 1.78)):
            bars.append({"timestamp": f"2026-07-01T09:{30 + index:02d}:00+08:00", "open": close, "high": close + 0.002, "low": close - 0.002, "close": close, "volume": 1000})
        return {"symbol": config.symbol, "name": "测试 ETF", "source": "TEST", "timezone": "Asia/Shanghai", "fetched_at": "2026-07-01T01:30:00+00:00", "bars": bars}, []

    def fetch_daily_indicators(self, symbol: str) -> dict:
        """返回无需联网的固定日线均线摘要。

        Args:
            symbol: HTTP 查询提供的六位证券代码。

        Returns:
            字段与正式指标接口一致的固定 JSON 对象。
        """

        return {
            "symbol": symbol,
            "name": "测试 ETF",
            "as_of": "2026-07-17",
            "latest_price": 1.8,
            "change_percent": 1.2,
            "ma5": 1.75,
            "ma20": 1.7,
            "ma60": 1.65,
            "ema60": 1.66,
            "ma60_deviation_percent": 9.09,
            "source": "TEST",
        }


class JsonStorageTests(unittest.TestCase):
    """验证配置、缓存和报告均使用普通 JSON 文件。"""

    def test_writes_and_reads_json_without_database(self) -> None:
        """所有数据类型应直接写入可读 JSON，且无需数据库文件。"""

        with tempfile.TemporaryDirectory() as directory:
            storage = JsonStorage(Path(directory))
            storage.write_config({"symbol": "588000"})
            storage.write_market("588000", 30, {"bars": [{"close": 1.74}, {"close": 1.75}]})
            storage.write_report("report-1", {"report_id": "report-1", "market_data": {"symbol": "588000"}, "grid": {"profit": 1}, "buy_and_hold": {"profit": 0}, "comparison": {"winner": "GRID"}})
            storage.write_optimization("opt-1", {"job_id": "opt-1", "created_at": "2026-07-18T00:00:00+00:00", "status": "completed", "completed": 100, "config": {"symbol": "588000"}, "best": [{"return_percent": 3.25}]})
            self.assertEqual("588000", storage.read_config()["symbol"])
            self.assertEqual(2, len(storage.read_market("588000", 30)["bars"]))
            self.assertEqual("report-1", storage.read_report("report-1")["report_id"])
            optimization_summary = storage.list_optimization_summaries()[0]
            self.assertEqual("opt-1", optimization_summary["job_id"])
            self.assertEqual(3.25, optimization_summary["best_return_percent"])
            self.assertFalse(any(Path(directory).rglob("*.sqlite")))


class HttpServerTests(unittest.TestCase):
    """验证独立服务无需投资组合主项目即可提供网页和配置 API。"""

    def test_serves_homepage_and_default_config(self) -> None:
        """临时端口应返回中文首页和 588000 默认 JSON 配置。"""

        with tempfile.TemporaryDirectory() as directory:
            server = create_server(port=0, data_directory=Path(directory))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(base_url + "/", timeout=5) as response:
                    homepage = response.read().decode("utf-8")
                with urllib.request.urlopen(base_url + "/api/config", timeout=5) as response:
                    config = json.load(response)
                with urllib.request.urlopen(base_url + "/optimizer.html", timeout=5) as response:
                    optimizer_page = response.read().decode("utf-8")
                with urllib.request.urlopen(base_url + "/compare.html", timeout=5) as response:
                    compare_page = response.read().decode("utf-8")
                self.assertIn("网格策略回测", homepage)
                self.assertIn('href="/optimizer.html" target="_blank" rel="noopener"', homepage)
                self.assertIn('href="/compare.html"', homepage)
                self.assertIn('id="copy-to-optimizer-button"', homepage)
                self.assertIn("复制方案到参数优化", homepage)
                self.assertIn('name="minimum_position_percent"', homepage)
                self.assertIn("四参数迭代优化", optimizer_page)
                self.assertIn('data-config-field="minimum_position_percent"', optimizer_page)
                self.assertIn('<details class="panel history-panel optimizer-history-panel"', optimizer_page)
                self.assertIn('<summary class="optimizer-history-summary">', optimizer_page)
                self.assertIn("归一化 K 线叠加", compare_page)
                self.assertIn('id="report-a-select"', compare_page)
                self.assertIn('id="report-b-select"', compare_page)
                self.assertIn('id="comparison-zoom-in"', compare_page)
                self.assertIn('data-zoom-start="0.5"', compare_page)
                self.assertIn('id="comparison-zoom-reset"', compare_page)
                self.assertEqual("588000", config["symbol"])
                self.assertEqual(30, config["days"])
                self.assertEqual(20.0, config["minimum_position_percent"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_gets_market_indicators_for_valid_symbol(self) -> None:
        """六位证券代码应通过 GET 接口返回 MA60 和 EMA60 等摘要。"""

        with tempfile.TemporaryDirectory() as directory:
            server = create_server(port=0, data_directory=Path(directory))
            server.RequestHandlerClass.service.market_provider = FixedMarketProvider()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_address[1]}/api/market-indicators?symbol=588000"
                with urllib.request.urlopen(url, timeout=5) as response:
                    indicators = json.load(response)
                self.assertEqual("588000", indicators["symbol"])
                self.assertEqual(1.65, indicators["ma60"])
                self.assertEqual(1.66, indicators["ema60"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_runs_small_optimization_and_persists_json(self) -> None:
        """单值范围优化应通过后台 API 完成并写入 optimizations JSON。"""

        with tempfile.TemporaryDirectory() as directory:
            data_directory = Path(directory)
            server = create_server(port=0, data_directory=data_directory)
            server.RequestHandlerClass.optimization_manager.market_provider = FixedMarketProvider()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                parameter_names = (
                    "rise_trigger_percent",
                    "sell_pullback_percent",
                    "fall_trigger_percent",
                    "buy_rebound_percent",
                )
                payload = {
                    "config": {},
                    "ranges": {name: {"minimum": 1, "maximum": 1} for name in parameter_names},
                    "coarse_value_limit": 25,
                }
                request = urllib.request.Request(
                    base_url + "/api/optimizations",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    created = json.load(response)
                deadline = time.monotonic() + 15
                snapshot = created
                while snapshot["status"] in {"queued", "running"} and time.monotonic() < deadline:
                    time.sleep(0.1)
                    with urllib.request.urlopen(base_url + f"/api/optimizations/{created['job_id']}", timeout=5) as response:
                        snapshot = json.load(response)
                self.assertEqual("completed", snapshot["status"], snapshot.get("error"))
                self.assertEqual(25, snapshot["coarse_value_limit"])
                self.assertEqual(1, snapshot["completed"])
                self.assertEqual(1, len(snapshot["best"]))
                self.assertTrue(list((data_directory / "optimizations").glob("*.json")))
                with urllib.request.urlopen(base_url + "/api/optimizations", timeout=5) as response:
                    history = json.load(response)
                self.assertEqual(created["job_id"], history["data"][0]["job_id"])
                self.assertEqual("588000", history["data"][0]["symbol"])
                self.assertEqual(snapshot["best"][0]["return_percent"], history["data"][0]["best_return_percent"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_posts_backtest_and_persists_json_report(self) -> None:
        """POST 回测应返回结果，并在临时 data 目录生成报告 JSON。"""

        with tempfile.TemporaryDirectory() as directory:
            server = create_server(port=0, data_directory=Path(directory))
            server.RequestHandlerClass.service.market_provider = FixedMarketProvider()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_address[1]}/api/backtests"
                request = urllib.request.Request(url, data=json.dumps({}).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(request, timeout=5) as response:
                    report = json.load(response)
                self.assertEqual("588000", report["market_data"]["symbol"])
                self.assertIn("comparison", report)
                self.assertIn("final_position_percent", report["grid"])
                self.assertIn("average_position_percent", report["grid"])
                self.assertTrue(list((Path(directory) / "reports").glob("*.json")))
                self.assertTrue((Path(directory) / "config.json").exists())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    # 允许直接执行单个测试文件，同时保持 unittest discover 兼容。
    unittest.main()
