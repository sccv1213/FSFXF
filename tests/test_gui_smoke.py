"""GUI 冒烟测试(offscreen,迁移自旧版 test_gui_smoke.py 组件级部分)。

方向键悬停分发 → test_key_router.py;音频缓冲 flush → test_audio_output.py。

接口变化(旧 → 新):
- fv.set_current_segment/set_current_grid → fv.set_scope_context(scope)
- fv._nudge_grid_edge → fv._interactor.nudge_grid_edge(白盒)
- worker._pts_hist → worker._planner.hist_pts;_pending_step → _planner.pending
"""
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from core.grid import GridLayout
from core.planning import Scope
from core.project import Patch, Project, Rect, Segment
from services.decoder import DecodeWorker
from services.player_controller import PlayerController
from state.app_state import AppState
from ui.check_combo import CheckCombo
from ui.frame_view import DragOp, DragState, FrameView
from ui.patch_panel import PatchPanel
from ui.timeline_widget import TimelineWidget


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _make_sample(td: str) -> str:
    path = os.path.join(td, "sample.mp4")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-y", "-f", "lavfi", "-i",
         "testsrc2=size=640x360:rate=30:duration=3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", path],
        capture_output=True, check=True)
    return path


def make_scope(project: Project, seg: Segment | None,
               start: float = 0.0, end: float = 1.0) -> Scope:
    """按段构造 scope(测试用;与 effective_scopes 语义一致)。"""
    g = seg.grid if seg is not None and seg.grid is not None else project.grid
    gk = f"segment:{seg.id}" if seg is not None and seg.grid is not None else "project"
    ids = tuple(seg.patch_ids) if seg is not None else tuple(p.id for p in project.patches)
    cids = (tuple(seg.copy_rule_ids) if seg is not None
            else tuple(r.id for r in project.copy_rules))
    key = f"segment:{seg.id}" if seg is not None else "global"
    s, e = (seg.start, seg.end) if seg is not None else (start, end)
    return Scope(key, s, e, g, gk, ids, cids)


def _make_patch_panel() -> tuple:
    """补丁面板 + 单补丁(source=格1, dst=格2, lock=默认)的测试夹具。"""
    panel = PatchPanel()
    pr = Project("D:\\x.mp4", 100)
    pr.grid = GridLayout.thirds()
    p = Patch(dst=Rect(0.45, 0.1, 0.1, 0.2), src=Rect(0.1, 0.1, 0.1, 0.2),
              source_tile_idx=0, dst_tile_idx=1)
    pr.patches = [p]
    panel.set_project(pr)
    panel.set_scope_context(make_scope(pr, None, 0, 100))
    return panel, p


def _click_combo_item(combo, row: int) -> None:
    """真实路径点击菜单项(viewport 中心):勾选切换且弹出保持。"""
    combo.showPopup()
    QTest.qWait(50)
    view = combo.view()
    r = view.visualRect(combo.model().index(row, 0))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=r.center())
    QTest.qWait(50)


@unittest.skipUnless(_has_ffmpeg(), "需要 ffmpeg")
class TestDecoderSmoke(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls._td = tempfile.mkdtemp()
        cls.video = _make_sample(cls._td)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._td, ignore_errors=True)

    def _run_until(self, predicate, timeout_ms=8000):
        loop = QEventLoop()

        class _Hook:
            def check(self, *args):
                if predicate(*args):
                    loop.quit()

        return _Hook(), loop

    def test_open_seek_frame(self):
        worker = DecodeWorker()
        frames = []
        seeked = []
        errors = []

        worker.frameReady.connect(lambda img, pts: frames.append((img, pts)))
        worker.seekDone.connect(lambda t: seeked.append(t))
        worker.errorOccurred.connect(errors.append)

        worker.open(self.video)
        worker.seek(1.5)

        hook, loop = self._run_until(lambda *a: len(frames) >= 1 and len(seeked) >= 1)
        worker.frameReady.connect(hook.check)
        worker.seekDone.connect(hook.check)

        worker.start()
        QTimer.singleShot(8000, loop.quit)
        loop.exec()
        worker.stop()

        self.assertEqual(errors, [], f"解码错误:{errors}")
        self.assertGreaterEqual(len(frames), 1, "未收到帧")
        self.assertGreaterEqual(len(seeked), 1, "未收到 seek 完成")
        img, pts = frames[-1]
        self.assertFalse(img.isNull())
        self.assertEqual(img.width(), 640)
        self.assertAlmostEqual(pts, 1.5, delta=1 / 30 + 0.02, msg=f"seek 帧偏差:{pts}")

    def test_step_forward(self):
        worker = DecodeWorker()
        pts_list = []

        worker.open(self.video)
        worker.seek(0.0)

        hook, loop = self._run_until(lambda *a: len(pts_list) >= 2)
        worker.seekDone.connect(lambda t: pts_list.append(t))
        worker.seekDone.connect(hook.check)

        worker.start()
        QTimer.singleShot(2000, lambda: worker.step(1))
        QTimer.singleShot(6000, loop.quit)
        loop.exec()
        worker.stop()

        self.assertGreaterEqual(len(pts_list), 2, "逐帧步进未产生新帧")
        self.assertAlmostEqual(pts_list[-1] - pts_list[-2], 1 / 30, delta=0.02)

    def test_step_backward_and_accumulate(self):
        """暂停态后退:连点两次上一帧 → 精确倒退 2 帧。"""
        worker = DecodeWorker()
        pts_list = []

        worker.open(self.video)
        worker.seek(2.0)

        hook, loop = self._run_until(lambda *a: len(pts_list) >= 2)
        worker.seekDone.connect(lambda t: pts_list.append(t))
        worker.seekDone.connect(hook.check)

        worker.start()
        QTimer.singleShot(2000, lambda: (worker.step(-1), worker.step(-1)))
        QTimer.singleShot(8000, loop.quit)
        loop.exec()
        worker.stop()

        self.assertGreaterEqual(len(pts_list), 2, f"步进未生效:{pts_list}")
        self.assertAlmostEqual(pts_list[-1], 2.0 - 2 / 30, delta=0.03,
                               msg=f"连点两次应倒退 2 帧:{pts_list}")

    def test_step_backward_short_history_no_crash(self):
        """历史不足时后退步进 → 估算兜底而非越界崩溃(线程静默死亡回归)。"""
        from types import SimpleNamespace

        worker = DecodeWorker()
        worker._stream = SimpleNamespace(time_base=1 / 30)
        worker._last_emit_pts = 10.0
        worker._planner.hist_pts = [100, 200]      # seek 刚完成、历史最短的情况
        worker._waiting_seek_frame = False
        worker._seek_target = None
        worker._do_step(-2)                  # 曾 IndexError
        self.assertIsNotNone(worker._seek_target, "后退 2 帧应产生定位目标")
        worker._seek_target = None
        worker._planner.hist_pts = [100, 200, 300]
        worker._do_step(-3)                  # 历史 3 条退 3 帧:同样走估算兜底
        self.assertIsNotNone(worker._seek_target)
        self.assertGreaterEqual(worker._seek_target, 0.0)

    def test_step_exact_60fps(self):
        """60fps 视频:步进 ±1 帧精确(浮点往返导致的 ±1 帧偏差回归)。"""
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        video = os.path.join(td, "v60.mp4")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-y", "-f", "lavfi", "-i",
             "testsrc2=size=320x180:rate=60:duration=2",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", video],
            capture_output=True, check=True)

        worker = DecodeWorker()
        pts_list = []
        worker.open(video)
        worker.seek(1.0)
        hook, loop = self._run_until(lambda *a: len(pts_list) >= 3)
        worker.seekDone.connect(lambda t: pts_list.append(t))
        worker.seekDone.connect(hook.check)
        worker.start()
        QTimer.singleShot(2000, lambda: worker.step(1))
        QTimer.singleShot(2500, lambda: worker.step(-1))
        QTimer.singleShot(6000, loop.quit)
        loop.exec()
        worker.stop()
        self.assertGreaterEqual(len(pts_list), 3, f"步进未生效:{pts_list}")
        self.assertAlmostEqual(pts_list[1] - pts_list[0], 1 / 60, delta=0.001,
                               msg=f"前进应精确 1 帧:{pts_list}")
        self.assertAlmostEqual(pts_list[2] - pts_list[1], -1 / 60, delta=0.001,
                               msg=f"后退应精确 1 帧:{pts_list}")

    def test_step_vfr_exact(self):
        """VFR 视频:后退 = 真实上一帧,前进 = 实测间隔回到原帧。"""
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        video = os.path.join(td, "vfr.mp4")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-y", "-f", "lavfi", "-i",
             "testsrc2=size=320x180:rate=30:duration=2",
             "-vf", "select='not(mod(n,3))'", "-fps_mode", "vfr",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", video],
            capture_output=True, check=True)

        worker = DecodeWorker()
        pts_list = []
        worker.open(video)
        worker.seek(1.0)
        hook, loop = self._run_until(lambda *a: len(pts_list) >= 3)
        worker.seekDone.connect(lambda t: pts_list.append(t))
        worker.seekDone.connect(hook.check)
        worker.start()
        QTimer.singleShot(2000, lambda: worker.step(-1))
        QTimer.singleShot(2500, lambda: worker.step(1))
        QTimer.singleShot(6000, loop.quit)
        loop.exec()
        worker.stop()
        self.assertGreaterEqual(len(pts_list), 3, f"步进未生效:{pts_list}")
        self.assertLess(pts_list[1], pts_list[0], "后退应到真实上一帧")
        self.assertAlmostEqual(pts_list[2], pts_list[0], delta=0.001,
                               msg=f"前进应按实测间隔回到原帧:{pts_list}")

    def test_step_pauses_playback(self):
        """播放中点击步进 → 自动暂停。"""
        worker = DecodeWorker()
        pts_list = []

        worker.open(self.video)
        worker.seek(2.0)
        hook, loop = self._run_until(lambda *a: len(pts_list) >= 1)
        worker.seekDone.connect(lambda t: pts_list.append(t))
        worker.seekDone.connect(hook.check)
        worker.start()
        QTimer.singleShot(2000, worker.play)
        QTimer.singleShot(2500, lambda: worker.step(-1))
        QTimer.singleShot(6000, loop.quit)
        loop.exec()
        worker.stop()
        self.assertFalse(worker._playing, "步进后应自动暂停")

    def test_pause_step_play_recovers_quickly(self):
        """暂停 → 连点步进 → 播放:必须立即续播(时钟虚增冻结回归)。"""
        worker = DecodeWorker()
        frames = []
        before_play = []
        t_play = []
        t_done = []

        worker.frameReady.connect(lambda img, pts: frames.append(pts))
        worker.open(self.video)
        worker.seek(0.0)

        hook1, loop1 = self._run_until(lambda *a: len(frames) >= 1)
        worker.frameReady.connect(hook1.check)
        worker.start()
        QTimer.singleShot(8000, loop1.quit)
        loop1.exec()
        self.assertTrue(frames, "首帧未到达")

        def _resumed(*a):
            if before_play and len(frames) >= before_play[0] + 3:
                t_done.append(time.perf_counter())
                return True
            return False

        hook2, loop2 = self._run_until(_resumed)
        worker.frameReady.connect(hook2.check)
        QTimer.singleShot(500, worker.pause)
        QTimer.singleShot(2500, lambda: worker.step(1))
        QTimer.singleShot(2600, lambda: worker.step(1))
        QTimer.singleShot(3500, lambda: (worker.play(),
                                         before_play.append(len(frames)),
                                         t_play.append(time.perf_counter())))
        QTimer.singleShot(8000, loop2.quit)
        loop2.exec()
        worker.stop()

        self.assertTrue(before_play and t_play, "播放应已发起")
        self.assertTrue(t_done, f"播放后未续播:{frames}")
        self.assertLess(t_done[0] - t_play[0], 1.0,
                        f"续播过慢(冻结≈暂停时长):{t_done[0] - t_play[0]:.2f}s")

    def test_step_during_seek_merges(self):
        """定位途中连点:合并为一次后续定位,不反复重启 GOP 重解(防连点卡顿)。"""
        worker = DecodeWorker()
        pts_list = []
        seek_calls = []
        orig_seek = worker._do_seek
        worker._do_seek = lambda t: (seek_calls.append(1), orig_seek(t))[1]

        worker.open(self.video)
        worker.seek(0.0)

        hook, loop = self._run_until(lambda *a: len(pts_list) >= 3)
        worker.seekDone.connect(lambda t: pts_list.append(t))
        worker.seekDone.connect(hook.check)

        merged = []

        def _drive():
            worker._do_step(1)                   # 正常路径:写 _seek_target
            worker._waiting_seek_frame = True    # 模拟定位途中
            worker._do_step(1)                   # 应合并:只累计不覆盖 _seek_target
            merged.append(worker._planner.pending is not None)
            worker._waiting_seek_frame = False

        worker.start()
        QTimer.singleShot(1500, _drive)
        QTimer.singleShot(6000, loop.quit)
        loop.exec()
        worker.stop()

        self.assertTrue(merged, "连点未走合并路径(_planner.pending 未置位)")
        self.assertIsNone(worker._planner.pending, "合并目标应已被消费")
        self.assertEqual(len(seek_calls), 3,
                         f"应 3 次定位(open+步进+合并补一次):{seek_calls}")
        self.assertGreaterEqual(len(pts_list), 3, f"步进未生效:{pts_list}")
        self.assertAlmostEqual(pts_list[-1], 2 / 30, delta=0.03,
                               msg=f"连点累计两帧应到 2/30s:{pts_list}")

    def test_controller_step_syncs_pause_state(self):
        """步进后 PlayerController 同步为暂停态(UI 按钮/音频联动正确)。"""
        worker = DecodeWorker()
        controller = PlayerController(worker)
        opened = []
        changed = []
        stepped = []

        controller.opened.connect(opened.append)
        controller.playingChanged.connect(changed.append)
        worker.open(self.video)

        hook, loop = self._run_until(
            lambda *a: len(opened) >= 1 and stepped and not controller.playing)
        controller.opened.connect(hook.check)
        controller.playingChanged.connect(hook.check)

        worker.start()
        QTimer.singleShot(1000, controller.play)
        QTimer.singleShot(1500, lambda: (stepped.append(1), controller.step(1)))
        QTimer.singleShot(4000, loop.quit)
        loop.exec()
        worker.stop()

        self.assertEqual(changed, [True, False], f"应发出 播放→暂停 两次状态:{changed}")
        self.assertFalse(controller.playing, "步进后控制器应为暂停态")

    def test_step_while_playing_then_resume(self):
        """播放中步进 → 自动暂停 → 再播放:立即从当前帧续播。"""
        worker = DecodeWorker()
        frames = []
        before_play = []

        worker.frameReady.connect(lambda img, pts: frames.append(pts))
        worker.open(self.video)
        worker.seek(0.0)

        hook, loop = self._run_until(
            lambda *a: before_play and len(frames) >= before_play[0] + 2)
        worker.frameReady.connect(hook.check)
        worker.start()
        QTimer.singleShot(2000, worker.play)
        QTimer.singleShot(2500, lambda: worker.step(-1))
        QTimer.singleShot(3000, lambda: (worker.play(),
                                         before_play.append(len(frames))))
        QTimer.singleShot(6000, loop.quit)
        loop.exec()
        was_playing = worker._playing
        worker.stop()

        self.assertTrue(before_play, "播放中应已产生帧")
        self.assertTrue(was_playing, "再次播放应生效")
        self.assertGreaterEqual(len(frames), before_play[0] + 2,
                                f"步进后应快速续播:{frames}")

    def test_audio_data_emitted(self):
        """带音频的视频播放时输出 s16 音频数据。"""
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        video = os.path.join(td, "with_audio.mp4")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-y", "-f", "lavfi", "-i",
             "testsrc2=size=320x180:rate=30:duration=1",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
             "-c:v", "libx264", "-c:a", "aac", "-shortest", video],
            capture_output=True, check=True)

        worker = DecodeWorker()
        audio_chunks = []
        opened_info = []

        worker.audioData.connect(lambda data, pts: audio_chunks.append(data))
        worker.opened.connect(opened_info.append)
        worker.open(video)

        hook, loop = self._run_until(lambda *a: len(audio_chunks) >= 3)
        worker.audioData.connect(hook.check)

        worker.start()
        QTimer.singleShot(2000, worker.play)
        QTimer.singleShot(7000, loop.quit)
        loop.exec()
        worker.stop()

        self.assertGreaterEqual(len(audio_chunks), 3, "播放未输出音频数据")
        self.assertTrue(all(len(c) > 0 for c in audio_chunks), "音频数据为空")
        self.assertTrue(any("audio" in info for info in opened_info), "opened 未带音频信息")
        worker.close()

    def test_no_audio_video_no_audio_data(self):
        """无音轨的视频不发送音频数据。"""
        worker = DecodeWorker()
        audio_chunks = []

        worker.audioData.connect(lambda data, pts: audio_chunks.append(data))
        worker.open(self.video)
        hook, loop = self._run_until(lambda *a: len(audio_chunks) >= 1)
        worker.audioData.connect(hook.check)
        worker.start()
        QTimer.singleShot(2000, worker.play)
        QTimer.singleShot(4000, loop.quit)
        loop.exec()
        worker.stop()
        self.assertEqual(audio_chunks, [], "无音轨视频不应有音频输出")

    def test_pan_and_auto_center(self):
        """缩放 >100% 支持平移偏移;≤100% 自动居中并清零偏移。"""
        from PySide6.QtGui import QImage

        fv = FrameView()
        fv.set_project(Project("D:\\x.mp4", 10))
        fv.set_scope_context(Scope("global", 0, 10, GridLayout.thirds(), "project", (), ()))
        fv.set_frame(QImage(640, 360, QImage.Format.Format_RGB888))
        fv.resize(800, 500)
        fv._update_transform()
        base_off = (fv._offx, fv._offy)

        fv._zoom = 2.0
        fv._pan_x, fv._pan_y = 50.0, 30.0
        fv._update_transform()
        self.assertNotEqual((fv._offx, fv._offy), base_off)
        fv._zoom = 1.0
        fv._update_transform()
        self.assertEqual((fv._pan_x, fv._pan_y), (0.0, 0.0))
        self.assertAlmostEqual(fv._offx, base_off[0], places=6)
        self.assertAlmostEqual(fv._offy, base_off[1], places=6)
        fv.close()

    def test_constrain_four_directions(self):
        """挤压约束:四个方向越界都收缩宽/高,且不越出格子。"""
        fv = FrameView()
        pr = Project("D:\\x.mp4", 10)
        pr.grid = GridLayout.thirds()
        fv.set_project(pr)
        fv.set_scope_context(make_scope(pr, None, 0, 10))
        orig = Rect(0.1, 0.1, 0.1, 0.1)          # 三格格1 内
        c = fv._interactor._constrain_to_orig_tile
        g = pr.grid
        r = c(Rect(-0.05, 0.1, 0.1, 0.1), orig, g)
        self.assertAlmostEqual(r.nx, 0.0, places=6)
        self.assertLess(r.nw, 0.1 - 1e-9)
        r2 = c(Rect(0.1, 0.1, 0.3, 0.1), orig, g)
        self.assertAlmostEqual(r2.nx + r2.nw, 1 / 3, places=6)
        self.assertLess(r2.nw, 0.3)
        r3 = c(Rect(0.1, -0.05, 0.1, 0.1), orig, g)
        self.assertAlmostEqual(r3.ny, 0.0, places=6)
        self.assertLess(r3.nh, 0.1 - 1e-9)
        r4 = c(Rect(0.1, 0.1, 0.1, 0.95), orig, g)
        self.assertAlmostEqual(r4.ny + r4.nh, 1.0, places=6)
        self.assertLess(r4.nh, 0.95)
        fv.close()

    def test_widgets_render(self):
        """FrameView / TimelineWidget 在 offscreen 下可构建、可渲染。"""
        from core.planning import effective_scopes

        w = FrameView()
        w.resize(800, 500)
        proj = Project(self.video, 3.0)
        proj.grid = GridLayout.thirds()
        proj.patches = [Patch(dst=Rect(0.45, 0.2, 0.1, 0.1), src=Rect(0.1, 0.2, 0.1, 0.1),
                              source_tile_idx=0)]
        proj.segments = [Segment(id="A", start=0, end=3, patch_ids=[proj.patches[0].id])]
        w.set_project(proj)
        w.set_scope_context(effective_scopes(proj)[0])
        w.set_mode(FrameView.MODE_TARGET)
        w.set_mode(FrameView.MODE_SELECT)
        w.resize(640, 360)
        w.show()

        tl = TimelineWidget()
        tl.resize(800, 60)
        tl.set_data(3.0, (0.0, 3.0), effective_scopes(proj), 1.5, proj.segments)
        tl.show()

        self.app.processEvents()
        w.close()
        tl.close()


class TestEdgeMoveGuard(unittest.TestCase):
    """边缘/分界线移动守卫:当前 scope 有补丁/复制时禁止移动(防对齐错位)。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _make(self):
        fv = FrameView()
        pr = Project("D:\\x.mp4", 100)
        pr.grid = g = GridLayout.thirds()
        patch = Patch(dst=Rect(0.45, 0.1, 0.1, 0.2), src=Rect(0.1, 0.1, 0.1, 0.2),
                      source_tile_idx=0, dst_tile_idx=1)
        pr.patches = [patch]
        seg0 = Segment(id="A", start=0, end=50, patch_ids=[patch.id])     # 启用补丁
        seg1 = Segment(id="B", start=50, end=100)                          # 无补丁
        pr.segments = [seg0, seg1]
        state = AppState(settings_org="FenshenFuTest", settings_app="test")
        state.set_video(pr, None)
        fv.set_project(pr)
        fv.set_state(state)
        return fv, pr, state, g, seg0, seg1

    def test_edge_move_blocked_with_active_patches(self):
        from PySide6.QtCore import QPointF

        fv, pr, state, g, seg0, seg1 = self._make()
        blocked = []
        fv.edgeMoveBlocked.connect(lambda: blocked.append(1))

        # 播放头在 seg0(有补丁)→ 方向键微调被禁
        state.set_current_time(10)
        fv.set_scope_context(make_scope(pr, seg0))
        fv._hover_grid_edge = 1
        fv._interactor.nudge_grid_edge(Qt.Key.Key_Right)
        self.assertEqual(len(blocked), 1, "应发禁止信号")
        self.assertAlmostEqual(g.tiles[1].nx, 1 / 3, places=6, msg="分界线不应移动")

        # 鼠标拖线:被禁 → 线不动(守卫在编辑之前)
        fv._interactor._apply_grid_edge(DragState(DragOp.MOVE_EDGE, edge=1),
                                        QPointF(0, 0))
        self.assertEqual(len(blocked), 2, "拖线也应发禁止信号")
        self.assertAlmostEqual(g.tiles[1].nx, 1 / 3, places=6, msg="拖线不应移动分界线")

        # 无补丁段:放行 → 懒克隆独立网格后移动(分界线按段独立,全局不动)
        state.set_current_time(60)
        fv.set_scope_context(make_scope(pr, seg1))
        fv._hover_grid_edge = 1
        fv._interactor.nudge_grid_edge(Qt.Key.Key_Right)
        self.assertEqual(len(blocked), 2, "无补丁段不应被禁")
        self.assertIsNotNone(seg1.grid, "应懒克隆独立网格")
        self.assertIsNot(seg1.grid, g, "克隆网格不应与全局同对象")
        self.assertNotAlmostEqual(seg1.grid.tiles[1].nx, 1 / 3, places=6,
                                  msg="无补丁段应可移动")
        self.assertAlmostEqual(g.tiles[1].nx, 1 / 3, places=6,
                               msg="分界线按段独立:全局网格不应被改动")
        fv.close()

    def test_patches_hidden_when_not_enabled(self):
        """当前段未启用的补丁在预览画面隐藏(按 scope 过滤绘制)。"""
        from PySide6.QtGui import QImage

        fv = FrameView()
        pr = Project("D:\\x.mp4", 100)
        pr.grid = g = GridLayout.thirds()
        dst = Rect(1 / 3 + 0.05, 0.1, 0.3, 0.3)
        src = g.align_rect(dst, 0)
        patch = Patch(dst=dst, src=src, source_tile_idx=0, dst_tile_idx=1, lock_align=True)
        pr.patches = [patch]
        seg0 = Segment(id="A", start=0, end=50, patch_ids=[patch.id])     # 启用补丁
        seg1 = Segment(id="B", start=50, end=100)                          # 未启用
        pr.segments = [seg0, seg1]
        fv.set_project(pr)
        img = QImage(640, 360, QImage.Format.Format_RGB888)
        img.fill(Qt.GlobalColor.white)
        fv.set_frame(img)
        fv.resize(800, 500)

        fv.set_scope_context(make_scope(pr, seg0))
        pm = fv.grab()
        wx, wy = fv._n2w(dst.nx + dst.nw / 2, dst.ny + dst.nh / 2)
        c = pm.toImage().pixelColor(int(wx), int(wy))
        self.assertGreaterEqual(c.red() - c.green(), 15,
                                f"启用段应显示补丁填充:{c.name()}")

        fv.set_scope_context(make_scope(pr, seg1))
        pm = fv.grab()
        c2 = pm.toImage().pixelColor(int(wx), int(wy))
        self.assertLessEqual(abs(c2.red() - c2.green()), 10,
                             f"未启用段应隐藏补丁:{c2.name()}")
        fv.close()

    def test_no_yellow_fill_with_selected_patch_and_copy(self):
        """选中补丁 + 复制高亮:复制格子不被不透明黄填充(brush 残留回归)。"""
        from PySide6.QtGui import QImage

        fv = FrameView()
        pr = Project("D:\\x.mp4", 100)
        pr.grid = g = GridLayout.thirds()
        dst = Rect(1 / 3 + 0.05, 0.1, 0.3, 0.3)
        src = g.align_rect(dst, 0)
        p = Patch(dst=dst, src=src, source_tile_idx=0, dst_tile_idx=1, lock_align=True)
        pr.patches = [p]
        fv.set_project(pr)
        fv.set_scope_context(make_scope(pr, None, 0, 100))
        fv.set_copy_highlight([(0, [2])])     # 复制: 左格 -> 右格
        fv._selected = p.id                   # 选中补丁 → 触发手柄绘制
        img = QImage(640, 360, QImage.Format.Format_RGB888)
        img.fill(Qt.GlobalColor.white)
        fv.set_frame(img)
        fv.resize(800, 500)
        pm = fv.grab()
        for nx in (1 / 6, 5 / 6):             # 复制来源格/目标格中心
            wx, wy = fv._n2w(nx, 0.5)
            c = pm.toImage().pixelColor(int(wx), int(wy))
            self.assertFalse(
                (c.red() > 180 and c.green() > 140 and c.blue() < 120),
                f"复制格子不应被不透明黄填充:{c.name()}")
        fv.close()

    def test_edge_move_blocked_implicit_full_segment(self):
        """无片段(隐式全段)且存在补丁 → 同样禁止移动边界。"""
        fv = FrameView()
        pr = Project("D:\\x.mp4", 100)
        pr.grid = g = GridLayout.thirds()
        pr.patches = [Patch(dst=Rect(0.45, 0.1, 0.1, 0.2), src=Rect(0.1, 0.1, 0.1, 0.2),
                            source_tile_idx=0, dst_tile_idx=1)]
        state = AppState(settings_org="FenshenFuTest", settings_app="test")
        state.set_video(pr, None)
        fv.set_project(pr)
        fv.set_state(state)
        fv.set_scope_context(make_scope(pr, None, 0, 100))   # global scope
        blocked = []
        fv.edgeMoveBlocked.connect(lambda: blocked.append(1))
        fv._hover_grid_edge = 1
        fv._interactor.nudge_grid_edge(Qt.Key.Key_Left)
        self.assertEqual(len(blocked), 1, "隐式全段有补丁也应禁止")
        self.assertAlmostEqual(g.tiles[1].nx, 1 / 3, places=6, msg="分界线不应移动")
        fv.close()


class TestInvalidPatch(unittest.TestCase):
    """目标格全取消(dst 清零)→ 补丁整条(含 src)从预览隐藏。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_invalid_patch_hidden_entirely(self):
        from PySide6.QtGui import QImage

        fv = FrameView()
        pr = Project("D:\\x.mp4", 100)
        pr.grid = g = GridLayout.thirds()
        dst = Rect(1 / 3 + 0.05, 0.1, 0.3, 0.3)
        src = g.align_rect(dst, 0)
        p = Patch(dst=dst, src=src, source_tile_idx=0, dst_tile_idx=1, lock_align=True)
        pr.patches = [p]
        fv.set_project(pr)
        fv.set_scope_context(make_scope(pr, None, 0, 100))
        img = QImage(640, 360, QImage.Format.Format_RGB888)
        img.fill(Qt.GlobalColor.white)
        fv.set_frame(img)
        fv.resize(800, 500)
        p.dst = Rect(0, 0, 0, 0)             # 目标格全取消 → dst 清零
        pm = fv.grab()
        wx, wy = fv._n2w(src.nx + src.nw / 2, src.ny + src.nh / 2)
        c = pm.toImage().pixelColor(int(wx), int(wy))
        self.assertLessEqual(abs(c.red() - c.green()), 10,
                             f"无效补丁的 src 不应显示:{c.name()}")
        fv.close()


class TestCopyHighlight(unittest.TestCase):
    """复制高亮绘制:来源/目标格保留半透明填充(用户要求,同格混色允许)。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_copy_highlight_has_fill(self):
        from PySide6.QtGui import QImage

        fv = FrameView()
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        g = pr.grid
        fv.set_project(pr)
        fv.set_scope_context(make_scope(pr, None, 0, 100))
        fv.set_copy_highlight([(0, [1])])          # 来源左格、目标中格
        img = QImage(640, 360, QImage.Format.Format_RGB888)
        img.fill(Qt.GlobalColor.white)
        fv.set_frame(img)
        fv.resize(800, 500)
        pm = fv.grab()
        wx, wy = fv._n2w(0.5, 0.5)
        c = pm.toImage().pixelColor(int(wx), int(wy))
        self.assertGreaterEqual(c.red() - c.green(), 15,
                                f"中格中心应有红填充:{c.name()}")
        wx2, wy2 = fv._n2w(1 / 6, 0.5)
        c2 = pm.toImage().pixelColor(int(wx2), int(wy2))
        self.assertGreaterEqual(c2.green() - c2.red(), 15,
                                f"左格中心应有绿填充:{c2.name()}")
        fv.close()


class TestDerivedPatchTargetsCache(unittest.TestCase):
    def test_derived_targets_cached_until_refresh(self):
        """多目标补丁派生矩形在 paint 间复用;refresh 后失效重算。"""
        QApplication.instance() or QApplication([])
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        p = Patch(dst=Rect(0.45, 0.1, 0.1, 0.1), src=Rect(0.1, 0.1, 0.1, 0.1),
                  source_tile_idx=0, dst_tile_idx=1, extra_tile_indices=[2])
        pr.patches = [p]
        fv = FrameView()
        fv.set_project(pr)
        fv.set_scope_context(Scope("global", 0, 100, pr.grid, "project", (p.id,)))
        with mock.patch.object(pr.grid, "align_from_src",
                               wraps=pr.grid.align_from_src) as m:
            first = fv.derived_patch_targets(p)
            second = fv.derived_patch_targets(p)
        self.assertIs(first, second)
        self.assertEqual(m.call_count, 1)
        fv.refresh()
        with mock.patch.object(pr.grid, "align_from_src",
                               wraps=pr.grid.align_from_src) as m2:
            third = fv.derived_patch_targets(p)
        self.assertEqual(m2.call_count, 1)
        self.assertEqual(len(third), 2)


class TestFrameHoverReporting(unittest.TestCase):
    """FrameView 悬停上报:方向键分发依赖 hover_target(回归:悬停画面需先点击)。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_enter_sets_frame_leave_sets_window(self):
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QEnterEvent

        fv = FrameView()
        state = AppState(settings_org="FenshenFuTest", settings_app="test")
        fv.set_state(state)
        self.assertEqual(state.hover_target, "window")
        fv.enterEvent(QEnterEvent(QPointF(1, 1), QPointF(1, 1), QPointF(0, 0)))
        self.assertEqual(state.hover_target, "frame", "进入画面应上报 frame")
        # 离开:上报 window + 清分界线悬停(回归:mouseLeaveEvent 不是 Qt 虚方法)
        fv._hover_grid_edge = 1
        fv.leaveEvent(QEvent(QEvent.Type.Leave))
        self.assertEqual(state.hover_target, "window", "离开画面应恢复 window")
        self.assertIsNone(fv._hover_grid_edge, "离开画面应清分界线悬停")
        fv.close()


class TestRangeFormat(unittest.TestCase):
    """处理范围 时:分:秒:帧 格式(帧从 1 开始)。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_roundtrip_with_fps(self):
        from core.ffprobe import MediaInfo
        from ui.video_info_panel import VideoInfoPanel

        p = VideoInfoPanel()
        p.show_info(MediaInfo(path="x.mp4", duration=100.0, fps=30.0))
        p.set_range(10.5, 90.25)
        lo, hi = p.get_range()
        self.assertAlmostEqual(lo, 10.5, delta=0.02)
        self.assertAlmostEqual(hi, 90.25, delta=0.02)   # 帧粒度(半帧内)
        # 帧号从 1 开始:0.0s → 帧框 = 1
        p.set_range(0.0, 1 / 30)
        self.assertEqual(p._range_start_spins[3].value(), 1)
        self.assertEqual(p._range_end_spins[3].value(), 2)
        p.close()

    def test_buttons_notify_rangeChanged(self):
        """开头/结尾按钮点击后应发 rangeChanged(回归:只 set_range 时间轴不刷新)。"""
        from core.ffprobe import MediaInfo
        from ui.video_info_panel import VideoInfoPanel

        p = VideoInfoPanel()
        p.show_info(MediaInfo(path="x.mp4", duration=100.0, fps=30.0))
        got = []
        p.rangeChanged.connect(lambda lo, hi: got.append((lo, hi)))
        p.set_range(10.0, 90.0)
        p._btn_begin.click()
        self.assertEqual(got[-1], (0.0, 90.0), "开头按钮应通知范围变化")
        p._btn_end.click()
        self.assertAlmostEqual(got[-1][1], 100.0, delta=0.02, msg="结尾按钮应通知范围变化")
        p._btn_current_start.click()          # 播放头 0 → 起点 0(useCurrent 路径)
        self.assertEqual(got[-1][0], 0.0)
        p.close()


class TestVideoInfoPanelAudio(unittest.TestCase):
    """视频信息面板的音频码率设置(与范围格式无关,独立成类)。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_audio_kbps_combo(self):
        from ui.video_info_panel import VideoInfoPanel

        p = VideoInfoPanel()
        s = p.get_settings()
        self.assertEqual(s.audio_kbps, 0)               # 默认匹配源码率
        p._audio_kbps_combo.setCurrentIndex(2)          # 128 kbps
        self.assertEqual(p.get_settings().audio_kbps, 128)
        p.apply_settings(s)                             # 回写 0 → 匹配源码率
        self.assertEqual(p.get_settings().audio_kbps, 0)
        p.close()


class TestRubberCreatesPatch(unittest.TestCase):
    """目标模式框选 → 生成补丁(回归:rubber_mode 属性引用报错)。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_target_rubber_creates_patch(self):
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QImage

        fv = FrameView()
        pr = Project("D:\\x.mp4", 10)
        pr.grid = GridLayout.thirds()
        fv.set_project(pr)
        fv.set_scope_context(make_scope(pr, None, 0, 10))
        fv.set_mode(FrameView.MODE_TARGET)
        img = QImage(640, 360, QImage.Format.Format_RGB888)
        img.fill(Qt.GlobalColor.white)
        fv.set_frame(img)
        fv.resize(800, 500)
        fv.show()
        self.app.processEvents()
        created = []
        fv.patchCreated.connect(created.append)
        QTest.mousePress(fv, Qt.MouseButton.LeftButton, pos=QPoint(300, 200))
        QTest.mouseMove(fv, QPoint(400, 300))
        QTest.mouseRelease(fv, Qt.MouseButton.LeftButton, pos=QPoint(400, 300))
        self.app.processEvents()
        self.assertEqual(len(created), 1, "目标模式框选应生成补丁")
        self.assertEqual(created[0].source_tile_idx, 0, "源格应自动对齐")
        fv.close()

    def test_rubber_visible_during_drag(self):
        """拖动框选过程中 rubber 状态随鼠标更新且绘制可见(回归:读 FrameView._rubber 恒 None)。"""
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QImage

        fv = FrameView()
        pr = Project("D:\\x.mp4", 10)
        pr.grid = GridLayout.thirds()
        fv.set_project(pr)
        fv.set_scope_context(make_scope(pr, None, 0, 10))
        fv.set_mode(FrameView.MODE_TARGET)
        img = QImage(640, 360, QImage.Format.Format_RGB888)
        img.fill(Qt.GlobalColor.white)
        fv.set_frame(img)
        fv.resize(800, 500)
        fv.show()
        self.app.processEvents()
        QTest.mousePress(fv, Qt.MouseButton.LeftButton, pos=QPoint(300, 200))
        QTest.mouseMove(fv, QPoint(400, 300))
        self.app.processEvents()
        rubber = fv._interactor.rubber
        self.assertIsNotNone(rubber, "拖动中 rubber 应存在")
        self.assertGreater(rubber[2], rubber[0], "rubber 应随鼠标扩展")
        # 绘制可见:rubber 起点区域半透明红填充(白底上偏红)
        pm = fv.grab()
        c = pm.toImage().pixelColor(300, 200)
        self.assertGreater(c.red() - c.green(), 10,
                           f"rubber 区域应有红色填充:{c.name()}")
        QTest.mouseRelease(fv, Qt.MouseButton.LeftButton, pos=QPoint(400, 300))
        fv.close()

    def test_source_rubber_creates_patch(self):
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QImage

        fv = FrameView()
        pr = Project("D:\\x.mp4", 10)
        pr.grid = GridLayout.thirds()
        fv.set_project(pr)
        fv.set_scope_context(make_scope(pr, None, 0, 10))
        fv.set_mode(FrameView.MODE_SOURCE)
        img = QImage(640, 360, QImage.Format.Format_RGB888)
        img.fill(Qt.GlobalColor.white)
        fv.set_frame(img)
        fv.resize(800, 500)
        fv.show()
        self.app.processEvents()
        created = []
        fv.patchCreated.connect(created.append)
        QTest.mousePress(fv, Qt.MouseButton.LeftButton, pos=QPoint(100, 200))
        QTest.mouseMove(fv, QPoint(180, 280))
        QTest.mouseRelease(fv, Qt.MouseButton.LeftButton, pos=QPoint(180, 280))
        self.app.processEvents()
        self.assertEqual(len(created), 1, "源模式框选应生成补丁")
        fv.close()


class TestTimelineHover(unittest.TestCase):
    """时间轴悬停时间:拖出时间轴松开后清除(回归:Qt 按下期间 leave 不触发残留)。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_release_outside_clears_hover(self):
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QMouseEvent

        tl = TimelineWidget()
        tl.resize(800, 60)
        tl.set_data(100, (0, 100), [], 50)
        tl._hover_t = 42.0                     # 模拟悬停时间显示中
        ev = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(900, 30),
                         QPointF(900, 30), Qt.MouseButton.LeftButton,
                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        tl.mouseReleaseEvent(ev)               # 出界(900 > 800)松开
        self.assertIsNone(tl._hover_t, "拖出时间轴松开后悬停时间应清除")

    def test_leave_event_clears_hover(self):
        """leaveEvent 清悬停时间(回归:曾用 mouseLeaveEvent,Qt 从不调用一直显示)。"""
        from PySide6.QtCore import QEvent

        tl = TimelineWidget()
        tl.resize(800, 60)
        tl.set_data(100, (0, 100), [], 50)
        tl._hover_t = 42.0
        tl.leaveEvent(QEvent(QEvent.Type.Leave))
        self.assertIsNone(tl._hover_t, "leaveEvent 应清除悬停时间")

    def test_release_inside_keeps_hover(self):
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QMouseEvent

        tl = TimelineWidget()
        tl.resize(800, 60)
        tl.set_data(100, (0, 100), [], 50)
        tl._hover_t = 42.0
        ev = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(400, 30),
                         QPointF(400, 30), Qt.MouseButton.LeftButton,
                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        tl.mouseReleaseEvent(ev)               # 界内松开:保留(鼠标仍在)
        self.assertIsNotNone(tl._hover_t)


class TestPatchPanelCombo(unittest.TestCase):
    """补丁/复制面板下拉(CheckCombo):来源格单选、目标格多选、互斥。

    曾误命名 TestRangeFormat 遮蔽范围格式测试类,导致三个测试静默丢失。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_targets_combo_multi(self):
        """目标格:多选、来源格禁用、摘要"格1,格2"、防抖发射 main/extra。"""
        panel, p = _make_patch_panel()
        combo = panel._patch_table.cellWidget(0, 3)
        self.assertIsNotNone(combo)
        self.assertFalse(combo.model().item(0).isEnabled(), "来源格应禁用")
        self.assertEqual(combo.model().item(1).checkState(), Qt.CheckState.Checked)
        self.assertEqual(combo.lineEdit().text(), "格2")
        got = []
        panel.patchTargetsChanged.connect(lambda pid, main, extra: got.append((pid, main, extra)))
        _click_combo_item(combo, 2)
        self.assertTrue(combo.view().parentWidget().isVisible(), "点击项不应关闭弹出")
        QTest.qWait(300)   # 等防抖
        self.assertTrue(got, "应发射 patchTargetsChanged")
        self.assertEqual(got[-1], (p.id, 1, [2]), "主目标=最小索引,额外=[2]")
        self.assertEqual(combo.lineEdit().text(), "格2,格3")
        panel.close()

    def test_targets_combo_clear_all(self):
        """目标格全取消:摘要回占位 + 发射 (-1, []) 清空模型。"""
        panel, p = _make_patch_panel()
        combo = panel._patch_table.cellWidget(0, 3)
        got = []
        panel.patchTargetsChanged.connect(lambda pid, main, extra: got.append((pid, main, extra)))
        _click_combo_item(combo, 1)   # 取消格2(唯一勾选)
        self.assertTrue(combo.view().parentWidget().isVisible(), "点击项不应关闭弹出")
        QTest.qWait(300)
        self.assertTrue(got, "全取消也应发射")
        self.assertEqual(got[-1], (p.id, -1, []))
        self.assertEqual(combo.lineEdit().text(), "目标格…")
        panel.close()

    def test_source_combo_single(self):
        """来源格:单选——点另一项只勾该项、摘要"格N"、发射 idx。"""
        panel, p = _make_patch_panel()
        combo = panel._patch_table.cellWidget(0, 2)
        self.assertEqual(combo.model().item(0).checkState(), Qt.CheckState.Checked)
        self.assertEqual(combo.lineEdit().text(), "格1")
        got = []
        panel.patchSourceChanged.connect(lambda pid, idx: got.append((pid, idx)))
        _click_combo_item(combo, 2)
        self.assertTrue(combo.view().parentWidget().isVisible(), "点击项不应关闭弹出")
        QTest.qWait(300)
        self.assertTrue(got, "应发射 patchSourceChanged")
        self.assertEqual(got[-1], (p.id, 2))
        self.assertEqual(combo.model().item(2).checkState(), Qt.CheckState.Checked)
        self.assertEqual(combo.model().item(0).checkState(), Qt.CheckState.Unchecked)
        self.assertEqual(combo.lineEdit().text(), "格3")
        panel.close()

    def test_mutual_exclusion_disabled(self):
        """互斥:来源格禁用已勾选目标格;目标格禁用来源格;set_disabled 运行时更新。"""
        panel, p = _make_patch_panel()   # p: source=0, dst=1
        src = panel._patch_table.cellWidget(0, 2)
        dst = panel._patch_table.cellWidget(0, 3)
        self.assertFalse(dst.model().item(0).isEnabled(), "目标格不应可选来源格")
        self.assertFalse(src.model().item(1).isEnabled(),
                         "来源格不应可选已勾选的目标格")
        # 点击禁用的目标格位置 → 无变化
        _click_combo_item(src, 1)
        QTest.qWait(300)
        self.assertEqual(src.checked(), [0], "禁用的目标格点击应无变化")
        # 非目标格位置可正常切换(文字同步)
        _click_combo_item(src, 2)
        QTest.qWait(300)
        self.assertEqual(src.checked(), [2], "来源格可切换到非目标格位置")
        self.assertEqual(src.lineEdit().text(), "格3")
        # 主窗口同步:目标格互斥更新
        dst.set_disabled([2])
        dst.set_checked([])
        self.assertFalse(dst.model().item(2).isEnabled())
        self.assertEqual(dst.model().item(2).checkState(), Qt.CheckState.Unchecked)
        panel.close()

    def test_set_checked_syncs_without_emit(self):
        """外部 set_checked:同步勾选且不触发发射(防循环)。"""
        panel, p = _make_patch_panel()
        dst = panel._patch_table.cellWidget(0, 3)
        got = []
        panel.patchTargetsChanged.connect(lambda *a: got.append(a))
        dst.set_checked([2])
        QTest.qWait(300)
        self.assertEqual(got, [], "set_checked 不应触发发射")
        self.assertEqual(dst.lineEdit().text(), "格3")
        panel.close()

    def test_segment_line_delete_button_no_residue(self):
        """删除分段线后行重建:残留删除按钮必须清除(曾错位到分段/结尾行)。

        根因:非 line 行只 setItem("—")不清 cellWidget——删除分段线后
        setRowCount 收缩、行内容错位,原 line 行的删除按钮残留在新行,
        且闭包 t 仍指向已删除的线(点击会删错线)。
        """
        panel = PatchPanel()
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        pr.segments = [Segment(id="A", start=0, end=30), Segment(id="B", start=30, end=60),
                       Segment(id="C", start=60, end=100)]
        panel.set_project(pr)
        self.assertEqual(
            [r for r in range(panel._seg_table.rowCount())
             if isinstance(panel._seg_table.cellWidget(r, 4), QPushButton)],
            [2, 4], "3 段应有 2 条分段线的删除按钮")
        # 删除分段线2(60)→ 2 段:走 setRowCount 收缩路径
        pr.segments = [Segment(id="A", start=0, end=30), Segment(id="C", start=60, end=100)]
        panel.set_project(pr)
        self.assertEqual(
            [r for r in range(panel._seg_table.rowCount())
             if isinstance(panel._seg_table.cellWidget(r, 4), QPushButton)],
            [2], "删除线2 后按钮应只剩线1 一处,不得残留在分段/结尾行")
        panel.close()


class TestComboDeadZone(unittest.TestCase):
    """弹出层第一项上缘死区点击(回归:点击整次丢失 → 文字残留)。

    根因:弹出层容器有 2px 边框/边距,点击第一项上缘落在容器上而非
    viewport,indexAt 返回无效 → 事件不拦截 → 点击丢失。修后坐标夹进
    viewport 必命中某行;弹出层窗口外点击仍放行(由 Qt 关闭弹出)。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _make_combo(self):
        combo = CheckCombo(3, [2], [], multi=False, placeholder="来源格…",
                           on_change=lambda c: None)   # 初始勾选格3
        combo.show()
        combo.showPopup()
        QTest.qWait(50)
        return combo

    def test_dead_zone_click_first_item(self):
        """viewport y=-1(弹出层窗口内边框条,indexAt 无效区)点击 → 切第一项。

        注意 y=-2 在 offscreen 已落在弹出层窗口外(Qt::Popup 正常关闭区,
        _inside_popup 放行);y=-1 才是窗口内边框死区。
        """
        combo = self._make_combo()
        view = combo.view()
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                         pos=QPoint(10, -1))
        QTest.qWait(50)
        self.assertEqual(combo.checked(), [0], "边框死区点击应切换第一项")
        self.assertEqual(combo.lineEdit().text(), "格1", "文字应同步为格1")
        combo.hidePopup()

    def test_container_frame_click(self):
        """点击弹出层容器(本地坐标 x=1:真实平台左边框/offscreen view 左缘)
        → 容器级过滤器路径仍切换对应行。"""
        combo = self._make_combo()
        container = combo.view().parentWidget()
        QTest.mouseClick(container, Qt.MouseButton.LeftButton,
                         pos=QPoint(1, 10))
        QTest.qWait(50)
        self.assertEqual(combo.checked(), [0], "容器边框点击应切换第一项")
        combo.hidePopup()

    def test_outside_popup_click_passes_through(self):
        """弹出层窗口外点击 → 不切换且弹出关闭(放行给 Qt 的 Qt::Popup 语义)。"""
        combo = self._make_combo()
        container = combo.view().parentWidget()
        QTest.mouseClick(container, Qt.MouseButton.LeftButton,
                         pos=QPoint(-500, -500))
        QTest.qWait(50)
        self.assertEqual(combo.checked(), [2], "窗口外点击不应切换勾选")
        self.assertFalse(container.isVisible(), "窗口外点击应关闭弹出")

    def test_decreasing_switch_to_first_item_text(self):
        """用户链(3→2→1 递减):点第一项死区 → 文字不残留、发射 source=0。"""
        panel, _ = _make_patch_panel()
        src = panel._patch_table.cellWidget(0, 2)
        got = []
        panel.patchSourceChanged.connect(lambda pid, idx: got.append(idx))
        _click_combo_item(src, 2)      # → 格3
        QTest.qWait(300)
        _click_combo_item(src, 1)      # → 格2
        QTest.qWait(300)
        # 最后点第一项上缘(弹出层窗口内边框死区)
        view = src.view()
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                         pos=QPoint(10, -1))
        QTest.qWait(300)
        self.assertEqual(got[-1], 0, "递减切换最后点第一项应发射 source=0")
        self.assertEqual(src.lineEdit().text(), "格1", "文字不应残留为格2")
        self.assertEqual(src.checked(), [0])
        panel.close()


class TestPatchTileConstraint(unittest.TestCase):
    """分界线移动后补丁框体按新格子约束(回归:来源框体越过格界/分界线)。

    用户场景:格子2|3 分界线左移 → 格子3 变宽;目标模式在格子3 画 dst
    到最右边,dst 宽超过来源格 → 修复前 src 右边缘越过格子2|3 分界线,
    渲染时取到目标格像素(自我复制,修复错乱)。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _make_view(self):
        from PySide6.QtGui import QImage

        fv = FrameView()
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        fv.set_project(pr)
        fv.set_scope_context(make_scope(pr, None, 0, 100))
        img = QImage(640, 360, QImage.Format.Format_RGB888)
        img.fill(Qt.GlobalColor.white)
        fv.set_frame(img)
        fv.resize(800, 500)
        return fv, pr

    def _draw(self, fv, n1, n2) -> Patch:
        """目标模式从归一化 n1 拖到 n2 画 dst,返回创建的补丁。"""
        created = []
        fv.patchCreated.connect(created.append)
        fv.set_mode(fv.MODE_TARGET)
        wx1, wy1 = fv._n2w(*n1)
        wx2, wy2 = fv._n2w(*n2)
        QTest.mousePress(fv, Qt.MouseButton.LeftButton, pos=QPoint(int(wx1), int(wy1)))
        QTest.mouseMove(fv, QPoint(int(wx2), int(wy2)))
        QTest.mouseRelease(fv, Qt.MouseButton.LeftButton, pos=QPoint(int(wx2), int(wy2)))
        QTest.qWait(20)
        self.assertEqual(len(created), 1, "应创建补丁")
        return created[0]

    def test_draw_target_after_boundary_move(self):
        """分界线左移后画 dst 到格子3 最右边:来源框体不出分界线、同尺寸。"""
        fv, pr = self._make_view()
        pr.grid.move_edge(2, 0.5)            # 格子2|3 分界线左移 → 格子3 变宽
        p = self._draw(fv, (0.55, 0.4), (1.0, 0.6))   # 画到格子3 最右边

        self.assertEqual(p.dst_tile_idx, 2, "目标格应在格子3")
        t_src = pr.grid.tiles[p.source_tile_idx]
        self.assertLessEqual(p.src.nx + p.src.nw, t_src.nx + t_src.nw + 1e-9,
                             "来源框体不得越过格界(分界线)")
        t_dst = pr.grid.tiles[2]
        self.assertGreaterEqual(p.dst.nx, t_dst.nx - 1e-9)
        self.assertLessEqual(p.dst.nx + p.dst.nw, t_dst.nx + t_dst.nw + 1e-9,
                             "目标框体应在格子3 内")
        self.assertAlmostEqual(p.src.nw, p.dst.nw, places=6,
                               msg="src/dst 同尺寸(像素复制不变式)")
        fv.close()

    def test_auto_source_picks_fitting_tile(self):
        """自动来源格优先选能容纳 dst 的格(格子2 太窄 → 格子1,不收窄)。"""
        fv, pr = self._make_view()
        pr.grid.move_edge(2, 0.5)
        p = self._draw(fv, (0.52, 0.4), (0.75, 0.6))   # dst 宽 0.25

        self.assertEqual(p.source_tile_idx, 0, "格子2 装不下 0.25 宽 → 应选格子1")
        t_src = pr.grid.tiles[0]
        self.assertLessEqual(p.src.nx + p.src.nw, t_src.nx + t_src.nw + 1e-9)
        self.assertGreater(p.src.nw, 0.2, "格子1 装得下则不应收窄到窄格尺寸")
        self.assertAlmostEqual(p.dst.nw, p.src.nw, places=6)
        fv.close()


if __name__ == "__main__":
    unittest.main()
