"""新增结构性测试:归属解析三态、effective_scopes 无洞无重叠、scope 守卫与 anchor 赋值。"""
import random
import unittest

from core.grid import GridLayout
from core.planning import effective_scopes
from core.project import CopyRule, Patch, Project, Segment
from core.rect import Rect


def make_project() -> Project:
    pr = Project("D:\\x.mp4", 100)
    pr.process_range = [0, 100]
    pr.grid = GridLayout.thirds()
    return pr


class TestAnchorResolution(unittest.TestCase):
    def test_resolve_project(self):
        pr = make_project()
        self.assertIs(pr.resolve_grid("project"), pr.grid)

    def test_resolve_segment_with_grid(self):
        pr = make_project()
        g = GridLayout.quarters()
        seg = Segment(id="A", start=0, end=50, grid=g)
        pr.segments = [seg]
        self.assertIs(pr.resolve_grid("segment:A"), g)

    def test_resolve_segment_inherits(self):
        pr = make_project()
        seg = Segment(id="A", start=0, end=50)      # 无网格 → 继承
        pr.segments = [seg]
        self.assertIs(pr.resolve_grid("segment:A"), pr.grid)

    def test_resolve_stale_falls_back(self):
        """anchor 悬空(段已删除)→ 回退全局 + stale_anchors 标记(不静默)。"""
        pr = make_project()
        seg = Segment(id="A", start=0, end=50, grid=GridLayout.quarters())
        pr.segments = [seg]
        p = Patch(anchor_grid="segment:A", dst=Rect(0.45, 0.2, 0.1, 0.1),
                  src=Rect(0.1, 0.2, 0.1, 0.1), source_tile_idx=0)
        pr.patches = [p]
        pr.segments = []                            # 段被删除
        self.assertIs(pr.resolve_grid(p.anchor_grid), pr.grid)
        self.assertEqual(pr.stale_anchors(), {"segment:A"})
        # stale 补丁照常参与渲染/校验(用全局网格),不崩
        self.assertEqual(pr.validate(), [])

    def test_no_holes_no_overlap_property(self):
        """随机段集合:effective_scopes 对 process_range 覆盖无洞无重叠(性质测试)。"""
        rng = random.Random(3)
        for _ in range(50):
            pr = make_project()
            for _ in range(rng.randrange(0, 8)):
                a, b = sorted(rng.uniform(0, 100) for _ in range(2))
                pr.segments.append(Segment(id=f"S{rng.randrange(100000)}",
                                           start=a, end=b))
            scopes = effective_scopes(pr)
            cur = pr.process_range[0]
            for s in scopes:
                self.assertAlmostEqual(s.start, cur, places=6, msg=f"{scopes}")
                cur = s.end
            self.assertAlmostEqual(cur, pr.process_range[1], places=6, msg=f"{scopes}")

    def test_gap_scope_uses_global_grid(self):
        pr = make_project()
        pr.segments = [Segment(id="A", start=20, end=60)]
        scopes = effective_scopes(pr)
        gaps = [s for s in scopes if s.is_gap]
        self.assertEqual(len(gaps), 2)
        for g in gaps:
            self.assertIs(g.grid, pr.grid)
            self.assertEqual(g.patch_ids, ())
            self.assertEqual(g.copy_rule_ids, ())
            self.assertFalse(g.has_work)

    def test_scope_has_work(self):
        pr = make_project()
        p1 = Patch(dst=Rect(0.45, 0.2, 0.1, 0.1), src=Rect(0.1, 0.2, 0.1, 0.1),
                   source_tile_idx=0)
        pr.patches = [p1]
        pr.segments = [Segment(id="A", start=0, end=50, patch_ids=[p1.id])]
        seg_scope = next(s for s in effective_scopes(pr) if s.key == "segment:A")
        self.assertTrue(seg_scope.has_work)
        pr.segments = []                       # 全局模式:全部补丁生效
        self.assertTrue(effective_scopes(pr)[0].has_work)
        pr.patches = []
        pr.copy_rules = []
        self.assertFalse(effective_scopes(pr)[0].has_work)

    def test_global_scope_keys(self):
        """无段 → 单个 global scope(全补丁/全规则)。"""
        pr = make_project()
        p = Patch(dst=Rect(0.45, 0.2, 0.1, 0.1), src=Rect(0.1, 0.2, 0.1, 0.1),
                  source_tile_idx=0)
        r = CopyRule(source_tile_idx=0, target_tile_indices=[1])
        pr.patches, pr.copy_rules = [p], [r]
        scopes = effective_scopes(pr)
        self.assertEqual(len(scopes), 1)
        self.assertEqual(scopes[0].key, "global")
        self.assertEqual(scopes[0].grid_key, "project")
        self.assertEqual(scopes[0].patch_ids, (p.id,))
        self.assertEqual(scopes[0].copy_rule_ids, (r.id,))
        self.assertEqual((scopes[0].start, scopes[0].end), (0, 100))


if __name__ == "__main__":
    unittest.main()
