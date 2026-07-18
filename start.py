"""独立网格回测项目的最小启动入口。"""

from grid_backtest.web_server import run_server


def main() -> None:
    """启动本机 HTTP 服务并持续处理网页和回测请求。

    Returns:
        无返回值；服务会持续运行，直到用户按 Ctrl+C 停止。
    """

    run_server()


if __name__ == "__main__":
    # 仅在直接执行 start.py 时启动，导入模块不会产生监听端口副作用。
    main()

