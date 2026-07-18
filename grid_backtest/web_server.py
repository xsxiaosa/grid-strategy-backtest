"""只使用 Python 标准库的本机网页与 JSON API 服务。"""

import json
import logging
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .optimization import OptimizationManager
from .runtime_paths import get_data_directory, get_web_directory
from .service import BacktestService
from .storage import JsonStorage


WEB_DIRECTORY = get_web_directory()
DATA_DIRECTORY = get_data_directory()


class GridBacktestRequestHandler(BaseHTTPRequestHandler):
    """处理静态网页、策略配置和回测报告 JSON 请求。"""

    service: BacktestService
    optimization_manager: OptimizationManager

    def do_GET(self) -> None:
        """处理首页、静态资源、配置和历史报告读取请求。

        Returns:
            无返回值；响应状态、头和正文直接写入客户端连接。
        """

        parsed_url = urlparse(self.path)
        path = parsed_url.path
        if path == "/api/config":
            self._write_json(HTTPStatus.OK, self.service.get_config())
            return
        if path == "/api/market-indicators":
            symbol = (parse_qs(parsed_url.query).get("symbol") or [""])[0]
            try:
                self._write_json(HTTPStatus.OK, self.service.get_market_indicators(symbol))
            except ValueError as error:
                self._write_error(HTTPStatus.BAD_REQUEST, "INVALID_SYMBOL", str(error))
            except RuntimeError as error:
                self._write_error(HTTPStatus.BAD_GATEWAY, "MARKET_INDICATORS_UNAVAILABLE", str(error))
            return
        if path == "/api/backtests":
            self._write_json(HTTPStatus.OK, {"data": self.service.list_reports()})
            return
        if path.startswith("/api/backtests/"):
            report_id = path.removeprefix("/api/backtests/")
            report = self.service.get_report(report_id)
            if report is None:
                self._write_error(HTTPStatus.NOT_FOUND, "REPORT_NOT_FOUND", "回测报告不存在")
            else:
                self._write_json(HTTPStatus.OK, report)
            return
        if path == "/api/optimizations":
            self._write_json(HTTPStatus.OK, {"data": self.optimization_manager.list_history()})
            return
        if path.startswith("/api/optimizations/"):
            job_id = path.removeprefix("/api/optimizations/")
            snapshot = self.optimization_manager.get(job_id)
            if snapshot is None:
                self._write_error(HTTPStatus.NOT_FOUND, "OPTIMIZATION_NOT_FOUND", "参数优化任务不存在")
            else:
                self._write_json(HTTPStatus.OK, snapshot)
            return
        self._serve_static(path)

    def do_POST(self) -> None:
        """处理创建一次新网格回测的 JSON 请求。

        Returns:
            无返回值；成功返回完整报告，失败返回结构化错误。
        """

        path = urlparse(self.path).path
        if path not in {"/api/backtests", "/api/optimizations"}:
            self._write_error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "接口不存在")
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > 1_000_000:
                raise ValueError("请求体大小必须在 1 字节至 1 MB 之间")
            raw_body = self.rfile.read(content_length)
            body = json.loads(raw_body.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("请求 JSON 根节点必须是对象")
            if path == "/api/optimizations":
                snapshot = self.optimization_manager.start(body)
                self._write_json(HTTPStatus.ACCEPTED, snapshot)
            else:
                report = self.service.run(body)
                self._write_json(HTTPStatus.OK, report)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            self._write_error(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", str(error))
        except RuntimeError as error:
            if path == "/api/optimizations":
                self._write_error(HTTPStatus.CONFLICT, "OPTIMIZATION_BUSY", str(error))
            else:
                self._write_error(HTTPStatus.BAD_GATEWAY, "MARKET_DATA_UNAVAILABLE", str(error))
        except OSError as error:
            self._write_error(HTTPStatus.INTERNAL_SERVER_ERROR, "JSON_STORAGE_ERROR", str(error))

    def do_DELETE(self) -> None:
        """处理运行中优化任务的取消请求。

        Returns:
            无返回值；成功时返回最新任务状态，不存在时返回结构化 404。
        """

        path = urlparse(self.path).path
        if not path.startswith("/api/optimizations/"):
            self._write_error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "接口不存在")
            return
        job_id = path.removeprefix("/api/optimizations/")
        snapshot = self.optimization_manager.cancel(job_id)
        if snapshot is None:
            self._write_error(HTTPStatus.NOT_FOUND, "OPTIMIZATION_NOT_FOUND", "参数优化任务不存在")
        else:
            self._write_json(HTTPStatus.OK, snapshot)

    def log_message(self, format_string: str, *args: Any) -> None:
        """以中文项目前缀输出访问日志，并过滤高频成功进度轮询。

        Args:
            format_string: BaseHTTPRequestHandler 提供的日志格式。
            args: 格式化日志所需参数。

        Returns:
            无返回值；普通访问日志写入标准错误流，成功的优化进度轮询不重复输出。
        """

        # 前端每 750 毫秒查询一次状态；成功轮询没有诊断价值，会淹没真正的后台计算进度。
        path = urlparse(self.path).path
        status_code = str(args[1]) if len(args) > 1 else ""
        if self.command == "GET" and path.startswith("/api/optimizations/") and status_code == "200":
            return
        super().log_message("[网格回测] " + format_string, *args)

    def _serve_static(self, path: str) -> None:
        """只允许访问白名单中的三个前端静态文件。

        Args:
            path: URL 中不含查询参数的请求路径。

        Returns:
            无返回值；文件内容或 404 错误直接写入响应。
        """

        static_files = {
            "/": "index.html",
            "/index.html": "index.html",
            "/app.js": "app.js",
            "/styles.css": "styles.css",
            "/optimizer.html": "optimizer.html",
            "/optimizer.js": "optimizer.js",
            "/compare.html": "compare.html",
            "/compare.js": "compare.js",
        }
        filename = static_files.get(path)
        if filename is None:
            self._write_error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "页面不存在")
            return
        file_path = WEB_DIRECTORY / filename
        content = file_path.read_bytes()
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _write_json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
        """输出 UTF-8 JSON 成功或错误响应。

        Args:
            status: HTTP 状态码。
            value: 需要序列化的 JSON 根对象。

        Returns:
            无返回值；编码后的响应直接写入客户端连接。
        """

        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _write_error(self, status: HTTPStatus, code: str, message: str) -> None:
        """输出字段稳定的结构化 JSON 错误。

        Args:
            status: HTTP 错误状态码。
            code: 供程序判断的稳定错误代码。
            message: 面向用户的中文错误说明。

        Returns:
            无返回值；错误对象直接写入客户端连接。
        """

        self._write_json(status, {"code": code, "message": message})


def create_server(host: str = "127.0.0.1", port: int = 8765, data_directory: Path | None = None) -> ThreadingHTTPServer:
    """创建已注入 JSON 存储和应用服务的线程化 HTTP 服务器。

    Args:
        host: 监听地址，默认只允许本机访问。
        port: 监听 TCP 端口。
        data_directory: 可选数据根目录，测试时用于隔离临时文件。

    Returns:
        尚未开始监听循环的 ThreadingHTTPServer 实例。
    """

    storage = JsonStorage(data_directory or DATA_DIRECTORY)
    handler_class = type(
        "ConfiguredGridBacktestRequestHandler",
        (GridBacktestRequestHandler,),
        {
            "service": BacktestService(storage),
            "optimization_manager": OptimizationManager(storage),
        },
    )
    return ThreadingHTTPServer((host, port), handler_class)


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    """启动独立回测服务并在 Ctrl+C 后安全关闭监听端口。

    Args:
        host: 监听地址，默认只允许本机访问。
        port: 浏览器和 API 使用的端口。

    Returns:
        无返回值；停止服务后关闭底层套接字。
    """

    # 后台优化运行在线程与进程池中，统一启用带时间和级别的控制台日志便于观察长任务进度。
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    server = create_server(host, port)
    print(f"网格策略回测已启动：http://{host}:{port}")
    print(f"JSON 数据目录：{DATA_DIRECTORY.resolve()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止网格策略回测服务……")
    finally:
        server.server_close()
