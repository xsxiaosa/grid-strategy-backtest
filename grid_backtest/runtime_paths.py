"""提供源码运行和 PyInstaller 打包运行共用的路径解析。"""

from __future__ import annotations

import sys
from pathlib import Path


def get_resource_directory() -> Path:
    """返回只读资源目录，兼容源码运行和 PyInstaller 目录包运行。

    Returns:
        源码运行时的项目根目录，或 PyInstaller 解包后的资源根目录。
    """

    frozen_directory = getattr(sys, "_MEIPASS", None)
    if frozen_directory:
        return Path(frozen_directory).resolve()
    return Path(__file__).resolve().parent.parent


def get_application_directory() -> Path:
    """返回应用程序目录，用于保存打包程序的运行期数据。

    Returns:
        打包运行时两个入口程序共同的目录，源码运行时的项目根目录。
    """

    if getattr(sys, "frozen", False):
        # PyInstaller 目录包将网页服务和优化器分别放在根目录下的子目录中，
        # 向上一级后才能让两个入口共享同一套 data 运行数据。
        return Path(sys.executable).resolve().parent.parent
    return Path(__file__).resolve().parent.parent


def get_web_directory() -> Path:
    """返回网页静态资源目录。

    Returns:
        包含首页、脚本和样式文件的 ``web`` 目录路径。
    """

    return get_resource_directory() / "web"


def get_data_directory() -> Path:
    """返回独立的运行期数据目录。

    Returns:
        程序旁的 ``data`` 目录，配置、行情缓存和回测结果均保存于此。
    """

    return get_application_directory() / "data"
