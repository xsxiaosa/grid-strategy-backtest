"""JSON 存储与独立 HTTP 服务集成测试。"""

import json
import tempfile
import threading
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


class JsonStorageTests(unittest.TestCase):
    """验证配置、缓存和报告均使用普通 JSON 文件。"""

    def test_writes_and_reads_json_without_database(self) -> None:
        """所有数据类型应直接写入可读 JSON，且无需数据库文件。"""

        with tempfile.TemporaryDirectory() as directory:
            storage = JsonStorage(Path(directory))
            storage.write_config({"symbol": "588000"})
            storage.write_market("588000", 30, {"bars": [{"close": 1.74}, {"close": 1.75}]})
            storage.write_report("report-1", {"report_id": "report-1", "market_data": {"symbol": "588000"}, "grid": {"profit": 1}, "buy_and_hold": {"profit": 0}, "comparison": {"winner": "GRID"}})
            self.assertEqual("588000", storage.read_config()["symbol"])
            self.assertEqual(2, len(storage.read_market("588000", 30)["bars"]))
            self.assertEqual("report-1", storage.read_report("report-1")["report_id"])
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
                self.assertIn("网格策略回测", homepage)
                self.assertEqual("588000", config["symbol"])
                self.assertEqual(30, config["days"])
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
                self.assertTrue(list((Path(directory) / "reports").glob("*.json")))
                self.assertTrue((Path(directory) / "config.json").exists())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    # 允许直接执行单个测试文件，同时保持 unittest discover 兼容。
    unittest.main()
