"""集成测试(迁移自旧 test_seek.py):PyAV 精确 seek(用 ffmpeg 现场生成样例视频)。

验证 decoder 所用的 seek 做法:container.seek(backward=True) 落回关键帧,
再解码到目标帧,误差 ≤ 1 帧。
"""
import os
import shutil
import subprocess
import tempfile
import unittest

import numpy as np

import av


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


@unittest.skipUnless(_has_ffmpeg(), "需要 ffmpeg 生成样例视频")
class TestPyAVSeek(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        cls.video = os.path.join(cls._td.name, "sample.mp4")
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-y", "-f", "lavfi", "-i",
             "testsrc2=size=640x360:rate=30:duration=5",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "30", cls.video],
            capture_output=True)
        if r.returncode != 0:
            raise unittest.SkipTest("ffmpeg 生成样例视频失败")

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def _frame_at(self, t: float):
        """复刻 decoder 的 seek 逻辑,返回 (pts秒, ndarray)。"""
        container = av.open(self.video)
        try:
            stream = container.streams.video[0]
            tb = float(stream.time_base)
            target_pts = int(round(t / tb))
            container.seek(target_pts, stream=stream, backward=True)
            for frame in container.decode(stream):
                if frame.pts is not None and frame.pts >= target_pts:
                    return float(frame.pts) * tb, frame.to_ndarray(format="rgb24")
            return None, None
        finally:
            container.close()

    def test_seek_accuracy_within_one_frame(self):
        for t in (0.5, 1.7, 3.25, 4.9):
            pts, _ = self._frame_at(t)
            self.assertIsNotNone(pts, f"seek {t}s 无结果")
            self.assertLess(abs(pts - t), 1 / 30 + 0.01, f"seek {t}s 偏差过大:{pts}")

    def test_seek_returns_distinct_frames(self):
        pts1, a1 = self._frame_at(0.5)
        pts2, a2 = self._frame_at(3.0)
        self.assertGreater(pts2 - pts1, 2.0)
        diff = float(np.abs(a1.astype(np.int64) - a2.astype(np.int64)).mean())
        self.assertGreater(diff, 5.0, "不同时间的画面应明显不同")

    def test_consecutive_decode_continuity(self):
        """seek 后连续解码:相邻 pts 间隔 ≈ 1/30s。"""
        container = av.open(self.video)
        try:
            stream = container.streams.video[0]
            tb = float(stream.time_base)
            target_pts = int(round(1.0 / tb))
            container.seek(target_pts, stream=stream, backward=True)
            pts_list = []
            for frame in container.decode(stream):
                if frame.pts is not None and frame.pts >= target_pts:
                    pts_list.append(float(frame.pts) * tb)
                    if len(pts_list) >= 5:
                        break
            self.assertEqual(len(pts_list), 5)
            for i in range(1, len(pts_list)):
                self.assertAlmostEqual(pts_list[i] - pts_list[i - 1], 1 / 30, delta=0.01)
        finally:
            container.close()


if __name__ == "__main__":
    unittest.main()
