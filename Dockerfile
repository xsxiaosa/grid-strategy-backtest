# 使用与项目要求一致的 Python 3.11 轻量运行时，项目本身不需要安装第三方依赖。
FROM python:3.11-slim

# 禁止生成字节码并立即刷新日志，便于容器日志采集和故障排查。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GRID_BACKTEST_HOST=0.0.0.0 \
    GRID_BACKTEST_PORT=8765

WORKDIR /app

# 仅复制服务运行所需文件，避免把测试、运行数据和桌面打包产物带入镜像。
COPY grid_backtest ./grid_backtest
COPY web ./web
COPY start.py ./start.py
COPY pyproject.toml ./pyproject.toml

# 使用非 root 用户运行服务，并预先创建可写的数据目录。
RUN addgroup --system app \
    && adduser --system --ingroup app app \
    && mkdir -p /app/data \
    && chown -R app:app /app

USER app

EXPOSE 8765

# 通过标准库访问健康接口，避免为健康检查额外引入 curl 或 wget。
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/config', timeout=3)"]

CMD ["python", "start.py"]
