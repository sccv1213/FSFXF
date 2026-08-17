"""依赖声明单一来源:launcher 启动探测与 decoder 运行期提示共用,版本只改这一处。

check_deps 用真实 import 探测(而非 find_spec),同时覆盖两类失败:
未安装(ModuleNotFoundError)与已装但损坏(DLL load failed 等)。
"""
from __future__ import annotations

import importlib

REQUIRED = (("PySide6", "6.11.1"), ("av", "18.0.0"), ("numpy", "2.5.2"))
PIP_INSTALL_HINT = "pip install " + " ".join(f"{n}=={v}" for n, v in REQUIRED)


def check_deps() -> list[tuple[str, Exception]]:
    """逐个真实 import 必需依赖,返回 [(包名, 异常)];全部可用时返回 []。"""
    missing = []
    for name, _version in REQUIRED:
        try:
            importlib.import_module(name)
        except Exception as e:  # 未安装 / DLL load failed 等一律视为依赖失败
            missing.append((name, e))
    return missing
