"""新增结构性测试:网格不变量、move_edge clamp 边界、to_px 一致性、旧格式拒绝。

这些性质测试把旧版"靠回归测试钉住"的网格行为变成结构性保证:
- 随机 move_edge 1000 次后 check_invariants 恒成立
- to_px 独立取偶:宽高只取决于 nw/nh(等宽矩形像素层必等宽)
"""
import random
import unittest

from core.grid import GridLayout, MIN_TILE
from core.project import Project
from core.rect import Rect


class TestEdgesInvariants(unittest.TestCase):
    def test_random_moves_keep_invariants(self):
        """随机 move_edge 1000 次后不变量恒成立(结构性保证)。"""
        rng = random.Random(42)
        for _ in range(50):
            g = GridLayout.quarters()
            for _ in range(1000):
                i = rng.randrange(len(g.tiles) + 1)   # 边界数 = 格数 + 1
                g.move_edge(i, rng.uniform(-0.5, 1.5))
            g.check_invariants()

    def test_move_edge_bounds_three_branches(self):
        """move_edge 三分支 clamp 边界值。"""
        g = GridLayout.thirds()
        # 分支0(左边缘线):clamp [0, min(0.5, 格1右-MIN_TILE)]
        g.move_edge(0, -1.0)
        self.assertEqual(g.crop_left, 0.0)
        g.move_edge(0, 0.6)                        # > 0.5 → 被 0.5 上限压回
        self.assertLessEqual(g.crop_left, 0.5)
        self.assertAlmostEqual(g.crop_left, 1 / 3 - MIN_TILE, places=6)
        # 分支 n(右边缘线):clamp [max(0.5, 末格左+MIN_TILE), 1.0]
        g.move_edge(3, 2.0)
        self.assertEqual(g.crop_right, 1.0)
        g.move_edge(3, 0.3)                        # < 0.5 → 被 0.5 下限顶回
        self.assertGreaterEqual(g.crop_right, 0.5)
        self.assertAlmostEqual(g.crop_right, 2 / 3 + MIN_TILE, places=6)
        # 分支内部:两侧格子各保 MIN_TILE(每次用干净网格,避免前序操作污染)
        g = GridLayout.thirds()
        g.move_edge(1, 0.0)                    # 分界线1 下限:格1右 - MIN = 0 + 0.02
        self.assertAlmostEqual(g.tiles[1].nx, 0.0 + MIN_TILE, places=6)
        g2 = GridLayout.thirds()
        g2.move_edge(2, 1.0)                   # 分界线2 上限:末格右(1.0) - MIN
        self.assertAlmostEqual(g2.tiles[2].nx, 1.0 - MIN_TILE, places=6)
        g2.check_invariants()

    def test_move_edge_adjacent_only(self):
        """只移动一条边界:其他分界线(含相邻格的另一侧)不动。"""
        g = GridLayout.quarters()
        before = [(t.nx, t.nw) for t in g.tiles]
        g.move_edge(2, 0.55)
        for i, (nx, nw) in enumerate(before):
            t = g.tiles[i]
            if i == 1:
                self.assertAlmostEqual(t.nw, 0.55 - 0.25, places=6)   # 格2 变宽
            elif i == 2:
                self.assertAlmostEqual(t.nx, 0.55, places=6)          # 格3 左移
                self.assertAlmostEqual(t.nw, 0.75 - 0.55, places=6)
            else:
                self.assertEqual((t.nx, t.nw), (nx, nw), f"格{i+1} 不应变化")


class TestSetCropSync(unittest.TestCase):
    def test_set_crop_syncs_all_grids(self):
        """set_crop 后全局 + 所有段网格首/尾边相等(边缘线全程统一)。"""
        from core.project import Segment
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        pr.segments = [
            Segment(id="A", start=0, end=50, grid=GridLayout.thirds()),
            Segment(id="B", start=50, end=100, grid=GridLayout.quarters()),
            Segment(id="C", start=20, end=30),          # 继承全局
        ]
        for lo, hi in ((0.05, 0.95), (0.0, 1.0), (0.2, 0.8)):
            pr.set_crop(lo, hi)
            grids = [pr.grid] + [s.grid for s in pr.segments if s.grid is not None]
            for g in grids:
                self.assertAlmostEqual(g.crop_left, pr.grid.crop_left, places=6)
                self.assertAlmostEqual(g.crop_right, pr.grid.crop_right, places=6)
                g.check_invariants()

    def test_presets_are_valid(self):
        for g in (GridLayout.thirds(), GridLayout.halves(), GridLayout.quarters()):
            g.check_invariants()
            self.assertGreaterEqual(len(g.tiles), 2)


class TestToPxConsistency(unittest.TestCase):
    def test_px_consistency_property(self):
        """to_px 独立取偶:x/y/w/h 全偶、宽高只取决于 nw/nh、矩形不越出画面。

        等宽性 = 同一 round(nw*fw) 输入必然同输出(结构性保证,测试等宽断言见下)。
        """
        rng = random.Random(7)
        for _ in range(2000):
            nx, ny = rng.random(), rng.random()
            nw = rng.random() * (1 - nx)
            nh = rng.random() * (1 - ny)
            fw, fh = rng.randrange(2, 4000), rng.randrange(2, 4000)
            x, y, w, h = Rect(nx, ny, nw, nh).to_px(fw, fh)
            rw = int(round(nw * fw)) & ~1
            rh = int(round(nh * fh)) & ~1
            if rw <= 0 or rh <= 0:                 # 零尺寸 = 停用标记
                self.assertEqual((x, y, w, h), (0, 0, 0, 0))
                continue
            self.assertEqual((x % 2, y % 2, w % 2, h % 2), (0, 0, 0, 0))
            self.assertEqual(w, rw, f"宽应只取决于 nw: {nw}@{fw}")
            self.assertEqual(h, rh, f"高应只取决于 nh: {nh}@{fh}")
            self.assertGreaterEqual(x, 0)
            self.assertLessEqual(x + w, fw)
            self.assertLessEqual(y + h, fh)

    def test_equal_width_rects_equal_px_width(self):
        """归一化等宽的两个矩形,任意分辨率下像素宽必等(补丁 src/dst 同尺寸保证)。"""
        rng = random.Random(11)
        for _ in range(500):
            nw = rng.random() * 0.5 + 0.01
            a = Rect(rng.random() * 0.5, 0.1, nw, 0.3)
            b = Rect(0.5 + rng.random() * 0.2, 0.2, nw, 0.4)
            fw = rng.randrange(2, 4000)
            aw = a.to_px(fw, 1080)[2]
            bw = b.to_px(fw, 1080)[2]
            self.assertEqual(aw, bw, f"等宽矩形像素宽不等: {nw}@{fw} → {aw} vs {bw}")


class TestFormatRejected(unittest.TestCase):
    def test_old_format_rejected(self):
        """旧版(version=1 / tiles 表示)工程显式拒绝,不静默迁移。"""
        old = {"version": 1, "video_path": "x.mp4", "duration": 100,
               "process_range": [0, 100],
               "grid": {"tiles": [{"nx": 0, "ny": 0, "nw": 1 / 3, "nh": 1}]},
               "segments": [], "patches": [], "settings": {}}
        with self.assertRaises(ValueError) as cm:
            Project.from_dict(old)
        self.assertIn("不兼容", str(cm.exception))
        # 缺 format 字段也一样拒绝
        nofmt = {"version": 2, "video_path": "x.mp4", "duration": 100,
                 "process_range": [0, 100], "grid": {"edges": [0, 1]},
                 "segments": [], "patches": [], "settings": {}}
        with self.assertRaises(ValueError):
            Project.from_dict(nofmt)


if __name__ == "__main__":
    unittest.main()
