"""core 单测(迁移自旧 test_models.py,38 项语义全数保留):取偶、网格对齐映射、归属、校验、JSON。

差异(旧版 → 新版):
- GridLayout 保持 tiles 列表值语义(曾试验 edges 分界线列表,因格子间可带间隙而放弃);
  move_tile_edge → move_edge(先算后写);clone → copy
- Patch.grid 对象引用 → Patch.anchor_grid 字符串归属;rebuild_locked_src → realign_patches(grid_key)
- Project.validate() 委托 validation.validate(调用方式不变)
- 旧档迁移逻辑删除 → 改为"旧格式拒绝"(test_old_format_rejected 在 test_grid_edges)
"""
import os
import tempfile
import unittest

from core.grid import GridLayout, MIN_TILE
from core.planning import effective_scopes
from core.project import CopyRule, Patch, Project, Segment
from core.rect import Rect
from core.template import load_template, save_template


class TestRect(unittest.TestCase):
    def test_to_px_even_snapping(self):
        r = Rect(0.1, 0.2, 0.3, 0.4)
        x, y, w, h = r.to_px(1920, 1080)
        self.assertEqual((x % 2, y % 2, w % 2, h % 2), (0, 0, 0, 0))
        self.assertEqual((x, y), (192, 216))          # round(192), round(216)
        self.assertEqual((w, h), (576, 432))

    def test_to_px_clamp(self):
        r = Rect(-0.1, 1.0, 0.5, 0.5)
        x, y, w, h = r.to_px(1920, 1080)
        self.assertGreaterEqual(x, 0)
        self.assertLessEqual(x + w, 1920)
        self.assertLessEqual(y + h, 1080)

    def test_roundtrip_json(self):
        r = Rect(0.111, 0.222, 0.333, 0.444)
        r2 = Rect.from_dict(r.to_dict())
        self.assertEqual(r, r2)


class TestGridLayout(unittest.TestCase):
    def test_thirds(self):
        g = GridLayout.thirds()
        self.assertEqual(len(g.tiles), 3)
        self.assertEqual(g.tile_index_at(0.1, 0.5), 0)
        self.assertEqual(g.tile_index_at(0.5, 0.5), 1)
        self.assertEqual(g.tile_index_at(0.9, 0.5), 2)

    def test_quarters(self):
        g = GridLayout.quarters()
        self.assertEqual(len(g.tiles), 4)
        self.assertEqual(g.tile_index_at(0.1, 0.5), 0)
        self.assertEqual(g.tile_index_at(0.3, 0.5), 1)
        self.assertEqual(g.tile_index_at(0.55, 0.5), 2)
        self.assertEqual(g.tile_index_at(0.9, 0.5), 3)
        # 四格自动对齐:目标在格2(0.5-0.75) rel=0.2 → 源在格0 同相对位置
        dst = Rect(0.55, 0.1, 0.1, 0.2)
        src = g.align_rect(dst, 0)
        self.assertAlmostEqual(src.nx, 0.05, places=6)
        self.assertAlmostEqual(src.ny, 0.1, places=6)

    def test_align_rect_equal_thirds(self):
        """三格等宽:目标在中间格中央 → 源在左格中央,尺寸一致。"""
        g = GridLayout.thirds()
        dst = Rect(1 / 3 + 0.1, 0.1, 0.1, 0.2)   # 中格内
        src = g.align_rect(dst, 0)
        self.assertIsNotNone(src)
        self.assertAlmostEqual(src.nx, 0.1, places=6)
        self.assertAlmostEqual(src.ny, 0.1, places=6)
        self.assertAlmostEqual(src.nw, 0.1, places=6)
        self.assertAlmostEqual(src.nh, 0.2, places=6)

    def test_align_rect_with_gap(self):
        """带间隙/黑边的布局:相对坐标映射仍精确。"""
        g = GridLayout([Rect(0, 0, 0.3, 1), Rect(0.34, 0, 0.3, 1), Rect(0.68, 0, 0.3, 1)])
        dst = Rect(0.35, 0.2, 0.1, 0.3)          # 中格
        src = g.align_rect(dst, 0)
        rel = (0.35 - 0.34) / 0.3                # 0.0333
        self.assertAlmostEqual(src.nx, rel * 0.3, places=6)
        self.assertAlmostEqual(src.ny, 0.2, places=6)

    def test_align_rect_preserves_size(self):
        """目标格与来源格尺寸不同:位置按相对坐标映射,尺寸保持与 dst 一致。

        修复前:src 按格宽比缩放(src.nw = (0.1/0.2)*0.4 = 0.2),
        移动分界线后 src/dst 像素尺寸失配 → 渲染内容错位。
        """
        g = GridLayout([Rect(0, 0, 0.4, 1), Rect(0.5, 0, 0.2, 1)])
        dst = Rect(0.55, 0.1, 0.1, 0.2)          # 右格(小格)中央
        src = g.align_rect(dst, 0)
        rel = (0.55 - 0.5) / 0.2                 # 0.25
        self.assertAlmostEqual(src.nx, 0.25 * 0.4, places=6)
        self.assertAlmostEqual(src.nw, 0.1, places=6)   # 尺寸保持(修复前会缩放为 0.2)
        self.assertAlmostEqual(src.nh, 0.2, places=6)

    def test_align_rect_outside_returns_none(self):
        g = GridLayout.thirds()
        dst = Rect(0.05, 0.9, 0.9, 0.09)          # 跨格(中心在中间格,宽跨三格)
        src = g.align_rect(dst, 0)
        self.assertIsNotNone(src)                 # 按中心所在格映射,UI 层另有跨格警告

    def test_align_from_src_symmetry(self):
        """源模式对称映射:align_rect 与 align_from_src 互为逆运算。"""
        g = GridLayout.thirds()
        dst = Rect(1 / 3 + 0.1, 0.1, 0.1, 0.2)         # 中格
        src = g.align_rect(dst, 0)                      # 中格 → 左格
        back = g.align_from_src(src, 1)                 # 左格 → 中格
        self.assertIsNotNone(src)
        self.assertIsNotNone(back)
        for a, b in ((dst.nx, back.nx), (dst.ny, back.ny),
                     (dst.nw, back.nw), (dst.nh, back.nh)):
            self.assertAlmostEqual(a, b, places=6)

    def test_align_from_src_with_gap(self):
        g = GridLayout([Rect(0, 0, 0.3, 1), Rect(0.34, 0, 0.3, 1), Rect(0.68, 0, 0.3, 1)])
        src = Rect(0.05, 0.2, 0.1, 0.3)                # 左格
        dst = g.align_from_src(src, 1)
        rel = 0.05 / 0.3                                # 0.1667
        self.assertAlmostEqual(dst.nx, 0.34 + rel * 0.3, places=6)
        self.assertAlmostEqual(dst.ny, 0.2, places=6)

    def test_align_rect_clamps_overflow(self):
        """分界线移动后来源格比 dst 窄:src 收窄贴格右,不出分界线(回归)。

        用户场景:格子2|3 分界线左移 → 格子3 变宽;目标模式画 dst 到格子3
        最右边(宽超过来源格)→ 修复前 src 右边缘越过格子2|3 分界线,
        渲染时取到目标格像素(自我复制)。
        """
        g = GridLayout.thirds()
        g.move_edge(2, 0.5)                    # 格子2|3 分界线左移 → 格子2 变窄
        dst = Rect(0.5, 0.1, 0.4, 0.2)         # 画到格子3 最右边(宽 0.4)
        src = g.align_rect(dst, 1)             # 来源格 = 格子2(宽 0.167)
        self.assertIsNotNone(src)
        t = g.tiles[1]
        self.assertGreaterEqual(src.nx, t.nx - 1e-9)
        self.assertLessEqual(src.nx + src.nw, t.nx + t.nw + 1e-9,
                             "src 右边缘不得越过格界(分界线)")
        self.assertAlmostEqual(src.nw, 0.5 - 1 / 3, places=6,
                               msg="src 收窄到来源格内可用宽度")
        self.assertAlmostEqual(src.nh, 0.2, places=6)

    def test_align_from_src_clamps_overflow(self):
        """源模式对称:目标格比 src 窄 → dst 收窄贴格右,不出格界。"""
        g = GridLayout.thirds()
        g.move_edge(1, 0.5)                    # 格子1|2 分界线右移 → 格子2 变窄
        src = Rect(0.1, 0.1, 0.3, 0.2)        # 宽 0.3 > 格子2 宽 0.167
        dst = g.align_from_src(src, 1)
        self.assertIsNotNone(dst)
        t = g.tiles[1]
        self.assertGreaterEqual(dst.nx, t.nx - 1e-9)
        self.assertLessEqual(dst.nx + dst.nw, t.nx + t.nw + 1e-9,
                             "dst 右边缘不得越过格界")
        # 收窄到相对位置(rx=0.2)处的可用宽度,右边缘贴格右
        # (格子2 右界 = 原始 2/3,勿用 0.667 近似值)
        rel = 0.1 / 0.5                         # src 在格子1 内的相对位置
        self.assertAlmostEqual(dst.nw, (1 - rel) * (2 / 3 - 0.5), places=6)

    def test_realign_patches_syncs_dst_on_overflow(self):
        """realign_patches(分界线移动后重算):src 收窄时 dst 同步收窄,保持同尺寸。"""
        pr = Project("D:\\x.mp4", 100)
        pr.grid = g = GridLayout.thirds()
        p = Patch(dst=Rect(0.5, 0.1, 0.4, 0.2), src=Rect(0.05, 0.1, 0.4, 0.2),
                  source_tile_idx=0, dst_tile_idx=2, lock_align=True)
        pr.patches = [p]
        g.move_edge(2, 0.5)                    # 分界线左移:来源格(格子1)比 dst 窄
        pr.realign_patches()
        self.assertLessEqual(p.src.nx + p.src.nw, 1 / 3 + 1e-9,
                             "src 不得越过格子1 右界")
        self.assertAlmostEqual(p.src.nw, p.dst.nw, places=6,
                               msg="src/dst 必须同尺寸(像素复制不变式)")
        self.assertAlmostEqual(p.dst.nw, 1 / 3, places=6,
                               msg="dst 同步收窄到来源格可用宽度")

    def test_patch_dst_tile_idx_json_compat(self):
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        pr.patches = [Patch(dst=Rect(0.45, 0.2, 0.1, 0.1), src=Rect(0.1, 0.2, 0.1, 0.1),
                            source_tile_idx=0, dst_tile_idx=1)]
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "p.json")
            pr.save(path)
            pr2 = Project.load(path)
        self.assertEqual(pr2.patches[0].dst_tile_idx, 1)
        # 缺省字段兼容(新格式内)
        old = {"id": "abc", "anchor_grid": "project",
               "dst": {"nx": 0.4, "ny": 0.1, "nw": 0.1, "nh": 0.1},
               "src": {"nx": 0.1, "ny": 0.1, "nw": 0.1, "nh": 0.1},
               "source_tile_idx": 0, "lock_align": True}
        p_old = Patch.from_dict(old)
        self.assertEqual(p_old.dst_tile_idx, -1)
        self.assertEqual(p_old.anchor_grid, "project")

    def test_segment_grid_switch_isolated(self):
        """网格按片段:切布局只影响当前段,其他段补丁几何不变(#9 语义)。

        旧版靠对象身份(补丁引用段网格对象,切布局产生新对象 → 冻结);
        新版靠 anchor_grid 字符串归属(补丁锚定段,realign 按 grid_key 派发)。
        """
        pr = Project("D:\\x.mp4", 100)
        g3 = GridLayout.thirds()
        pr.grid = g3
        seg_a = Segment(id="A", start=0, end=50, patch_ids=["p1"], grid=g3)
        seg_b = Segment(id="B", start=50, end=100)      # 无自己网格 → 继承全局
        pr.segments = [seg_a, seg_b]
        patch = Patch(anchor_grid="segment:A",
                      dst=Rect(0.45, 0.2, 0.1, 0.1), src=Rect(0.1, 0.2, 0.1, 0.1),
                      source_tile_idx=0, dst_tile_idx=1)
        pr.patches = [patch]

        # 段B 切两格布局:只重算锚定段B 的补丁(无)→ 旧补丁不变
        seg_b.grid = GridLayout.halves()
        pr.realign_patches("segment:B")
        self.assertAlmostEqual(patch.dst.nx, 0.45, places=6)
        self.assertAlmostEqual(patch.src.nx, 0.1, places=6)
        # 拖线微调段A 的网格(原地改)→ 锚定它的补丁 src 重算(dst 不动)
        g3.move_edge(1, 0.40)
        pr.realign_patches("segment:A")
        self.assertAlmostEqual(patch.dst.nx, 0.45, places=6)
        self.assertNotAlmostEqual(patch.src.nx, 0.1, places=6)
        # 尺寸必须保持与 dst 一致(修复前 src.nw 会被按格宽比缩放成 0.15)
        self.assertAlmostEqual(patch.src.nw, 0.1, places=6,
                               msg="移线后 src 尺寸应保持 = dst")

    def test_move_edge_preserves_src_px_size(self):
        """移动分界线后 src 保持与 dst 同尺寸(像素级,主回归)。

        修复前:align_rect 按格宽比重算 src.nw → 移线后 src 像素宽 ≠ dst
        (三等分移中界线到 0.4:dst 192px、src 变 288px)。
        """
        pr = Project("D:\\x.mp4", 100)
        g = GridLayout.thirds()
        pr.grid = g
        dst = Rect(1 / 3 + 0.1, 0.1, 0.1, 0.2)          # 中格
        patch = Patch(dst=dst, src=Rect(0.1, 0.1, 0.1, 0.2),
                      source_tile_idx=0, dst_tile_idx=1)
        pr.patches = [patch]

        g.move_edge(1, 0.4)                        # 左格变宽、中格变窄
        pr.realign_patches("project")
        self.assertAlmostEqual(patch.src.nw, patch.dst.nw, places=6,
                               msg=f"src 尺寸应保持 = dst: {patch.src.nw} vs {patch.dst.nw}")
        self.assertAlmostEqual(patch.src.nh, patch.dst.nh, places=6)
        sw = patch.src.to_px(1920, 1080)[2]
        dw = patch.dst.to_px(1920, 1080)[2]
        self.assertEqual(sw, dw, f"像素级尺寸应相等: src {sw}px vs dst {dw}px")

    def test_validate_src_outside_tile(self):
        """src 越出来源格(源格过窄)→ validate 报错(不静默兜底)。"""
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout([Rect(0, 0, 0.2, 1), Rect(0.3, 0, 0.4, 1)])   # 左格窄
        dst = Rect(0.35, 0.1, 0.15, 0.2)       # 右格内
        src = Rect(0.1, 0.1, 0.2, 0.2)         # 0.1+0.2 = 0.3 > 0.2 → 越出左格
        pr.patches = [Patch(dst=dst, src=src, source_tile_idx=0, dst_tile_idx=1)]
        issues = pr.validate()
        self.assertTrue(any("源区域跨出来源格" in i for i in issues),
                        f"应报源跨格: {issues}")

    def test_grid_copy_independent(self):
        """copy 值拷贝:改拷贝不影响原对象(分割/按段隔离用)。"""
        g = GridLayout.thirds()
        c = g.copy()
        c.move_edge(1, 0.4)
        self.assertNotAlmostEqual(c.tiles[1].nx, g.tiles[1].nx, places=6)
        self.assertIsNot(c.tiles[0], g.tiles[0])

    def test_segment_grid_inherit_and_materialize(self):
        """段网格继承语义:None = 继承全局(值语义,无引用计数);物化后独立。"""
        pr = Project("D:\\x.mp4", 100)
        g3 = GridLayout.thirds()
        pr.grid = g3
        s0 = Segment(id="S0", start=0, end=10)          # 继承
        s1 = Segment(id="S1", start=10, end=20)         # 继承
        pr.segments = [s0, s1]
        self.assertIsNone(s0.grid)
        self.assertIs(pr.segment_grid(s0), pr.grid)
        # 拖段内分界线 → 物化克隆,只影响该段
        self.assertTrue(pr.materialize_segment_grid(s0))
        self.assertIsNot(s0.grid, pr.grid)
        self.assertEqual(s0.grid, pr.grid)              # 几何相等(拷贝)
        self.assertIsNone(s1.grid)                      # 其他段仍继承
        s0.grid.move_edge(1, 0.4)
        self.assertAlmostEqual(pr.grid.tiles[1].nx, 1 / 3, places=6)   # 全局不受影响

    def test_set_crop(self):
        """set_crop:唯一裁剪写入口,全局 + 所有段网格首/尾边同步。"""
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        pr.segments = [Segment(id="A", start=0, end=50, grid=GridLayout.thirds()),
                       Segment(id="B", start=50, end=100)]      # B 继承全局
        pr.set_crop(0.05, 0.95)
        self.assertAlmostEqual(pr.grid.crop_left, 0.05, places=6)
        self.assertAlmostEqual(pr.grid.crop_right, 0.95, places=6)
        self.assertAlmostEqual(pr.segments[0].grid.crop_left, 0.05, places=6)
        self.assertAlmostEqual(pr.segments[0].grid.crop_right, 0.95, places=6)
        # 非法值 → clamp([0,0.5]/[0.5,1] + 最小格宽 2%)(用干净网格断言)
        pr2 = Project("D:\\x.mp4", 100)
        pr2.grid = GridLayout.thirds()
        pr2.set_crop(0.6, 0.4)
        self.assertAlmostEqual(pr2.grid.crop_left, 1 / 3 - MIN_TILE, places=6)
        self.assertAlmostEqual(pr2.grid.crop_right, 2 / 3 + MIN_TILE, places=6)

    def test_project_crop_json_roundtrip(self):
        """crop 派生自网格首/尾格边界:save/load 后保持(JSON 不再存 crop 字段)。"""
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        pr.set_crop(0.05, 0.95)
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "p.json")
            pr.save(path)
            pr2 = Project.load(path)
        self.assertAlmostEqual(pr2.grid.crop_left, 0.05, places=6)
        self.assertAlmostEqual(pr2.grid.crop_right, 0.95, places=6)

    def test_validate_uses_patch_grid_and_sequence_name(self):
        """跨段补丁不误报(各用各的归属网格)+ 报错用序号命名(#9)。"""
        pr = Project("D:\\x.mp4", 100)
        g3 = GridLayout.thirds()
        pr.grid = g3
        seg_a = Segment(id="A", start=0, end=50, grid=g3)
        seg_b = Segment(id="B", start=50, end=100, grid=GridLayout.halves())
        pr.segments = [seg_a, seg_b]
        p1 = Patch(anchor_grid="segment:A", dst=Rect(0.45, 0.2, 0.1, 0.1),
                   src=Rect(0.1, 0.2, 0.1, 0.1), source_tile_idx=0, dst_tile_idx=1)
        p2 = Patch(anchor_grid="segment:B", dst=Rect(0.55, 0.2, 0.1, 0.1),
                   src=Rect(0.05, 0.2, 0.1, 0.1), source_tile_idx=0, dst_tile_idx=1)
        pr.patches = [p1, p2]
        # 三格补丁在两格网格下不再误报(各用各的网格验证)
        self.assertEqual(pr.validate(), [])
        # 非法补丁:报错文本用序号
        p1.dst = Rect(0.9, 0.9, 0.05, 0.05)   # 三格格3内,但 dst_tile_idx=1 → 不符
        msgs = pr.validate()
        self.assertTrue(any("补丁1" in m for m in msgs))
        self.assertFalse(any("补丁2" in m for m in msgs))
        self.assertFalse(any("bddf" in m or "0x" in m for m in msgs), "不应出现代码式补丁名")

    def test_patch_segment_grid_json(self):
        pr = Project("D:\\x.mp4", 100)
        g3 = GridLayout.thirds()
        pr.grid = g3
        pr.segments = [Segment(id="A", start=0, end=50, patch_ids=["a"], grid=g3)]
        pr.patches = [Patch(anchor_grid="segment:A", dst=Rect(0.45, 0.2, 0.1, 0.1),
                            src=Rect(0.1, 0.2, 0.1, 0.1), source_tile_idx=0)]
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "p.json")
            pr.save(path)
            pr2 = Project.load(path)
        self.assertEqual(len(pr2.segments[0].grid.tiles), 3)
        self.assertEqual(pr2.patches[0].anchor_grid, "segment:A")

    def test_copy_rule_and_segment_json(self):
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        pr.copy_rules = [CopyRule(source_tile_idx=0, target_tile_indices=[1, 2])]
        pr.segments = [Segment(id="A", start=0, end=50, patch_ids=["p1"],
                               copy_rule_ids=["r1"], grid=GridLayout.thirds())]
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "p.json")
            pr.save(path)
            pr2 = Project.load(path)
        self.assertEqual(len(pr2.copy_rules), 1)
        self.assertEqual(pr2.copy_rules[0].target_tile_indices, [1, 2])
        self.assertEqual(pr2.segments[0].copy_rule_ids, ["r1"])

    def test_effective_scopes_carries_copy_rules_and_grid(self):
        pr = Project("D:\\x.mp4", 100)
        pr.process_range = [0, 100]
        g3 = GridLayout.thirds()
        seg = Segment(id="A", start=20, end=60, patch_ids=["p1"], copy_rule_ids=["r1"], grid=g3)
        pr.segments = [seg]
        scopes = effective_scopes(pr)
        self.assertEqual([(s.start, s.end, s.copy_rule_ids, s.grid) for s in scopes],
                         [(0, 20, (), pr.grid), (20, 60, ("r1",), g3), (60, 100, (), pr.grid)])

    def test_validate_copy_rule(self):
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        pr.segments = [Segment(id="A", start=0, end=50, copy_rule_ids=["r1"],
                               grid=GridLayout.halves())]
        # 规则锚定两格段:目标格 2 超出锚定网格格数
        pr.copy_rules = [CopyRule(id="r1", anchor_grid="segment:A",
                                  source_tile_idx=0, target_tile_indices=[0, 2])]
        msgs = pr.validate()
        self.assertTrue(any("包含来源格" in m for m in msgs), msgs)    # 目标含来源格
        self.assertTrue(any("超出" in m for m in msgs), msgs)          # 锚定两格段仍用索引 2
        # 修正后无问题
        pr.copy_rules = [CopyRule(anchor_grid="segment:A",
                                  source_tile_idx=0, target_tile_indices=[1])]
        self.assertEqual(pr.validate(), [])

    def test_patch_extra_tiles(self):
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        p = Patch(dst=Rect(0.45, 0.2, 0.1, 0.1), src=Rect(0.1, 0.2, 0.1, 0.1),
                  source_tile_idx=0, dst_tile_idx=1, extra_tile_indices=[2])
        pr.patches = [p]
        self.assertEqual(p.target_tile_indices(), [1, 2])
        self.assertEqual(pr.validate(), [])
        p.extra_tile_indices = [0]   # 额外目标 = 来源格
        self.assertTrue(any("不能是来源格" in m for m in pr.validate()))

    def test_move_edge(self):
        g = GridLayout.thirds()
        g.move_edge(1, 0.4)
        self.assertAlmostEqual(g.tiles[0].nw, 0.4, places=6)
        self.assertAlmostEqual(g.tiles[1].nx, 0.4, places=6)
        # 修复:只有这条分界线移动,相邻格右边界(下一条分界线)不动
        self.assertAlmostEqual(g.tiles[1].nw, 2 / 3 - 0.4, places=6)
        self.assertAlmostEqual(g.tiles[2].nx, 2 / 3, places=6)
        self.assertAlmostEqual(g.tiles[2].nw, 1 / 3, places=6)

    def test_move_edge_isolated(self):
        """每条分界线独立移动:移第 2 条只影响相邻两格,其余分界线不动。"""
        g = GridLayout.quarters()
        g.move_edge(2, 0.6)
        self.assertAlmostEqual(g.tiles[0].nw, 0.25, places=6)   # 格1 不动
        self.assertAlmostEqual(g.tiles[1].nw, 0.35, places=6)   # 格2 变宽(左边界 0.25 不动)
        self.assertAlmostEqual(g.tiles[2].nx, 0.6, places=6)
        self.assertAlmostEqual(g.tiles[2].nw, 0.15, places=6)   # 格3 变窄(右边界 0.75 不动)
        self.assertAlmostEqual(g.tiles[3].nx, 0.75, places=6)   # 第 3 条分界线不动
        self.assertAlmostEqual(g.tiles[3].nw, 0.25, places=6)

    def test_crop_edges(self):
        """边缘线 = 最左/最右格子边界:移动限制 [0,0.5]/[0.5,1],其他分界线不动。"""
        g = GridLayout.thirds()
        self.assertEqual(g.crop_left, 0.0)
        self.assertEqual(g.crop_right, 1.0)
        g.move_edge(0, -5.0)              # 左边界不能 < 0
        self.assertEqual(g.tiles[0].nx, 0.0)
        g.move_edge(0, 0.7)               # 不能越过分界线1(格子最小 2%)
        self.assertAlmostEqual(g.tiles[0].nx, 1 / 3 - MIN_TILE, places=6)
        g.move_edge(0, 0.2)
        self.assertAlmostEqual(g.tiles[0].nx, 0.2)
        self.assertAlmostEqual(g.tiles[0].nw, 1 / 3 - 0.2)   # 右边界(分界线1)不动
        g.move_edge(3, 5.0)               # 右边界不能 > 1
        self.assertEqual(g.crop_right, 1.0)
        g.move_edge(3, 0.3)               # 不能越过分界线2(格子最小 2%)
        self.assertAlmostEqual(g.crop_right, 2 / 3 + MIN_TILE, places=6)
        g.move_edge(3, 0.8)
        self.assertAlmostEqual(g.crop_right, 0.8)
        self.assertAlmostEqual(g.tiles[2].nw, 0.8 - 2 / 3)  # 左边界(分界线2)不动

    def test_crop_edges_json(self):
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        pr.grid.move_edge(0, 0.05)        # 左边界 0.05
        pr.grid.move_edge(3, 0.95)        # 右边界 0.95
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "p.json")
            pr.save(path)
            pr2 = Project.load(path)
        self.assertAlmostEqual(pr2.grid.crop_left, 0.05)
        self.assertAlmostEqual(pr2.grid.crop_right, 0.95)
        self.assertEqual(len(pr2.grid.tiles), 3)

    def test_move_edge_clamped_to_video(self):
        """分界线不能移出视频边界(且格子最小 2%)。"""
        g = GridLayout.thirds()
        g.move_edge(1, -5.0)
        self.assertGreaterEqual(g.tiles[0].nw, 0.02)
        self.assertAlmostEqual(g.tiles[1].nx, 0.02, places=6)
        g.move_edge(2, 5.0)
        self.assertLessEqual(g.tiles[2].nx, 0.98)
        self.assertAlmostEqual(g.tiles[2].nw, 0.02, places=6)
        # 边界位置可精确微调
        g.move_edge(1, 0.3333)
        self.assertAlmostEqual(g.tiles[1].nx, 0.3333, places=4)


class TestProject(unittest.TestCase):
    def test_effective_scopes_implicit_full(self):
        """无用户片段 → 隐式全段:全部补丁/复制规则应用于整个处理范围(#6 语义)。"""
        pr = Project("D:\\x.mp4", 100)
        pr.process_range = [10, 90]
        pr.patches = [Patch(dst=Rect(0.45, 0.2, 0.1, 0.1), src=Rect(0.1, 0.2, 0.1, 0.1),
                            source_tile_idx=0)]
        pr.copy_rules = [CopyRule(id="r1", source_tile_idx=0, target_tile_indices=[1])]
        scopes = effective_scopes(pr)
        self.assertEqual(len(scopes), 1)
        s = scopes[0]
        self.assertEqual(s.key, "global")
        self.assertEqual((s.start, s.end), (10, 90))
        self.assertEqual(s.patch_ids, (pr.patches[0].id,))
        self.assertEqual(s.copy_rule_ids, ("r1",))

    def test_effective_scopes_gap_fill(self):
        pr = Project(duration=100)
        pr.process_range = [0, 100]
        pr.segments = [Segment(id="A", start=20, end=50, patch_ids=["a"]),
                       Segment(id="B", start=70, end=90, patch_ids=["b"])]
        scopes = effective_scopes(pr)
        self.assertEqual([(s.start, s.end, len(s.patch_ids), s.is_gap) for s in scopes],
                         [(0, 20, 0, True), (20, 50, 1, False), (50, 70, 0, True),
                          (70, 90, 1, False), (90, 100, 0, True)])

    def test_effective_scopes_clip_to_range(self):
        pr = Project(duration=100)
        pr.process_range = [10, 80]
        pr.segments = [Segment(id="A", start=0, end=60, patch_ids=["a"]),
                       Segment(id="B", start=90, end=100, patch_ids=["b"])]
        scopes = effective_scopes(pr)
        self.assertEqual([(s.start, s.end, s.patch_ids) for s in scopes],
                         [(10, 60, ("a",)), (60, 80, ())])

    def test_effective_scopes_overlap_resolved(self):
        pr = Project(duration=50)
        pr.process_range = [0, 50]
        pr.segments = [Segment(id="A", start=0, end=30, patch_ids=["a"]),
                       Segment(id="B", start=20, end=40, patch_ids=["b"])]
        scopes = effective_scopes(pr)          # 重叠段按顺序裁剪
        self.assertEqual([(s.start, s.end, s.patch_ids) for s in scopes],
                         [(0, 30, ("a",)), (30, 40, ("b",)), (40, 50, ())])

    def test_realign_patches(self):
        pr = Project(duration=100)
        pr.grid = GridLayout.thirds()
        p = Patch(dst=Rect(0.45, 0.2, 0.1, 0.1), src=Rect(0, 0, 0, 0),
                  source_tile_idx=0, lock_align=True)
        pr.patches = [p]
        pr.realign_patches("project")
        self.assertAlmostEqual(p.src.nx, 0.1167, places=3)

    def test_realign_skips_other_grid(self):
        """realign_patches(grid_key) 只重算 anchor 该网格的补丁。"""
        pr = Project(duration=100)
        pr.grid = GridLayout.thirds()
        seg_a = Segment(id="A", start=0, end=50, grid=GridLayout.thirds())
        pr.segments = [seg_a]
        p1 = Patch(anchor_grid="segment:A", dst=Rect(0.45, 0.2, 0.1, 0.1),
                   src=Rect(0, 0, 0, 0), source_tile_idx=0, lock_align=True)
        p2 = Patch(anchor_grid="project", dst=Rect(0.45, 0.2, 0.1, 0.1),
                   src=Rect(0, 0, 0, 0), source_tile_idx=0, lock_align=True)
        pr.patches = [p1, p2]
        pr.realign_patches("project")
        self.assertEqual(p1.src, Rect(0, 0, 0, 0))          # 不属 project → 不动
        self.assertNotAlmostEqual(p2.src.nx, 0.0, places=3)
        pr.realign_patches("segment:A")
        self.assertNotAlmostEqual(p1.src.nx, 0.0, places=3)

    def test_validate(self):
        pr = Project("D:\\视频\\直播.mp4", 100)
        pr.process_range = [0, 100]
        pr.grid = GridLayout.thirds()
        pr.patches = [Patch(dst=Rect(0.45, 0.2, 0.1, 0.1), src=Rect(0.1, 0.2, 0.1, 0.1),
                            source_tile_idx=0)]
        self.assertEqual(pr.validate(), [])
        pr.patches[0].dst = Rect(0.05, 0.9, 0.9, 0.09)   # 跨格
        self.assertTrue(any("拆成两个补丁" in w for w in pr.validate()))

    def test_save_load_roundtrip(self):
        pr = Project("D:\\视频\\直播.mp4", 3600)
        pr.process_range = [10, 3500]
        pr.grid = GridLayout.thirds()
        pr.patches = [Patch(dst=Rect(0.45, 0.2, 0.1, 0.1), src=Rect(0.1, 0.2, 0.1, 0.1),
                            source_tile_idx=0)]
        pr.segments = [Segment(id="A", start=10, end=3500, patch_ids=[pr.patches[0].id])]
        pr.settings.encoder_mode = "sw"
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "proj.json")
            pr.save(path)
            pr2 = Project.load(path)
        self.assertEqual(pr2.video_path, pr.video_path)
        self.assertEqual(pr2.process_range, pr.process_range)
        self.assertEqual(len(pr2.patches), 1)
        self.assertEqual(pr2.patches[0].id, pr.patches[0].id)
        self.assertEqual(pr2.segments[0].patch_ids, [pr.patches[0].id])
        self.assertEqual(pr2.settings.encoder_mode, "sw")
        self.assertEqual(pr2.grid.tiles[1].nx, 1 / 3)

    def test_split_at_implicit_full(self):
        """无段分割:两段各含全部 id 且 list 独立(共享 list 曾致两段都生效)。"""
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        pr.patches = [Patch(dst=Rect(0.45, 0.2, 0.1, 0.1), src=Rect(0.1, 0.2, 0.1, 0.1),
                            source_tile_idx=0)]
        pr.split_at(30, 60)
        self.assertEqual(len(pr.segments), 2)
        a, b = pr.segments
        self.assertNotEqual(a.id, b.id)
        self.assertEqual((a.start, a.end), (0, 30))
        self.assertEqual((b.start, b.end), (30, 100))
        self.assertIsNot(a.patch_ids, b.patch_ids)
        self.assertEqual(a.patch_ids, b.patch_ids)
        # 帧精确:32.7s @60fps → 32.7s(32.7*60=1962 整数帧)
        pr2 = Project("D:\\x.mp4", 100)
        pr2.split_at(32.7, 60)
        self.assertEqual(pr2.segments[0].end, round(32.7 * 60) / 60)

    def test_split_at_clones_grids(self):
        """有段分割:两段各自克隆网格,分界线编辑互不影响。"""
        pr = Project("D:\\x.mp4", 100)
        g3 = GridLayout.thirds()
        seg = Segment(id="A", start=0, end=50, patch_ids=["p1"], grid=g3)
        pr.segments = [seg]
        pr.split_at(20, 60)
        a, b = pr.segments
        self.assertIsNot(a.grid, b.grid)
        self.assertEqual(a.grid, b.grid)
        a.grid.move_edge(1, 0.4)
        self.assertAlmostEqual(b.grid.tiles[1].nx, 1 / 3, places=6)

    def test_split_at_in_gap_preserves_segments(self):
        """删首段后段外时间重新分割 → 空隙内插入两段,不覆盖现有分段(回归)。"""
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        p = Patch(dst=Rect(0.45, 0.2, 0.1, 0.1), src=Rect(0.1, 0.2, 0.1, 0.1),
                  source_tile_idx=0)
        pr.patches = [p]
        pr.segments = [Segment(id="B", start=30, end=100, patch_ids=[p.id])]   # 首段已删
        pr.split_at(10, 60)
        self.assertEqual(len(pr.segments), 3, "空隙分割应插入两段,原段保留")
        a, b, c = pr.segments
        self.assertEqual((a.start, a.end), (0, 10))
        self.assertEqual((b.start, b.end), (10, 30))
        self.assertEqual((c.start, c.end), (30, 100), "现有分段不得被覆盖")
        self.assertEqual(c.id, "B")
        self.assertEqual(a.patch_ids, [p.id], "空隙新段应含全部补丁")
        self.assertEqual(b.patch_ids, [p.id])

    def test_split_at_bounded_by_range(self):
        """永久分段条 = 处理范围边界:分割点限在范围内,段从 lo 到 hi。"""
        pr = self._mk()
        pr.process_range = [5, 45]
        # 范围内分割 → 第一段从永久条 lo 开始,第二段到 hi
        pr.split_at(15, 60)
        self.assertEqual(len(pr.segments), 2)
        a, b = pr.segments
        self.assertEqual((a.start, a.end), (5, 15), "第一段应从永久条(lo)开始")
        self.assertEqual((b.start, b.end), (15, 45), "第二段应到永久条(hi)结束")
        # 范围外无法添加分段
        pr2 = self._mk()
        pr2.process_range = [5, 45]
        pr2.split_at(50, 60)
        pr2.split_at(2, 60)
        pr2.split_at(5, 60)    # 恰在边界(永久条位置)→ 不分割
        pr2.split_at(45, 60)
        self.assertEqual(pr2.segments, [], "处理范围外/边界处应无法添加分段")
        # 空隙插入也以 lo/hi 为边界
        pr3 = self._mk()
        pr3.process_range = [5, 45]
        pr3.segments = [Segment(id="B", start=30, end=45)]
        pr3.split_at(10, 60)
        a, b, c = pr3.segments
        self.assertEqual((a.start, a.end), (5, 10), "空隙左侧应到永久条 lo")
        self.assertEqual((b.start, b.end), (10, 30))
        self.assertEqual((c.start, c.end), (30, 45))

    def test_split_at_no_segments_equivalent(self):
        """无段分割(隐式全段)→ 与旧'整体重建两段'等价。"""
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        pr.split_at(30, 60)
        self.assertEqual(len(pr.segments), 2)
        self.assertEqual(pr.segments[0].end, 30)
        self.assertEqual(pr.segments[1].start, 30)

    def _mk(self) -> Project:
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        p = Patch(dst=Rect(0.45, 0.2, 0.1, 0.1), src=Rect(0.1, 0.2, 0.1, 0.1),
                  source_tile_idx=0)
        pr.patches = [p]
        return pr

    def test_remove_segment_merge_two_becomes_global(self):
        """两段删一段 → 合并 = 处理范围 → 无段(全局模式,用户主场景)。"""
        pr = self._mk()
        pr.segments = [Segment(id="A", start=0, end=50, patch_ids=[pr.patches[0].id]),
                       Segment(id="B", start=50, end=100, patch_ids=[pr.patches[0].id])]
        pr.remove_segment_merge(0)      # 删首段
        self.assertEqual(pr.segments, [], "合并覆盖处理范围应回到无段全局模式")
        # 删末段同理
        pr.segments = [Segment(id="A", start=0, end=50), Segment(id="B", start=50, end=100)]
        pr.remove_segment_merge(1)
        self.assertEqual(pr.segments, [])

    def test_remove_segment_merge_middle_three(self):
        """三段删线(删 index 1)→ 合并左侧两段,右侧段保留(两侧两段合一)。"""
        pr = self._mk()
        pr.segments = [Segment(id="A", start=0, end=30), Segment(id="B", start=30, end=60),
                       Segment(id="C", start=60, end=100)]
        pr.remove_segment_merge(1)
        self.assertEqual(len(pr.segments), 2)
        a, b = pr.segments
        self.assertEqual((a.start, a.end), (0, 60), "删线右侧段 B 应与左邻 A 合并")
        self.assertEqual((b.start, b.end), (60, 100), "右邻 C 应保持独立")
        self.assertEqual(a.patch_ids, [], "合并段应用应清空")

    def test_remove_segment_merge_middle_four(self):
        """四段删 index 1 → 合并 A+B(应用清空),C、D 保留。"""
        pr = self._mk()
        pid = pr.patches[0].id
        pr.segments = [Segment(id="A", start=0, end=30, patch_ids=[pid]),
                       Segment(id="B", start=30, end=60, patch_ids=[pid]),
                       Segment(id="C", start=60, end=80),
                       Segment(id="D", start=80, end=100)]
        pr.remove_segment_merge(1)
        self.assertEqual(len(pr.segments), 3)
        a, c, d = pr.segments
        self.assertEqual((a.start, a.end), (0, 60))
        self.assertEqual(a.patch_ids, [], "合并段应用应清空")
        self.assertEqual(a.copy_rule_ids, [])
        self.assertEqual((c.start, c.end), (60, 80), "右邻 C 应保持独立")
        self.assertEqual((d.start, d.end), (80, 100))
        self.assertEqual(d.patch_ids, [], "未参与合并的段保留原状(D 原本未启用)")

    def test_remove_segment_merge_user_scenario(self):
        """用户场景回归:4 段删线3 再删线1 → 2 段(曾三连合并成全段全长)。

        开头-分段1-线1-分段2-线2-分段3-线3-分段4-结尾:删线3(分段3+4 合并)
        后删线1(分段1+2 合并)——两次都应只合并线两侧两段。
        """
        pr = self._mk()
        pr.segments = [Segment(id="A", start=0, end=25),
                       Segment(id="B", start=25, end=50),
                       Segment(id="C", start=50, end=75),
                       Segment(id="D", start=75, end=100)]
        pr.remove_segment_merge(3)      # 删线3 = 删分段4,与分段3 合并
        self.assertEqual([(s.start, s.end) for s in pr.segments],
                         [(0, 25), (25, 50), (50, 100)])
        pr.remove_segment_merge(1)      # 删线1 = 删分段2,与分段1 合并
        self.assertEqual([(s.start, s.end) for s in pr.segments],
                         [(0, 50), (50, 100)],
                         "删线1 应只合并分段1+分段2,不得吞掉分段3'")

    def test_remove_segment_merge_only_segment(self):
        """唯一段删除 → 无段(与旧行为一致)。"""
        pr = self._mk()
        pr.segments = [Segment(id="A", start=10, end=90)]
        pr.remove_segment_merge(0)
        self.assertEqual(pr.segments, [])
        # 补丁仍在全局列表(未删除)
        self.assertEqual(len(pr.patches), 1)

    def test_remove_segment_merge_gap_union(self):
        """空隙场景:段与段之间有间隙,删线(右侧段)与左邻合并含空隙。"""
        pr = self._mk()
        pr.segments = [Segment(id="A", start=0, end=20),
                       Segment(id="B", start=40, end=60),
                       Segment(id="C", start=90, end=100)]
        pr.remove_segment_merge(1)      # 删线 = 删 B,并入左邻 A(含间隙),C 保留
        self.assertEqual([(s.start, s.end) for s in pr.segments],
                         [(0, 60), (90, 100)])

    def test_toggle_in_segment(self):
        """段内 toggle 增删;全局模式取消 → 建显式全段(隐式→显式唯一入口)。"""
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        p1 = Patch(dst=Rect(0.45, 0.2, 0.1, 0.1), src=Rect(0.1, 0.2, 0.1, 0.1),
                   source_tile_idx=0)
        p2 = Patch(dst=Rect(0.45, 0.2, 0.1, 0.1), src=Rect(0.1, 0.2, 0.1, 0.1),
                   source_tile_idx=0)
        pr.patches = [p1, p2]
        # 有段:增删只影响该段
        seg = Segment(id="A", start=0, end=50)
        pr.segments = [seg]
        pr.toggle_patch_in_segment(p1.id, True, seg)
        self.assertEqual(seg.patch_ids, [p1.id])
        pr.toggle_patch_in_segment(p1.id, False, seg)
        self.assertEqual(seg.patch_ids, [])
        # 全局模式取消 → materialize_global_segment(其余全部 id,排除项)
        pr.segments = []
        pr.toggle_patch_in_segment(p2.id, False, None)
        self.assertEqual(len(pr.segments), 1)
        self.assertEqual(pr.segments[0].patch_ids, [p1.id])
        self.assertEqual(pr.segments[0].copy_rule_ids, [])

    def test_remove_patch_cleans_segments(self):
        pr = Project("D:\\x.mp4", 100)
        p1 = Patch()
        p2 = Patch()
        pr.patches = [p1, p2]
        pr.segments = [Segment(id="A", start=0, end=50, patch_ids=[p1.id, p2.id]),
                       Segment(id="B", start=50, end=100, patch_ids=[p1.id])]
        pr.remove_patch(p1.id)
        self.assertEqual([p.id for p in pr.patches], [p2.id])
        self.assertEqual(pr.segments[0].patch_ids, [p2.id])
        self.assertEqual(pr.segments[1].patch_ids, [])

    def test_repr_no_crash(self):
        """CopyRule/Segment 的 repr 不应崩溃。

        回归:Segment 的 repr 曾被误粘贴到 CopyRule 上(访问 self.start 崩)。
        """
        r = CopyRule(source_tile_idx=0, target_tile_indices=[1])
        self.assertIn("CopyRule", repr(r))
        s = Segment(id="A", start=0, end=10, patch_ids=["p1"], copy_rule_ids=["r1"])
        self.assertIn("0.0-10.0", repr(s))
        self.assertIn("1 补丁", repr(s))


class TestTemplate(unittest.TestCase):
    """补丁模板:保存/加载 roundtrip、应用模板到段/全局、批量克隆。"""

    def test_template_roundtrip_normalizes_anchor(self):
        """保存 → 加载一致;段锚定补丁保存时归一化为 project。"""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "t.fstpl.json")
            g = GridLayout.thirds()
            p = Patch(anchor_grid="segment:X", dst=Rect(0.45, 0.2, 0.1, 0.1),
                      src=Rect(0.1, 0.2, 0.1, 0.1), source_tile_idx=0, dst_tile_idx=1)
            r = CopyRule(source_tile_idx=0, target_tile_indices=[1, 2])
            save_template(g, [p], [r], path)
            tpl = load_template(path)
            self.assertEqual(tpl["grid"], g)
            self.assertEqual(tpl["patches"][0].anchor_grid, "project",
                             "模板补丁 anchor 应归一化为全局")
            self.assertEqual(tpl["patches"][0].dst, p.dst)
            self.assertEqual(tpl["copy_rules"][0].target_tile_indices, [1, 2])

    def test_apply_template_to_segment_replaces(self):
        """应用模板到段:替换该段补丁/复制/网格;克隆新 id + anchor=segment:id。"""
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        seg = Segment(id="A", start=0, end=50)
        pr.segments = [seg]
        old_p = Patch(anchor_grid="segment:A", dst=Rect(0.45, 0.2, 0.1, 0.1),
                      src=Rect(0.1, 0.2, 0.1, 0.1), source_tile_idx=0)
        pr.patches = [old_p]
        seg.patch_ids = [old_p.id]
        tpl = {
            "grid": GridLayout.quarters(),
            "patches": [Patch(dst=Rect(0.1, 0.1, 0.2, 0.2), src=Rect(0.6, 0.1, 0.2, 0.2),
                              source_tile_idx=0, dst_tile_idx=2, lock_align=True)],
            "copy_rules": [CopyRule(source_tile_idx=0, target_tile_indices=[1])],
        }
        pr.apply_template(tpl, seg)
        self.assertEqual(seg.grid, GridLayout.quarters(), "段网格应替换为模板网格(物化)")
        self.assertIsNone(pr.patch(old_p.id), "该段旧补丁应移除")
        self.assertEqual(len(pr.patches), 1)
        np = pr.patches[0]
        self.assertNotEqual(np.id, tpl["patches"][0].id, "模板补丁应克隆新 id")
        self.assertEqual(np.anchor_grid, "segment:A")
        self.assertEqual(seg.patch_ids, [np.id])
        self.assertEqual(len(pr.copy_rules), 1)
        self.assertEqual(seg.copy_rule_ids, [pr.copy_rules[0].id])

    def test_apply_template_global_replaces_all(self):
        """应用模板到全局:替换全部补丁/复制/网格,anchor=project。"""
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        pr.patches = [Patch(dst=Rect(0.45, 0.2, 0.1, 0.1),
                            src=Rect(0.1, 0.2, 0.1, 0.1), source_tile_idx=0)]
        pr.copy_rules = [CopyRule(source_tile_idx=0, target_tile_indices=[1])]
        tpl = {"grid": GridLayout.quarters(),
               "patches": [Patch(dst=Rect(0.1, 0.1, 0.2, 0.2),
                                 src=Rect(0.6, 0.1, 0.2, 0.2), source_tile_idx=0)],
               "copy_rules": []}
        pr.apply_template(tpl, None)
        self.assertEqual(pr.grid, GridLayout.quarters())
        self.assertEqual(len(pr.patches), 1)
        self.assertEqual(pr.patches[0].anchor_grid, "project")
        self.assertEqual(pr.copy_rules, [], "模板无复制规则 → 全局复制应清空")

    def test_clone_for_video_independent(self):
        """批量克隆:独立对象(改副本不影响源)、无分段、范围全长、anchor 归一化。"""
        pr = Project("D:\\src.mp4", 100)
        pr.grid = GridLayout.thirds()
        p = Patch(anchor_grid="project", dst=Rect(0.45, 0.2, 0.1, 0.1),
                  src=Rect(0.1, 0.2, 0.1, 0.1), source_tile_idx=0)
        pr.patches = [p]
        seg = Segment(id="A", start=0, end=50)
        pr.segments = [seg]
        seg.patch_ids = [p.id]
        pr2 = pr.clone_for_video("D:\\new.mp4", 200)
        self.assertEqual(pr2.video_path, "D:\\new.mp4")
        self.assertEqual(pr2.duration, 200)
        self.assertEqual(pr2.process_range, [0, 200])
        self.assertEqual(pr2.segments, [], "批量工程应无分段")
        self.assertEqual(len(pr2.patches), 1)
        self.assertEqual(pr2.patches[0].anchor_grid, "project")
        pr2.patches[0].dst = Rect(0.9, 0.9, 0.1, 0.1)
        self.assertEqual(pr.patches[0].dst.nx, 0.45, "副本修改不应影响源工程(深拷贝)")
        self.assertEqual(pr.segments, [seg], "源工程分段保留")


class TestMigrationRegistry(unittest.TestCase):
    def test_current_version_passes_registry(self):
        from core.project import migrate_project_dict
        pr = Project("D:\\x.mp4", 100)
        data = pr.to_dict()
        out = migrate_project_dict(data)
        self.assertIs(out, data)               # 当前版本无需迁移
        self.assertEqual(out["version"], 3)

    def test_v2_migrates_to_v3(self):
        from core.project import Project, migrate_project_dict
        v2 = {"format": "fenshenfu", "version": 2,
              "video_path": "D:\\x.mp4", "duration": 100,
              "process_range": [0, 100],
              "grid": {"tiles": [{"nx": 0, "ny": 0, "nw": 1 / 3, "nh": 1},
                                 {"nx": 1 / 3, "ny": 0, "nw": 1 / 3, "nh": 1},
                                 {"nx": 2 / 3, "ny": 0, "nw": 1 / 3, "nh": 1}]},
              "segments": [], "patches": [], "copy_rules": [], "settings": {}}
        out = migrate_project_dict(v2)
        self.assertEqual(out["version"], 3)
        pr = Project.from_dict(v2)
        self.assertEqual(pr.to_dict()["version"], 3)

    def test_missing_migration_path_rejected(self):
        from core.project import migrate_project_dict
        with self.assertRaises(ValueError) as cm:
            migrate_project_dict({"format": "fenshenfu", "version": 1})
        self.assertIn("旧版工程", str(cm.exception))

    def test_missing_version_rejected(self):
        from core.project import migrate_project_dict
        with self.assertRaises(ValueError):
            migrate_project_dict({"format": "fenshenfu"})


class TestFrameBoundaries(unittest.TestCase):
    def test_split_at_preserves_actual_pts_and_stores_frames(self):
        """分割时保留播放头实际 pts,同时保存帧号;不再用 round(t*fps)/fps 覆盖。"""
        pr = Project("D:\\x.mp4", 100)
        pr.process_range_frames = [0, 6000]
        pr.split_at(10.516, 60.0)
        segs = sorted(pr.segments, key=lambda s: s.start)
        self.assertEqual(len(segs), 2)
        self.assertAlmostEqual(segs[0].end, 10.516, places=9)
        self.assertAlmostEqual(segs[1].start, 10.516, places=9)
        self.assertEqual((segs[0].start_frame, segs[0].end_frame), (0, 631))
        self.assertEqual((segs[1].start_frame, segs[1].end_frame), (631, 6000))

    def test_scope_frame_count(self):
        pr = Project("D:\\x.mp4", 100)
        pr.process_range_frames = [0, 6000]
        pr.split_at(10.516, 60.0)
        scopes = effective_scopes(pr, 60.0)
        self.assertEqual(scopes[0].frame_count, 631)
        self.assertEqual(scopes[1].frame_count, 5369)

    def test_copy_rule_anchor_and_flip_roundtrip(self):
        pr = Project("D:\\x.mp4", 100)
        r = CopyRule(source_tile_idx=1, target_tile_indices=[2],
                     anchor_grid="segment:A", flip_horizontal=True)
        pr.copy_rules = [r]
        d = pr.to_dict()
        self.assertEqual(d["copy_rules"][0]["anchor_grid"], "segment:A")
        self.assertTrue(d["copy_rules"][0]["flip_horizontal"])
        self.assertEqual(pr.process_range_frames, None)
        pr2 = Project.from_dict(d)
        self.assertEqual(pr2.copy_rules[0].anchor_grid, "segment:A")
        self.assertTrue(pr2.copy_rules[0].flip_horizontal)


if __name__ == "__main__":
    unittest.main()
