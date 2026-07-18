"""验证源码和 PyInstaller 目录包的运行路径选择。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grid_backtest.runtime_paths import get_application_directory, get_data_directory, get_resource_directory


class RuntimePathTests(unittest.TestCase):
    """验证打包运行时资源和数据目录不会互相混淆。"""

    def test_source_runtime_uses_project_directory(self) -> None:
        """源码运行时应从当前项目根目录读取资源和保存数据。"""

        project_directory = Path(__file__).resolve().parent.parent
        self.assertEqual(get_resource_directory(), project_directory)
        self.assertEqual(get_application_directory(), project_directory)
        self.assertEqual(get_data_directory(), project_directory / "data")

    def test_frozen_runtime_shares_data_between_entrypoints(self) -> None:
        """两个目录包入口应共享压缩包根目录下的 data 目录。"""

        with tempfile.TemporaryDirectory() as directory:
            package_directory = Path(directory) / "grid-strategy-backtest-windows-x64"
            executable = package_directory / "grid-backtest" / "grid-backtest.exe"
            resource_directory = executable.parent / "_internal"
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(executable)),
                patch.object(sys, "_MEIPASS", str(resource_directory), create=True),
            ):
                self.assertEqual(get_resource_directory(), resource_directory.resolve())
                self.assertEqual(get_application_directory(), package_directory.resolve())
                self.assertEqual(get_data_directory(), package_directory.resolve() / "data")


if __name__ == "__main__":
    unittest.main()
