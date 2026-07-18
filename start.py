"""独立网格回测项目的最小启动入口。"""

from grid_backtest.runtime_paths import configure_output_encoding
from grid_backtest.web_server import run_server


def main() -> int:
    """启动本机 HTTP 服务并持续处理网页和回测请求。

    Returns:
        服务退出码；正常停止返回 0，端口被占用时返回 1。
    """

    configure_output_encoding()
    return run_server()


if __name__ == "__main__":
    # 仅在直接执行 start.py 时启动，导入模块不会产生监听端口副作用。
    raise SystemExit(main())
