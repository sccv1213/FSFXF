"""分身斧修复工具 V2 入口。

launcher.pyw(根目录) → 本模块。excepthook 弹窗兜底,MainWindow 懒加载。
"""
from __future__ import annotations

import sys
import traceback


def _excepthook(exc_type, exc_value, tb) -> None:
    """未捕获异常:打印 + 弹窗展示错误尾部(不静默)。"""
    text = "".join(traceback.format_exception(exc_type, exc_value, tb))
    try:
        print(text, file=sys.stderr)  # pythonw 下标准流可能为 None,守卫后弹窗必然执行
    except Exception:
        pass
    try:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.critical(None, "程序错误",
                             f"发生未处理的错误:\n\n{text[-2000:]}",
                             QMessageBox.StandardButton.Ok)
    except Exception:
        pass


def main() -> int:
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    app.setApplicationName("分身斧修复工具")
    sys.excepthook = _excepthook
    from ui.main_window import MainWindow   # 懒加载(启动更快)
    win = MainWindow()
    win.show()
    return app.exec()
