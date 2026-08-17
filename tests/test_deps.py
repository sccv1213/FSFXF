"""launcher 缺依赖兜底相关测试:依赖声明单一来源 / check_deps 探测 / 兜底文件层。

对应 /code-review 结论(commit 5d0c41a):
- 兜底分支写 启动失败.txt 无保护,只读目录下 PermissionError 穿透 → pythonw 静默退出
  → 三层通知每层各自 try/except,test_launcher_fallback_file 钉住"任何一层失败都不再抛出"
- 弹窗只显示 e.name 丢弃 str(e)(DLL load failed 病因不可见)→ check_deps 保留真实异常
- pip 版本串 5 处拷贝无同步机制 → core/deps.py 单一来源,test_pip_hint_in_readme 钉住
"""
from __future__ import annotations

import builtins
import importlib.util
from importlib.machinery import SourceFileLoader
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import deps

ROOT = Path(__file__).resolve().parent.parent


class TestPipHintInDocs(unittest.TestCase):
    def test_pip_hint_in_readme(self):
        """README 的安装命令必须与代码版本声明一致(改版本必须同步文档)。"""
        pins = [f"{n}=={v}" for n, v in deps.REQUIRED]
        with open(ROOT / "README.md", encoding="utf-8") as f:
            content = f.read()
        for pin in pins:
            self.assertIn(pin, content)


class TestCheckDeps(unittest.TestCase):
    def test_check_deps_ok(self):
        """测试环境依赖齐全 → 空列表。"""
        self.assertEqual(deps.check_deps(), [])

    @staticmethod
    def _fake_import(fail_name: str, exc: Exception):
        real_import = importlib.import_module

        def fake(name, *a, **k):
            if name == fail_name:
                raise exc
            return real_import(name, *a, **k)

        return fake

    def test_check_deps_missing(self):
        with mock.patch("importlib.import_module",
                                 side_effect=self._fake_import(
                                     "av", ModuleNotFoundError("No module named 'av'"))):
            missing = deps.check_deps()
        self.assertEqual([n for n, _ in missing], ["av"])
        self.assertIn("No module named 'av'", str(missing[0][1]))

    def test_check_deps_broken_install(self):
        """已装但损坏(DLL load failed)同样被探测到,真实异常原样保留(弹窗可见病因)。"""
        err = ImportError("DLL load failed while importing av")
        with mock.patch("importlib.import_module",
                                 side_effect=self._fake_import("av", err)):
            missing = deps.check_deps()
        self.assertEqual([n for n, _ in missing], ["av"])
        self.assertIs(missing[0][1], err)


class TestLauncherFallbackFile(unittest.TestCase):
    """兜底文件层回归:tkinter 与 ctypes 都不可用时,写文件本身失败也不得再抛出。"""

    @classmethod
    def setUpClass(cls):
        path = ROOT / "launcher.pyw"
        loader = SourceFileLoader("launcher_under_test", str(path))
        spec = importlib.util.spec_from_loader("launcher_under_test", loader)
        cls.mod = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.mod)

    def setUp(self):
        self.orig_excepthook = sys.excepthook  # launcher 模块导入会替换 excepthook

    def tearDown(self):
        sys.excepthook = self.orig_excepthook

    def test_fallback_file_written_no_raise(self):
        """前两层弹窗都失败 → 写 启动失败.txt 成功落盘且不抛异常(旧版会穿透静默退出)。"""
        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name in ("tkinter", "ctypes"):
                raise ImportError(f"No module named '{name}'")
            return real_import(name, globals, locals, fromlist, level)

        with tempfile.TemporaryDirectory() as tmp:
            self.mod._FAIL_FILE = os.path.join(tmp, "启动失败.txt")
            with mock.patch("builtins.__import__", side_effect=fake_import):
                self.mod._show_deps_error([("av", ImportError("DLL load failed while importing av"))])
            with open(self.mod._FAIL_FILE, encoding="utf-8") as f:
                content = f.read()
        self.assertIn("DLL load failed", content)
        self.assertIn(deps.PIP_INSTALL_HINT, content)
