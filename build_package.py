"""使用 PyInstaller 构建当前项目的跨平台目录包和压缩包。"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
PYINSTALLER_VERSION = "6.21.0"


def parse_arguments() -> argparse.Namespace:
    """解析构建脚本的可选输出目录参数。

    Returns:
        包含构建输出目录的命令行参数对象。
    """

    parser = argparse.ArgumentParser(description="构建网格策略回测的 PyInstaller 目录包")
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "dist-package",
        help="构建产物输出目录，默认是项目下的 dist-package",
    )
    return parser.parse_args()


def get_platform_name() -> str:
    """将当前操作系统名称转换为稳定的产物名称。

    Returns:
        ``windows``、``linux`` 或 ``macos``。

    Raises:
        RuntimeError: 当前操作系统不在支持范围内时抛出。
    """

    names = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}
    try:
        return names[platform.system()]
    except KeyError as error:
        raise RuntimeError(f"不支持的构建操作系统：{platform.system()}") from error


def get_architecture_name() -> str:
    """将当前处理器架构转换为稳定的产物名称。

    Returns:
        ``x64`` 或 ``arm64``。

    Raises:
        RuntimeError: 当前处理器架构不在支持范围内时抛出。
    """

    architecture = platform.machine().lower()
    if architecture in {"amd64", "x86_64", "x64"}:
        return "x64"
    if architecture in {"arm64", "aarch64"}:
        return "arm64"
    raise RuntimeError(f"不支持的构建处理器架构：{platform.machine()}")


def run_pyinstaller(entrypoint: str, name: str, package_directory: Path, work_directory: Path) -> None:
    """为一个 Python 入口构建 PyInstaller 目录包。

    Args:
        entrypoint: 相对于项目根目录的 Python 入口文件。
        name: 生成的可执行程序目录和文件名。
        package_directory: PyInstaller 的目录包输出目录。
        work_directory: PyInstaller 的临时工作目录。

    Returns:
        无返回值；构建失败时由子进程异常终止脚本。
    """

    import PyInstaller.__main__

    PyInstaller.__main__.run(
        [
            str(PROJECT_ROOT / entrypoint),
            "--clean",
            "--noconfirm",
            "--onedir",
            "--name",
            name,
            "--distpath",
            str(package_directory),
            "--workpath",
            str(work_directory / name),
            "--specpath",
            str(work_directory),
            "--paths",
            str(PROJECT_ROOT),
            "--add-data",
            f"{PROJECT_ROOT / 'web'}{os.pathsep}web",
        ]
    )


def create_runtime_directories(package_directory: Path) -> None:
    """创建独立运行数据目录，并写入使用说明文件。

    Args:
        package_directory: 当前平台目录包根目录。

    Returns:
        无返回值；目录和说明文件直接写入构建产物。
    """

    data_directory = package_directory / "data"
    for child in ("market", "reports", "optimizations", "optimization"):
        (data_directory / child).mkdir(parents=True, exist_ok=True)
    (package_directory / "README.txt").write_text(
        "网格策略回测\n\n"
        "启动网页服务：\n"
        f"  {('grid-backtest.exe' if get_platform_name() == 'windows' else './grid-backtest')}\n\n"
        "命令行优化器：\n"
        f"  {('grid-optimizer.exe' if get_platform_name() == 'windows' else './grid-optimizer')} --help\n\n"
        "运行数据保存在本目录的 data 文件夹中。\n",
        encoding="utf-8",
    )


def smoke_test(package_directory: Path) -> None:
    """启动两个打包入口并验证基本可用性。

    Args:
        package_directory: 当前平台目录包根目录。

    Returns:
        无返回值；验证失败时抛出 RuntimeError。
    """

    executable_suffix = ".exe" if get_platform_name() == "windows" else ""
    server_executable = package_directory / "grid-backtest" / f"grid-backtest{executable_suffix}"
    optimizer_executable = package_directory / "grid-optimizer" / f"grid-optimizer{executable_suffix}"
    subprocess.run([str(optimizer_executable), "--help"], cwd=package_directory, check=True, capture_output=True, text=True)

    process = subprocess.Popen([str(server_executable)], cwd=package_directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen("http://127.0.0.1:8765/api/config", timeout=1) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.25)
        raise RuntimeError("打包后的网页服务未能在 20 秒内响应 /api/config")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def create_archive(package_directory: Path, output_directory: Path) -> Path:
    """将当前平台目录包压缩为 zip 或 tar.gz 文件。

    Args:
        package_directory: 当前平台目录包根目录。
        output_directory: 压缩包输出目录。

    Returns:
        生成的压缩包绝对路径。
    """

    archive_format = "zip" if get_platform_name() == "windows" else "gztar"
    archive_base = output_directory / package_directory.name
    archive_path = Path(
        shutil.make_archive(
            str(archive_base),
            archive_format,
            root_dir=package_directory.parent,
            base_dir=package_directory.name,
        )
    )
    return archive_path.resolve()


def build_package(output_directory: Path) -> Path:
    """构建当前平台的两个入口、运行目录和压缩包。

    Args:
        output_directory: 构建产物输出目录。

    Returns:
        生成的压缩包绝对路径。
    """

    output_directory = output_directory.resolve()
    staging_directory = output_directory / "staging"
    work_directory = output_directory / "build"
    package_name = f"grid-strategy-backtest-{get_platform_name()}-{get_architecture_name()}"
    package_directory = staging_directory / package_name
    if staging_directory.exists():
        shutil.rmtree(staging_directory)
    if work_directory.exists():
        shutil.rmtree(work_directory)
    package_directory.mkdir(parents=True, exist_ok=True)
    run_pyinstaller("start.py", "grid-backtest", package_directory, work_directory)
    run_pyinstaller("optimize_strategy.py", "grid-optimizer", package_directory, work_directory)
    create_runtime_directories(package_directory)
    smoke_test(package_directory)
    archive_path = create_archive(package_directory, output_directory)
    print(f"构建完成：{archive_path}")
    return archive_path


def main() -> int:
    """执行参数解析和当前平台打包流程。

    Returns:
        成功时返回 0。
    """

    arguments = parse_arguments()
    build_package(arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
