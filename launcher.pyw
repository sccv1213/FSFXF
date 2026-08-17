"""双击启动分身斧修复工具(V2,无黑窗)。缺依赖时弹窗提示,不静默退出。

失败提示三层递进,每层各自 try/except 兜底,任何一层失败都不会让异常
穿透到 pythonw 静默退出:tkinter 弹窗 → ctypes 原生 MessageBoxW → 写 启动失败.txt。
"""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.deps import PIP_INSTALL_HINT, check_deps  # noqa: E402

_FAIL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "启动失败.txt")


def _notify_launch_failure(title: str, detail: str) -> None:
    """启动失败的用户可见提示:stderr + 三层弹窗,任何一层失败都不再抛出。"""
    try:
        print(f"{title}\n{detail}", file=sys.stderr)
    except Exception:  # pythonw 下标准流可能为 None,print 会先崩
        pass
    text = f"{detail}\n\n请按 README 安装依赖:\n{PIP_INSTALL_HINT}"
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, text)
        return
    except Exception:
        pass
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # MB_ICONERROR
        return
    except Exception:
        pass
    try:
        with open(_FAIL_FILE, "w", encoding="utf-8") as f:
            f.write(f"{title}\n{detail}\n请按 README 安装:\n{PIP_INSTALL_HINT}\n")
    except Exception:
        pass  # 已尽力;再抛只会让 pythonw 静默退出


def _show_deps_error(missing: list[tuple[str, Exception]]) -> None:
    """缺依赖提示:逐行包名 + 真实错误(DLL 名等),含安装命令。"""
    detail = "缺少 Python 包:\n" + "\n".join(f"- {name}: {e}" for name, e in missing)
    _notify_launch_failure("分身斧修复工具 - 启动失败", detail)


def _excepthook(exc_type, exc_value, tb) -> None:
    """launcher 阶段的兜底:main.py 安装自己的 excepthook 前的一切异常都可见。"""
    _notify_launch_failure("分身斧修复工具 - 启动失败",
                           "".join(traceback.format_exception(exc_type, exc_value, tb))[-2000:])


sys.excepthook = _excepthook

if __name__ == "__main__":
    missing = check_deps()  # PySide6 + av 都在启动探测,README 承诺的"缺依赖启动弹窗"兑现
    if missing:
        _show_deps_error(missing)
        raise SystemExit(1)
    try:
        from main import main
    except Exception:  # 不限于 ImportError:SyntaxError/文件损坏也要提示
        _notify_launch_failure("分身斧修复工具 - 启动失败", "".join(traceback.format_exc()))
        raise SystemExit(1)
    try:
        os.remove(_FAIL_FILE)  # 启动检查通过,清理上次失败残留
    except OSError:
        pass
    try:
        sys.exit(main())
    except Exception:  # main() 内懒加载/构造失败(缺 Qt 平台插件等)都可见
        _notify_launch_failure("分身斧修复工具 - 启动失败", "".join(traceback.format_exc()))
        raise SystemExit(1)
