"""方向键悬停分发测试(迁移自旧 TestArrowKeys 5 项):音量条 ±5 / 画面微调 / 秒跳。"""
import os
import shutil
import subprocess
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from core.project import Patch, Rect
from ui.frame_view import FrameView
from ui.main_window import MainWindow


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


@unittest.skipUnless(_has_ffmpeg(), "需要 ffmpeg")
class TestArrowKeys(unittest.TestCase):
    """方向键秒跳回归:任意焦点下 ←/→ 生效,FrameView 微调态不被打扰。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls._td = tempfile.mkdtemp()
        cls.video = os.path.join(cls._td, "sample.mp4")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-y", "-f", "lavfi", "-i",
             "testsrc2=size=960x540:rate=30:duration=3",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", cls.video],
            capture_output=True, check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._td, ignore_errors=True)

    def _wait(self, ms=1200):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def _open(self) -> MainWindow:
        win = MainWindow(settings_org="FenshenFuTest", settings_app="test")
        win.show()
        self._wait(300)
        win._load_video(self.video)
        self._wait(1500)
        return win

    def _close(self, win: MainWindow) -> None:
        win._worker.stop()
        win.close()

    def test_arrow_seek_from_button_focus(self):
        """按钮持焦时 ← 也触发秒跳(修复前焦点导航吞键,current_time 不变)。"""
        win = self._open()
        win._player.seek(2.0)
        self._wait(1200)
        self.assertAlmostEqual(win._player.current_time, 2.0, delta=0.2,
                               msg="前置 seek 未就位")

        win._btn_play.setFocus()
        self.app.processEvents()
        QTest.keyClick(win._btn_play, Qt.Key.Key_Left)
        self._wait(1200)
        self.assertAlmostEqual(win._player.current_time, 1.0, delta=0.3,
                               msg=f"按钮持焦时 ← 未秒跳:{win._player.current_time}")
        self._close(win)

    def test_frame_nudge_keeps_arrows(self):
        """FrameView 微调态(选中补丁)← 仍微调矩形,不触发秒跳(防误伤)。"""
        win = self._open()
        win._player.seek(2.0)
        self._wait(1200)
        before_t = win._player.current_time

        dst = Rect(0.4, 0.1, 0.12, 0.2)
        patch = Patch(dst=dst, src=Rect(0.066, 0.1, 0.12, 0.2), source_tile_idx=0)
        win._on_patch_created(patch)
        win._frame_view.set_mode(FrameView.MODE_SELECT)
        win._frame_view._select(patch.id)
        win._frame_view.setFocus()
        self.app.processEvents()
        before_nx = patch.dst.nx
        QTest.keyClick(win._frame_view, Qt.Key.Key_Left)
        self.app.processEvents()
        self.assertLess(patch.dst.nx, before_nx, "微调应左移矩形")
        self.assertAlmostEqual(win._player.current_time, before_t, delta=0.05,
                               msg="微调态不应触发秒跳")
        self._close(win)

    def test_volume_hover_arrows_adjust_volume(self):
        """悬停音量条(焦点在别处)← 调音量、不秒跳(主回归)。"""
        win = self._open()
        win._player.seek(2.0)
        self._wait(1200)
        win._volume_slider.setValue(50)            # 防 QSettings 残留音量污染
        win._state.set_hover("volume")             # 模拟鼠标悬停音量条
        vol_before = win._volume_slider.value()
        t_before = win._player.current_time
        win._btn_play.setFocus()
        self.app.processEvents()
        QTest.keyClick(win._btn_play, Qt.Key.Key_Left)
        self.app.processEvents()
        self.assertEqual(win._volume_slider.value(), vol_before - 5,
                         "悬停音量条时 ← 应调音量")
        self.assertAlmostEqual(win._player.current_time, t_before, delta=0.05,
                               msg="悬停音量条时不应秒跳")
        self._close(win)

    def test_frame_hover_nudge_without_focus(self):
        """悬停画面 + 选中补丁(焦点在按钮、未点击画面)← 微调矩形、不秒跳。"""
        win = self._open()
        win._player.seek(2.0)
        self._wait(1200)
        t_before = win._player.current_time

        dst = Rect(0.4, 0.1, 0.12, 0.2)
        patch = Patch(dst=dst, src=Rect(0.066, 0.1, 0.12, 0.2), source_tile_idx=0)
        win._on_patch_created(patch)
        win._frame_view.set_mode(FrameView.MODE_SELECT)
        win._frame_view._select(patch.id)
        win._state.set_hover("frame")              # 模拟鼠标悬停画面(未点击)
        before_nx = patch.dst.nx
        win._btn_play.setFocus()                   # 焦点在按钮——不点画面
        self.app.processEvents()
        QTest.keyClick(win._btn_play, Qt.Key.Key_Left)
        self.app.processEvents()
        self.assertLess(patch.dst.nx, before_nx, "悬停画面微调应左移矩形")
        self.assertAlmostEqual(win._player.current_time, t_before, delta=0.05,
                               msg="微调时不应秒跳")
        self._close(win)

    def test_frame_hover_up_down_nudge_without_focus(self):
        """悬停画面 + 选中补丁:↑/↓ 也微调矩形(与 ←/→ 同路径,焦点在按钮)。"""
        win = self._open()
        win._player.seek(2.0)
        self._wait(1200)
        t_before = win._player.current_time

        dst = Rect(0.4, 0.1, 0.12, 0.2)
        patch = Patch(dst=dst, src=Rect(0.066, 0.1, 0.12, 0.2), source_tile_idx=0)
        win._on_patch_created(patch)
        win._frame_view.set_mode(FrameView.MODE_SELECT)
        win._frame_view._select(patch.id)
        win._state.set_hover("frame")              # 模拟鼠标悬停画面(未点击)
        before_ny = patch.dst.ny
        win._btn_play.setFocus()                   # 焦点在按钮——不点画面
        self.app.processEvents()
        QTest.keyClick(win._btn_play, Qt.Key.Key_Up)
        self.app.processEvents()
        self.assertLess(patch.dst.ny, before_ny, "悬停画面 ↑ 应上移矩形")
        before_ny = patch.dst.ny
        QTest.keyClick(win._btn_play, Qt.Key.Key_Down)
        self.app.processEvents()
        self.assertGreater(patch.dst.ny, before_ny, "悬停画面 ↓ 应下移矩形")
        self.assertAlmostEqual(win._player.current_time, t_before, delta=0.05,
                               msg="微调时不应触发其他动作")
        self._close(win)


if __name__ == "__main__":
    unittest.main()
