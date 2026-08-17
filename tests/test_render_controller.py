"""RenderController 状态机测试(FakeProcess 注入,不依赖真实 ffmpeg)。

迁移自旧 test_batch_queue.py 6 项语义 + 转移表穷举。核心断言:
- 状态单一真源:每个转移单一赋值,skip 漏复位卡死不可能(回归旧 bug)
- NVENC_PENDING 期间队列不自动推进(等待用户三选一)
- retry_software 用 settings 副本,不动用户活工程(回归旧 bug)
- errorOccurred(FailedToStart)必须报错结束,不能卡"处理中"(回归旧 bug)
- 软件重试后天然只问一次 NVENC(无需标志位)
- 每个退出路径清理 tmp
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QObject, QProcess, Signal
from PySide6.QtWidgets import QApplication

from tests.helpers import make_media
from core.frame_index import FrameIndex
from core.grid import GridLayout
from fractions import Fraction

from core.planning import Scope
from core.project import EncoderSettings, Project
from services.batch_queue import JobStatus
from services.render_controller import RenderController, RenderState


class FakeProcess(QObject):
    """假 QProcess:脚本化 emit finished/errorOccurred,不启动真实进程。"""

    finished = Signal(int, QProcess.ExitStatus)
    errorOccurred = Signal(QProcess.ProcessError)
    readyReadStandardOutput = Signal()
    readyReadStandardError = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.calls: list[tuple] = []      # (program, args) 每次 start
        self._running = False
        self._out = b""

    def start(self, program, args):
        self.calls.append((program, args))
        self._running = True

    def kill(self):
        self._running = False

    def state(self):
        return (QProcess.ProcessState.Running if self._running
                else QProcess.ProcessState.NotRunning)

    def readAllStandardOutput(self):
        return self._out

    def readAllStandardError(self):
        return b""

    def set_stdout(self, data: bytes):
        """注入 stdout 数据(测试 -progress 解析)。"""
        self._out = data

    def emit_finished(self, code):
        self._running = False
        self.finished.emit(code, QProcess.ExitStatus.NormalExit)

    def emit_error(self):
        self._running = False
        self.errorOccurred.emit(QProcess.ProcessError.FailedToStart)


def make_project(path="D:\\x.mp4", mode="hw") -> Project:
    pr = Project(path, 1.0)
    pr.process_range = [0, 1]
    pr.grid = GridLayout.thirds()
    pr.settings = EncoderSettings(encoder_mode=mode)
    return pr


class ControllerTest(unittest.TestCase):
    """公共底座:建 controller + 打桩文件 IO(不产生真实文件)。"""

    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.proc = FakeProcess()
        self.ctrl = RenderController(process_factory=lambda p: self.proc,
                                     ffmpeg_path="ffmpeg")
        self._mocks = [
            mock.patch("services.render_controller.probe",
                       return_value=make_media(path="D:\\x.mp4", width=320, height=180,
            duration=1.0, fps=30.0, vcodec="h264",
            vbitrate=1_000_000, pix_fmt="yuv420p", has_audio=False)),
            mock.patch("services.render_controller.verify_output",
                       return_value=[]),
            mock.patch("services.render_controller.os.replace"),
            mock.patch("services.render_controller.os.path.exists",
                       return_value=True),
            # _dedupe_path 内部 while os.path.exists 会被上面 exists 桩变成死循环
            mock.patch.object(RenderController, "_dedupe_path",
                              return_value="D:\\out_修复.mp4"),
        ]
        for m in self._mocks:
            m.start()

    def tearDown(self):
        if self.ctrl._tmpdir and os.path.isdir(self.ctrl._tmpdir):
            shutil.rmtree(self.ctrl._tmpdir, ignore_errors=True)
            self.ctrl._tmpdir = ""
        for m in self._mocks:
            m.stop()

    # ---- 辅助:跑完一个任务的正常流程(1 段 + 1 concat) ----
    def run_job_to_ok(self):
        """当前 RUNNING 任务:段成功 + concat 成功 → ok。"""
        self.proc.emit_finished(0)      # 段
        self.proc.emit_finished(0)      # concat

    def run_job_to_nvenc_pending(self):
        """当前 RUNNING 任务:段阶段 NVENC 失败 → NVENC_PENDING。"""
        self.proc.emit_finished(1)


class TestSequential(ControllerTest):
    def test_two_jobs_sequential(self):
        """两任务串行:完成 → 自动启动下一个 → 全部 ok → queueFinished。"""
        done = []
        self.ctrl.jobFinished.connect(lambda jid, ok, msg: done.append((jid, ok)))
        finished = []
        self.ctrl.queueFinished.connect(lambda: finished.append(1))
        self.ctrl.submit(make_project("D:\\a.mp4"), auto_start=False)
        self.ctrl.submit(make_project("D:\\b.mp4"), auto_start=False)
        self.ctrl.start_queue()
        self.assertEqual(self.ctrl.state, RenderState.RUNNING)
        self.run_job_to_ok()
        self.assertEqual(self.ctrl.jobs()[0].status, JobStatus.OK)
        self.assertEqual(self.ctrl.jobs()[1].status, JobStatus.RUNNING)
        self.run_job_to_ok()
        self.assertEqual([ok for _, ok in done], [True, True])
        self.assertEqual(self.ctrl.state, RenderState.IDLE)
        self.assertEqual(finished, [1], "队列空应发 queueFinished")

    def test_remove_job_job(self):
        id1 = self.ctrl.submit(make_project("D:\\a.mp4"), auto_start=False)
        id2 = self.ctrl.submit(make_project("D:\\b.mp4"), auto_start=False)
        self.assertTrue(self.ctrl.remove_job(id2), "排队中可移除")
        self.assertTrue(self.ctrl.remove_job(id1), "排队中可移除")
        # 开始后:运行中的任务不可移除
        id3 = self.ctrl.submit(make_project("D:\\c.mp4"), auto_start=False)
        id4 = self.ctrl.submit(make_project("D:\\d.mp4"), auto_start=False)
        self.ctrl.start_queue()
        self.assertFalse(self.ctrl.remove_job(id3), "运行中不可移除")
        self.assertTrue(self.ctrl.remove_job(id4), "后续排队中可移除")


class TestNvencStateMachine(ControllerTest):
    def test_nvenc_failure_pauses_queue(self):
        """NVENC 失败 → NVENC_PENDING;期间不自动推进(等待用户)。"""
        prompts = []
        self.ctrl.retryPrompt.connect(lambda jid: prompts.append(jid))
        self.ctrl.submit(make_project("D:\\a.mp4"), auto_start=False)
        self.ctrl.submit(make_project("D:\\b.mp4"), auto_start=False)
        self.ctrl.start_queue()
        self.run_job_to_nvenc_pending()
        self.assertEqual(self.ctrl.state, RenderState.NVENC_PENDING)
        self.assertEqual(self.ctrl.jobs()[0].status, JobStatus.RETRY_PENDING)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(len(self.proc.calls), 1, "NVENC_PENDING 期间队列不得推进")
        # 期间 submit 新任务:只入队,不启动
        self.ctrl.submit(make_project("D:\\c.mp4"))
        self.assertEqual(self.ctrl.jobs()[-1].status, JobStatus.QUEUED)
        self.assertEqual(len(self.proc.calls), 1)

    def test_skip_after_nvenc_pending_resumes_queue(self):
        """NVENC 失败 → 用户选"跳过":队列必须继续(回归:曾永久卡死)。"""
        self.ctrl.submit(make_project("D:\\a.mp4"), auto_start=False)
        self.ctrl.submit(make_project("D:\\b.mp4"), auto_start=False)
        self.ctrl.start_queue()
        self.run_job_to_nvenc_pending()
        self.ctrl.skip_job()
        # 跳过 → 任务 failed、队列立即推进到下一任务(旧版曾漏复位 _running 永久卡死)
        self.assertEqual(self.ctrl.jobs()[0].status, JobStatus.FAILED)
        self.assertEqual(self.ctrl.jobs()[1].status, JobStatus.RUNNING,
                         "跳过后续任务应能启动(不能卡在 queued)")
        self.assertEqual(self.ctrl.state, RenderState.RUNNING)
        self.assertEqual(len(self.proc.calls), 2, "跳过应推进到下一任务")

    def test_retry_software_keeps_live_project_settings(self):
        """软件重试只改当前任务的 settings 副本,不动用户活工程(回归旧 bug)。"""
        live = make_project("D:\\a.mp4", mode="hw")
        self.ctrl.submit(live, auto_start=False)
        self.ctrl.start_queue()
        self.run_job_to_nvenc_pending()
        self.ctrl.retry_software()
        cur = self.ctrl.current_job
        self.assertIsNotNone(cur)
        self.assertEqual(cur.project.settings.encoder_mode, "sw", "重试任务应切软件编码")
        self.assertEqual(live.settings.encoder_mode, "hw", "重试不应改动用户活工程")
        self.assertEqual(self.ctrl.state, RenderState.RUNNING)

    def test_retry_asks_only_once(self):
        """软件重试后 _nvenc=False:再失败直接报错,不再询问(天然只问一次)。"""
        prompts = []
        self.ctrl.retryPrompt.connect(lambda jid: prompts.append(jid))
        self.ctrl.submit(make_project("D:\\a.mp4"), auto_start=False)
        self.ctrl.start_queue()
        self.run_job_to_nvenc_pending()
        self.ctrl.retry_software()
        self.proc.emit_finished(1)      # 软件重试仍失败
        self.assertEqual(self.ctrl.jobs()[0].status, JobStatus.FAILED)
        self.assertEqual(len(prompts), 1, "每任务只问一次 NVENC")
        self.assertEqual(self.ctrl.state, RenderState.IDLE)

    def test_retry_skip_illegal_when_idle(self):
        """IDLE 态下 retry/skip 是非法转移,必须无效果。"""
        self.ctrl.retry_software()
        self.ctrl.skip_job()
        self.assertEqual(self.ctrl.state, RenderState.IDLE)
        self.assertIsNone(self.ctrl.current_job)


class TestFailurePaths(ControllerTest):
    def test_missing_ffmpeg_fails_job(self):
        """ffmpeg 启动失败(FailedToStart 不发 finished)→ 报错结束,不能卡住。"""
        done = []
        self.ctrl.jobFinished.connect(lambda jid, ok, msg: done.append((jid, ok, msg)))
        self.ctrl.submit(make_project("D:\\a.mp4"))
        self.proc.emit_error()
        self.assertTrue(done, "启动失败的任务必须结束")
        self.assertFalse(done[0][1], "启动失败应报错")
        self.assertIn("无法启动 ffmpeg", done[0][2])
        self.assertEqual(self.ctrl.state, RenderState.IDLE)

    def test_proc_error_continues_queue(self):
        self.ctrl.submit(make_project("D:\\a.mp4"), auto_start=False)
        self.ctrl.submit(make_project("D:\\b.mp4"), auto_start=False)
        self.ctrl.start_queue()
        self.proc.emit_error()
        self.assertEqual(self.ctrl.jobs()[0].status, JobStatus.FAILED)
        self.assertEqual(self.ctrl.jobs()[1].status, JobStatus.RUNNING, "后续任务应继续")

    def test_cancel_current(self):
        """取消:kill 后 finished → cancelled,不产出文件,队列继续。"""
        states = []
        self.ctrl.jobStateChanged.connect(lambda jid, st: states.append(st))
        self.ctrl.submit(make_project("D:\\a.mp4"), auto_start=False)
        self.ctrl.submit(make_project("D:\\b.mp4"), auto_start=False)
        self.ctrl.start_queue()
        self.ctrl.cancel_current()
        self.proc.emit_finished(1)      # kill 后进程结束(cancelled 分支)
        self.assertEqual(self.ctrl.jobs()[0].status, JobStatus.CANCELLED)
        self.assertIn("cancelled", states)
        self.assertEqual(self.ctrl.jobs()[1].status, JobStatus.RUNNING, "取消后队列继续")

    def test_cancel_all_resets_on_start(self):
        """取消全部:任务保留标记 cancelled;start_queue 重置后可再次处理。"""
        self.ctrl.submit(make_project("D:\\a.mp4"), auto_start=False)
        self.ctrl.submit(make_project("D:\\b.mp4"), auto_start=False)
        self.ctrl.cancel_all()
        self.assertEqual([j.status for j in self.ctrl.jobs()],
                         [JobStatus.CANCELLED, JobStatus.CANCELLED])
        self.ctrl.start_queue()
        self.assertEqual(self.ctrl.jobs()[0].status, JobStatus.RUNNING)

    def test_failed_non_nvenc_continues(self):
        """非 NVENC 失败(sw 模式)直接 failed,队列继续。"""
        self.ctrl.submit(make_project("D:\\a.mp4", mode="sw"), auto_start=False)
        self.ctrl.submit(make_project("D:\\b.mp4"), auto_start=False)
        self.ctrl.start_queue()
        self.proc.emit_finished(1)
        self.assertEqual(self.ctrl.jobs()[0].status, JobStatus.FAILED)
        self.assertEqual(self.ctrl.jobs()[1].status, JobStatus.RUNNING)


class TestProgressGradual(ControllerTest):
    def test_progress_gradual_weighted(self):
        """段阶段进度 0~95% 加权;concat 阶段 95~99% 渐进;完成 100%。

        回归:单段任务段阶段完成时总进度曾直接 = 1.0,视频还在 concat/
        核验时进度条已 100%。
        """
        self.ctrl.submit(make_project("D:\\a.mp4"), auto_start=False)
        self.ctrl.start_queue()
        # 段阶段:注入 out_time_ms=500(0.5s,单位毫秒),段 dur=1.0 → seg_p=0.5
        self.proc.set_stdout(b"out_time_ms=500\n")
        self.proc.readyReadStandardOutput.emit()
        self.assertAlmostEqual(self.ctrl.current_job.progress, 0.5 * 0.95, places=3,
                               msg="段阶段进度应按 0.95 加权")
        # 段完成 → concat 阶段
        self.proc.emit_finished(0)
        self.assertEqual(self.ctrl._stage_kind, "concat")
        # concat 阶段:总输出 0.5s → 95~99% 区间(渐进,非直接 100%)
        self.proc.set_stdout(b"out_time_ms=500\n")
        self.proc.readyReadStandardOutput.emit()
        p = self.ctrl.current_job.progress
        self.assertGreaterEqual(p, 0.95, "concat 进度应从 95% 起")
        self.assertLess(p, 0.99, "concat 阶段不应直接 100%")
        # concat 完成 → finalize → 100%
        self.proc.emit_finished(0)
        self.assertEqual(self.ctrl.jobs()[0].progress, 1.0)


class TestVfrBeginScopeRecompute(ControllerTest):
    def test_frame_index_apply_recomputes_render_scopes(self):
        """VFR 快照无帧索引时:_begin_job 建索引并 apply 后必须重算期望帧数。"""
        idx = FrameIndex([0, 200, 400], Fraction(1, 100), duration_ticks=400)
        media = make_media(width=320, height=180, duration=10.0, fps=0.5,
                           is_vfr=True, frame_count=0, has_audio=False)
        pr = make_project("D:\\vfr.mp4")
        pr.duration = 10.0
        pr.process_range = [0.0, 3.5]
        pr.process_range_frames = [0, 1]      # 故意给一个会被索引纠正的近似值
        pr.process_range_pts_ticks = None
        with mock.patch("services.render_controller.probe", return_value=media), \
             mock.patch("services.render_controller.build_frame_index", return_value=idx):
            self.ctrl._begin_job(pr)
        self.assertEqual(pr.process_range_frames, [0, 2])
        self.assertEqual(self.ctrl._expected_frames, 2)


class TestScopeMerge(unittest.TestCase):
    def test_adjacent_empty_scopes_merge(self):
        pr = Project("D:\\x.mp4", 10)
        pr.grid = GridLayout.thirds()
        scopes = [
            Scope("gap:0-1", 0, 1, pr.grid, "project"),
            Scope("segment:A", 1, 2, pr.grid, "segment:A"),
            Scope("segment:B", 2, 3, pr.grid, "segment:B"),
        ]
        merged = RenderController._merge_render_scopes(pr, scopes, 320, 180)
        self.assertEqual(len(merged), 1)
        self.assertAlmostEqual(merged[0][0].start, 0)
        self.assertAlmostEqual(merged[0][0].end, 3)

    def test_different_work_scopes_not_merged(self):
        pr = Project("D:\\x.mp4", 10)
        pr.grid = GridLayout.thirds()
        scopes = [
            Scope("segment:A", 0, 1, pr.grid, "segment:A", ("p1",)),
            Scope("segment:B", 1, 2, pr.grid, "segment:B", ("p2",)),
        ]
        merged = RenderController._merge_render_scopes(pr, scopes, 320, 180)
        self.assertEqual(len(merged), 2)


class TestCleanup(ControllerTest):
    def _fake_tmp(self):
        # 先清掉 _begin_job 创建的真实 tmpdir,再注入测试用目录,避免原目录泄漏
        if self.ctrl._tmpdir and os.path.isdir(self.ctrl._tmpdir):
            shutil.rmtree(self.ctrl._tmpdir, ignore_errors=True)
        self.ctrl._tmpdir = tempfile.mkdtemp()
        self.ctrl._stages = [["x"]]
        with open(os.path.join(self.ctrl._tmpdir, "f.txt"), "w") as f:
            f.write("x")
        return self.ctrl._tmpdir

    def test_tmp_cleaned_on_ok(self):
        self.ctrl.submit(make_project("D:\\a.mp4"), auto_start=False)
        self.ctrl.start_queue()
        td = self._fake_tmp()
        self.run_job_to_ok()
        self.assertEqual(self.ctrl._tmpdir, "", "ok 路径必须清 tmp")
        self.assertFalse(os.path.isdir(td))

    def test_tmp_cleaned_on_fail(self):
        self.ctrl.submit(make_project("D:\\a.mp4", mode="sw"), auto_start=False)
        self.ctrl.start_queue()
        td = self._fake_tmp()
        self.proc.emit_finished(1)
        self.assertEqual(self.ctrl._tmpdir, "", "失败路径必须清 tmp")
        self.assertFalse(os.path.isdir(td))

    def test_tmp_cleaned_on_skip(self):
        self.ctrl.submit(make_project("D:\\a.mp4"), auto_start=False)
        self.ctrl.start_queue()
        td = self._fake_tmp()
        self.run_job_to_nvenc_pending()
        self.ctrl.skip_job()
        self.assertEqual(self.ctrl._tmpdir, "", "跳过路径必须清 tmp")
        self.assertFalse(os.path.isdir(td))

    def test_tmp_cleaned_on_cancel(self):
        self.ctrl.submit(make_project("D:\\a.mp4"), auto_start=False)
        self.ctrl.start_queue()
        td = self._fake_tmp()
        self.ctrl.cancel_current()
        self.proc.emit_finished(1)
        self.assertEqual(self.ctrl._tmpdir, "", "取消路径必须清 tmp")
        self.assertFalse(os.path.isdir(td))


if __name__ == "__main__":
    unittest.main()
