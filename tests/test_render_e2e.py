"""端到端渲染测试(迁移自旧 4 项):三格拼屏样例 → 补丁 → RenderController 全流程 → 输出核验。

覆盖:分段编码(有补丁段+无补丁段)、concat 拼接、参数核验、
"补丁区域像素 = 干净格同位置像素"的最终质量验证。
"""
import os
import shutil
import subprocess
import tempfile
import unittest

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

import av

from core.ffprobe import probe
from core.grid import GridLayout
from core.project import CopyRule, EncoderSettings, Patch, Project, Rect, Segment
from services.frame_index import build_frame_index
from services.render_controller import RenderController

FW, FH = 1920, 360   # 三格:每格 640x360


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _build_sample(td: str) -> str:
    """生成三格拼屏样例:三份相同 testsrc 横排 + 中格一块红框模拟遮挡。"""
    base = os.path.join(td, "base.mp4")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-y", "-f", "lavfi", "-i",
         "testsrc2=size=640x360:rate=30:duration=4",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", base],
        capture_output=True, check=True)
    sample = os.path.join(td, "sample.mp4")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-y",
         "-i", base, "-i", base, "-i", base,
         "-filter_complex",
         "[0:v][1:v][2:v]hstack=inputs=3,drawbox=x=840:y=40:w=80:h=60:color=red@0.9:t=fill[v]",
         "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-g", "30", sample],
        capture_output=True, check=True)
    return sample


@unittest.skipUnless(_has_ffmpeg(), "需要 ffmpeg")
class TestRenderE2E(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls._td = tempfile.mkdtemp()
        cls.video = _build_sample(cls._td)
        cls.output = os.path.join(cls._td, "sample_修复.mp4")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._td, ignore_errors=True)

    def _run_controller(self, project) -> dict:
        controller = RenderController()
        result = {"finished": None, "ok": None, "msg": None}
        loop = QEventLoop()
        controller.jobFinished.connect(
            lambda jid, ok, msg: (result.update(finished=jid, ok=ok, msg=msg), loop.quit()))
        QTimer.singleShot(120000, loop.quit)   # 超时兜底
        controller.submit(project)
        loop.exec()
        return result

    def test_full_pipeline(self):
        # 构造工程:补丁 = 覆盖中格红框,源 = 左格同相对位置(自动对齐)
        pr = Project(self.video, 4.0)
        pr.process_range = [0.0, 4.0]
        pr.grid = GridLayout.thirds()
        dst = Rect(840 / FW, 40 / FH, 80 / FW, 60 / FH)      # 红框所在(中格)
        src = pr.grid.align_rect(dst, 0)
        self.assertIsNotNone(src)
        patch = Patch(dst=dst, src=src, source_tile_idx=0, lock_align=True)
        pr.patches = [patch]
        # 两段:0-2s 有补丁,2-4s 无补丁(覆盖无补丁段 + concat)
        pr.segments = [Segment(id="A", start=0, end=2, patch_ids=[patch.id]),
                       Segment(id="B", start=2, end=4)]
        pr.settings = EncoderSettings(encoder_mode="sw", quality_mode="match")

        result = self._run_controller(pr)
        self.assertTrue(result["ok"], f"渲染失败:{result['msg']}")
        self.assertTrue(os.path.exists(self.output), "输出文件不存在")

        # ---- 核验输出参数 ----
        m = probe(self.output)
        self.assertEqual((m.width, m.height), (FW, FH))
        self.assertAlmostEqual(m.duration, 4.0, delta=0.3)
        self.assertEqual(m.vcodec, "h264")

        # ---- 像素级核验:输出中格红框区域 == 左格同位置内容 ----
        def frame_px(path, t):
            c = av.open(path)
            try:
                st = c.streams.video[0]
                tb = float(st.time_base)
                target = int(round(t / tb))
                c.seek(target, stream=st, backward=True)
                for f in c.decode(st):
                    if f.pts is not None and f.pts >= target:
                        return f.to_ndarray(format="rgb24")
                return None
            finally:
                c.close()

        out_frame = frame_px(self.output, 1.0)   # 有补丁段
        in_frame = frame_px(self.video, 1.0)     # 原视频
        self.assertIsNotNone(out_frame)
        self.assertIsNotNone(in_frame)
        y_src, y_dst = 40, 40
        x_src = int(round(src.nx * FW))
        x_dst = int(round(dst.nx * FW))
        w, h = 80, 60
        # 原视频:中格是红框,左格是干净内容 → 两者差异大
        raw_diff = float(np.abs(in_frame[y_src:y_src + h, x_src:x_src + w].astype(np.int16)
                               - in_frame[y_dst:y_dst + h, x_dst:x_dst + w].astype(np.int16)).mean())
        self.assertGreater(raw_diff, 30, "样例构造失败:中格与左格本应差异大")
        # 输出:中格区域已被左格内容覆盖 → 差异接近 0
        fixed_diff = float(np.abs(out_frame[y_src:y_src + h, x_src:x_src + w].astype(np.int16)
                                  - out_frame[y_dst:y_dst + h, x_dst:x_dst + w].astype(np.int16)).mean())
        self.assertLess(fixed_diff, 3, f"补丁区域未与干净格对齐:diff={fixed_diff}")

        # 无补丁段(2-4s):画面与源一致
        out2 = frame_px(self.output, 3.0)
        in2 = frame_px(self.video, 3.0)
        self.assertIsNotNone(out2)
        self.assertLess(float(np.abs(out2.astype(np.int16) - in2.astype(np.int16)).mean()),
                        4.0, "无补丁段画面应保持原样")

    def test_junction_no_hole_mid_gop(self):
        """拼接点空洞回归:切割点远离关键帧 + 带音频 → 输出不得有帧空洞。

        修复前:-ss 输入 seek + -t + 音频流 → 视频起点比目标晚
        (≈ 切割点距上一关键帧的时长,此处 ~0.5s)→ 段内视频比音频短 →
        concat 按容器时长偏移 → 拼接点时间戳空洞(播放卡顿、总时长膨胀、
        帧率显示异常)。样例 -g 30:关键帧在 0/1/2/3s,切割点 2.5s
        = 距上一关键帧 0.5s(触发输入 seek 误丢)。
        """
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        video = os.path.join(td, "audio_sample.mp4")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=4",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
             "-c:v", "libx264", "-c:a", "aac", "-shortest", "-g", "30", video],
            capture_output=True, check=True)

        pr = Project(video, 4.0)
        pr.process_range = [0.0, 4.0]
        pr.grid = GridLayout.thirds()
        pr.segments = [Segment(id="A", start=0, end=2.5),
                       Segment(id="B", start=2.5, end=4)]
        pr.settings = EncoderSettings(encoder_mode="sw", quality_mode="match")

        result = self._run_controller(pr)
        self.assertTrue(result["ok"], f"渲染失败:{result['msg']}")
        out = os.path.join(td, "audio_sample_修复.mp4")
        self.assertTrue(os.path.exists(out), "输出文件不存在")

        c = av.open(out)
        try:
            vs = c.streams.video[0]
            tb = float(vs.time_base)
            last = None
            gaps = []
            n = 0
            for f in c.decode(vs):
                pts = f.pts * tb
                if last is not None:
                    gaps.append(pts - last)
                last = pts
                n += 1
        finally:
            c.close()

        self.assertGreaterEqual(n, 118, f"帧数不足:{n}")      # 期望 120
        self.assertLessEqual(n, 122, f"帧数过多:{n}")
        self.assertAlmostEqual(last, 4.0, delta=0.05,
                               msg=f"末帧应到 4s:{last}")
        # 阈值 3/30:容纳拼接点 B 帧重排的正常间隙(≤2-3 帧);
        # 修复前空洞 ~0.5s 必失败
        self.assertLessEqual(max(gaps), 3 / 30,
                             f"拼接点存在时间戳空洞:{max(gaps):.3f}s")

    def test_crop_with_patch_outputs_cropped_size(self):
        """边缘线裁剪 + 补丁:输出分辨率 = 裁切后尺寸(overlay main 推断回归)。

        根因:crop 标签被补丁 crop 与 overlay 多消费者共享时,ffmpeg 把
        overlay main 尺寸推断回原始分辨率(输出 1920 而非裁切尺寸)。
        """
        # 独立视频:避免与其他测试共享输出路径(_dedupe_path 改名会干扰)
        video = os.path.join(self._td, "crop_sample.mp4")
        shutil.copy(self.video, video)
        pr = Project(video, 4.0)
        pr.process_range = [0.0, 4.0]
        pr.grid = GridLayout.thirds()
        pr.set_crop(0.1, 0.9)
        dst = Rect(840 / FW, 40 / FH, 80 / FW, 60 / FH)
        src = pr.grid.align_rect(dst, 0)
        patch = Patch(dst=dst, src=src, source_tile_idx=0, lock_align=True)
        pr.patches = [patch]
        pr.segments = [Segment(id="A", start=0, end=4, patch_ids=[patch.id])]
        pr.settings = EncoderSettings(encoder_mode="sw", quality_mode="match")

        result = self._run_controller(pr)
        self.assertTrue(result["ok"], f"渲染失败:{result['msg']}")
        out = os.path.join(self._td, "crop_sample_修复.mp4")
        m = probe(out)
        # 1920x360 源 → crop 0.1/0.9 → 1536x360
        self.assertEqual((m.width, m.height), (1536, 360),
                         f"输出应为裁切尺寸:{m.width}x{m.height}")

    def test_audio_copy(self):
        """带音频的视频:默认流复制。"""
        audio_video = os.path.join(self._td, "with_audio.mp4")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-y", "-f", "lavfi", "-i",
             "testsrc2=size=640x360:rate=30:duration=2",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-c:v", "libx264", "-c:a", "aac", "-shortest", audio_video],
            capture_output=True, check=True)
        pr = Project(audio_video, 2.0)
        pr.process_range = [0, 2]
        pr.grid = GridLayout.thirds()
        pr.settings = EncoderSettings(encoder_mode="sw")
        out = os.path.join(self._td, "with_audio_修复.mp4")
        result = self._run_controller(pr)
        self.assertTrue(result["ok"], f"渲染失败:{result['msg']}")
        m = probe(out)
        self.assertTrue(m.has_audio, "输出丢失音频")
        self.assertEqual(m.acodec, "aac")


    def test_60fps_split_preserves_frame_count_and_fps(self):
        """帧号方案 e2e:60fps 在非毫秒帧边界分割 → 总帧数/时长/fps 不变。"""
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        video = os.path.join(td, "fps60.mp4")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=60",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-t", "2", "-c:v", "libx264", "-c:a", "aac", "-shortest", video],
            capture_output=True, check=True)
        media = probe(video)
        pr = Project(video, media.duration)
        pr.process_range = [0.0, media.duration]
        pr.process_range_frames = [0, media.frame_count or int(round(media.duration * media.fps))]
        pr.grid = GridLayout.halves()
        # 非毫秒对齐分割点:第 31 帧、第 79 帧
        pr.split_at(31 / 60, 60.0)
        pr.split_at(79 / 60, 60.0)
        pr.settings = EncoderSettings(encoder_mode="sw", preset="ultrafast",
                                      quality_mode="custom", custom_kbps=1000,
                                      output_dir=td)
        result = self._run_controller(pr)
        self.assertTrue(result["ok"], f"渲染失败:{result['msg']}")
        out = os.path.join(td, "fps60_修复.mp4")
        self.assertTrue(os.path.exists(out), "输出文件不存在")
        c = av.open(out)
        try:
            vs = c.streams.video[0]
            frames = list(c.decode(vs))
            self.assertEqual(len(frames), 120, "总帧数必须保持 120")
            self.assertAlmostEqual(vs.duration * float(vs.time_base), 2.0, delta=0.02)
            self.assertAlmostEqual(float(vs.average_rate), 60.0, delta=0.05)
            self.assertTrue(c.streams.audio, "全片范围应保留原音轨")
        finally:
            c.close()

    def test_copy_flip_horizontal_pixel_match(self):
        """左右翻转 e2e:目标格像素 == 来源格像素的水平镜像。"""
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        video = os.path.join(td, "flip.mp4")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=160x180:rate=30:duration=2",
             "-f", "lavfi", "-i", "color=c=blue:size=160x180:rate=30:duration=2",
             "-filter_complex", "[0:v][1:v]hstack=inputs=2[v]",
             "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", video],
            capture_output=True, check=True)
        pr = Project(video, 2.0)
        pr.process_range = [0.0, 2.0]
        pr.grid = GridLayout.halves()
        rule = CopyRule(source_tile_idx=0, target_tile_indices=[1],
                        anchor_grid="project", flip_horizontal=True)
        pr.copy_rules = [rule]
        pr.settings = EncoderSettings(encoder_mode="sw", preset="ultrafast",
                                      quality_mode="custom", custom_kbps=1000,
                                      output_dir=td)
        result = self._run_controller(pr)
        self.assertTrue(result["ok"], f"渲染失败:{result['msg']}")
        out = os.path.join(td, "flip_修复.mp4")

        def read_frame(path, t):
            c = av.open(path)
            try:
                st = c.streams.video[0]
                tb = float(st.time_base)
                target = int(round(t / tb))
                c.seek(target, stream=st, backward=True)
                for f in c.decode(st):
                    if f.pts is not None and f.pts >= target:
                        return f.to_ndarray(format="rgb24")
                return None
            finally:
                c.close()

        out_frame = read_frame(out, 1.0)
        in_frame = read_frame(video, 1.0)
        self.assertIsNotNone(out_frame)
        self.assertIsNotNone(in_frame)
        left = out_frame[:, :160, :].astype(np.int16)
        right = out_frame[:, 160:, :].astype(np.int16)
        # 目标格应等于来源格的水平镜像
        flip_diff = float(np.abs(left - right[:, ::-1, :]).mean())
        self.assertLess(flip_diff, 3.0, f"翻转复制像素不匹配:{flip_diff}")
        # 源视频中右格是纯蓝,与左格镜像差异应明显(证明确实发生了复制)
        in_left = in_frame[:, :160, :].astype(np.int16)
        in_right = in_frame[:, 160:, :].astype(np.int16)
        raw_diff = float(np.abs(in_left - in_right[:, ::-1, :]).mean())
        self.assertGreater(raw_diff, 30.0, "源视频左右格差异不足,测试样本无效")


    def test_vfr_range_audio_alignment(self):
        """VFR + 范围裁剪:视频帧精确、音频采样级对齐。"""
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        vfr = os.path.join(td, "vfr.mp4")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
             "-frames:v", "40",
             "-vf", "setpts='if(lt(N,10), N/(10*TB), (1+(N-10)/30)/TB)'",
             "-c:v", "libx264", "-g", "100", "-bf", "2",
             "-pix_fmt", "yuv420p", vfr],
            capture_output=True, check=True)
        video = os.path.join(td, "vfr_audio.mp4")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-y",
             "-i", vfr, "-f", "lavfi", "-i",
             "sine=frequency=440:sample_rate=48000:duration=2",
             "-map", "0:v", "-map", "1:a",
             "-c:v", "copy", "-c:a", "aac", "-shortest", video],
            capture_output=True, check=True)
        idx = build_frame_index(video)
        start, end = 0.5, 1.5
        start_frame = idx.frame_at_or_after(start)
        end_frame = idx.frame_at_or_after(end)
        expected_frames = end_frame - start_frame
        pr = Project(video, 2.0)
        pr.process_range = [start, end]
        pr.process_range_frames = None   # 走 RenderController 自动建索引路径
        pr.grid = GridLayout.halves()
        pr.split_at(1.0, 30.0)
        pr.settings = EncoderSettings(encoder_mode="sw", preset="ultrafast",
                                      quality_mode="custom", custom_kbps=1000,
                                      output_dir=td)
        result = self._run_controller(pr)
        self.assertTrue(result["ok"], f"渲染失败:{result['msg']}")
        out = os.path.join(td, "vfr_audio_修复.mp4")
        c = av.open(out)
        try:
            vs = c.streams.video[0]
            frames = list(c.decode(vs))
            self.assertEqual(len(frames), expected_frames)
            video_dur = vs.duration * float(vs.time_base)
            self.assertAlmostEqual(video_dur, end - start, delta=0.05)
            astream = c.streams.audio[0]
            audio_dur = astream.duration * float(astream.time_base)
            self.assertAlmostEqual(audio_dur, video_dur, delta=0.05)
        finally:
            c.close()

    def test_vfr_overlay_patch(self):
        """VFR + overlay:select 取帧后补丁仍然正确覆盖。"""
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        video = os.path.join(td, "vfr_overlay.mp4")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=160x180:rate=30",
             "-f", "lavfi", "-i", "color=c=blue:size=160x180:rate=30",
             "-frames:v", "40",
             "-filter_complex",
             "[0:v][1:v]hstack=inputs=2,"
             "setpts='if(lt(N,10), N/(10*TB), (1+(N-10)/30)/TB)'[v]",
             "-map", "[v]", "-c:v", "libx264", "-g", "100", "-bf", "2",
             "-pix_fmt", "yuv420p", video],
            capture_output=True, check=True)
        media = probe(video)
        pr = Project(video, media.duration)
        pr.process_range = [0.0, media.duration]
        pr.process_range_frames = [0, media.frame_count or int(round(media.duration * media.fps))]
        pr.grid = GridLayout.halves()
        src = Rect(0.0, 0.0, 0.5, 1.0)
        dst = Rect(0.5, 0.0, 0.5, 1.0)
        pr.patches = [Patch(dst=dst, src=src, source_tile_idx=0,
                            dst_tile_idx=1, lock_align=False)]
        pr.settings = EncoderSettings(encoder_mode="sw", preset="ultrafast",
                                      quality_mode="custom", custom_kbps=1000,
                                      output_dir=td)
        result = self._run_controller(pr)
        self.assertTrue(result["ok"], f"渲染失败:{result['msg']}")
        out = os.path.join(td, "vfr_overlay_修复.mp4")

        def read_frame(path, t):
            c = av.open(path)
            try:
                st = c.streams.video[0]
                tb = float(st.time_base)
                target = int(round(t / tb))
                c.seek(target, stream=st, backward=True)
                for f in c.decode(st):
                    if f.pts is not None and f.pts >= target:
                        return f.to_ndarray(format="rgb24")
                return None
            finally:
                c.close()

        out_frame = read_frame(out, 1.1)
        in_frame = read_frame(video, 1.1)
        self.assertIsNotNone(out_frame)
        self.assertIsNotNone(in_frame)
        left = out_frame[:, :160, :].astype(np.int16)
        right = out_frame[:, 160:, :].astype(np.int16)
        self.assertLess(float(np.abs(left - right).mean()), 3.0,
                        "VFR overlay 补丁未覆盖目标格")
        in_right = in_frame[:, 160:, :].astype(np.int16)
        self.assertGreater(float(np.abs(left - in_right).mean()), 30.0,
                           "测试样本左右格差异不足")


if __name__ == "__main__":
    unittest.main()
