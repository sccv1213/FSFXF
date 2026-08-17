"""FrameIndex 纯逻辑测试:帧号 ↔ PTS 映射、半开区间结束边界。"""
from __future__ import annotations

import unittest
from fractions import Fraction

from core.frame_index import FrameIndex


class TestFrameIndex(unittest.TestCase):
    def _index(self):
        # 10fps 3 帧 + 30fps 2 帧,timebase 1/90000
        pts = [0, 9000, 18000, 27000, 30000]
        return FrameIndex(pts, Fraction(1, 90000), duration_ticks=33000)

    def test_seconds_and_nearest(self):
        idx = self._index()
        self.assertEqual(idx.frame_count, 5)
        self.assertAlmostEqual(idx.seconds(0), 0.0)
        self.assertAlmostEqual(idx.seconds(2), 0.2)
        self.assertAlmostEqual(idx.seconds(3), 0.3)
        self.assertEqual(idx.nearest_frame(0.21), 2)
        self.assertEqual(idx.nearest_frame(0.24), 2)  # 0.2 与 0.3 中更近 0.2
        self.assertEqual(idx.nearest_frame(1.0), 4)

    def test_end_ticks_half_open(self):
        idx = self._index()
        self.assertEqual(idx.end_ticks(0), 0)
        self.assertEqual(idx.end_ticks(3), 27000)   # [0,3) 结束于第 3 帧 pts
        self.assertEqual(idx.end_ticks(5), 33000)   # 整段结束于流 duration

    def test_frame_duration(self):
        idx = self._index()
        self.assertEqual(idx.frame_duration_ticks(0), 9000)
        self.assertEqual(idx.frame_duration_ticks(3), 3000)
        self.assertEqual(idx.frame_duration_ticks(4), 3000)   # duration - last pts

if __name__ == "__main__":
    unittest.main()
