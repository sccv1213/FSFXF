"""AppState 工作流层单测:Stage 转移、current_scope 派生、校验门、QSettings 隔离。"""
import os
import unittest
from unittest import mock
from fractions import Fraction

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from core.frame_index import FrameIndex
from core.grid import GridLayout
from core.planning import effective_scopes
from core.project import CopyRule, Patch, Project, Rect, Segment
from state.app_state import AppState, EditMode, Stage


def make_state() -> AppState:
    return AppState(settings_org="FenshenFuTest", settings_app="test")


class TestStageAndMode(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])

    def test_initial_stage_no_video(self):
        s = make_state()
        self.assertEqual(s.stage, Stage.NO_VIDEO)
        self.assertIsNone(s.project)
        self.assertFalse(s.can_render()[0], "无视频不可渲染")

    def test_set_video_enters_ready(self):
        s = make_state()
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        s.set_video(pr, None)
        self.assertEqual(s.stage, Stage.READY)
        self.assertIs(s.project, pr)
        self.assertTrue(s.can_render()[0], "有视频无补丁应可渲染")

    def test_mode_switch(self):
        s = make_state()
        s.set_mode(EditMode.DST)
        self.assertEqual(s.mode, EditMode.DST)
        s.set_mode(EditMode.DST)          # 幂等
        self.assertEqual(s.mode, EditMode.DST)


class TestCurrentScope(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])

    def _project(self) -> Project:
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        pr.patches = [Patch(dst=Rect(0.45, 0.1, 0.1, 0.2), src=Rect(0.1, 0.1, 0.1, 0.2),
                            source_tile_idx=0)]
        pr.copy_rules = [CopyRule(source_tile_idx=0, target_tile_indices=[1])]
        return pr

    def test_global_scope_when_no_segments(self):
        s = make_state()
        pr = self._project()
        s.set_video(pr, None)
        scope = s.current_scope()
        self.assertEqual(scope.key, "global")
        self.assertEqual(scope.patch_ids, (pr.patches[0].id,))
        self.assertEqual(scope.copy_rule_ids, (pr.copy_rules[0].id,))
        self.assertTrue(s.is_global_mode())
        self.assertEqual(s.enabled_patch_ids(), {pr.patches[0].id})

    def test_segment_and_gap_scopes(self):
        s = make_state()
        pr = self._project()
        seg = Segment(id="A", start=20, end=60, patch_ids=[pr.patches[0].id])
        pr.segments = [seg]
        s.set_video(pr, None)
        s.set_current_time(30)             # 段内
        self.assertEqual(s.current_scope().key, "segment:A")
        self.assertEqual(s.enabled_patch_ids(), {pr.patches[0].id})
        s.set_current_time(10)             # 间隙
        self.assertTrue(s.current_scope().is_gap)
        self.assertEqual(s.enabled_patch_ids(), set(), "间隙内无补丁生效")

    def test_user_segment_at(self):
        s = make_state()
        pr = self._project()
        seg = Segment(id="A", start=20, end=60)
        pr.segments = [seg]
        s.set_video(pr, None)
        s.set_current_time(30)
        self.assertIs(s.current_user_segment(), seg)
        s.set_current_time(10)             # 间隙 → None(与旧版一致)
        self.assertIsNone(s.current_user_segment())

    def test_toggle_patch_global_creates_segment(self):
        """全局模式取消补丁 → 建显式全段(排除该项),隐式→显式唯一入口。"""
        s = make_state()
        pr = self._project()
        s.set_video(pr, None)
        s.toggle_patch(pr.patches[0].id, False)
        self.assertEqual(len(pr.segments), 1)
        self.assertEqual(pr.segments[0].patch_ids, [])
        self.assertEqual(pr.segments[0].copy_rule_ids, [pr.copy_rules[0].id])
        self.assertFalse(s.is_global_mode())

    def test_toggle_patch_in_segment(self):
        s = make_state()
        pr = self._project()
        seg = Segment(id="A", start=20, end=60)
        pr.segments = [seg]
        s.set_video(pr, None)
        s.set_current_time(30)
        s.toggle_patch(pr.patches[0].id, True)
        self.assertEqual(seg.patch_ids, [pr.patches[0].id])
        s.toggle_patch(pr.patches[0].id, False)
        self.assertEqual(seg.patch_ids, [])


class TestModelActions(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])

    def test_split_and_scope_change(self):
        s = make_state()
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        s.set_video(pr, None)
        changed = []
        s.scope_changed.connect(lambda: changed.append(1))
        s.split_at(30, 60)
        self.assertEqual(len(pr.segments), 2)
        self.assertTrue(changed)

    def test_set_crop_syncs_grids(self):
        s = make_state()
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        seg = Segment(id="A", start=0, end=50, grid=GridLayout.thirds())
        pr.segments = [seg]
        s.set_video(pr, None)
        s.set_crop(0.1, 0.9)
        self.assertAlmostEqual(pr.grid.crop_left, 0.1, places=6)
        self.assertAlmostEqual(seg.grid.crop_left, 0.1, places=6)

    def test_move_inner_edge_materializes(self):
        s = make_state()
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        seg = Segment(id="A", start=0, end=50)     # 继承全局
        pr.segments = [seg]
        s.set_video(pr, None)
        s.set_current_time(10)
        s.move_inner_edge(1, 0.4)
        self.assertIsNotNone(seg.grid, "应物化独立网格")
        self.assertIsNot(seg.grid, pr.grid)
        self.assertAlmostEqual(seg.grid.tiles[1].nx, 0.4, places=6)
        self.assertAlmostEqual(pr.grid.tiles[1].nx, 1 / 3, places=6, msg="全局不受影响")

    def test_set_layout_only_current_segment(self):
        s = make_state()
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        seg = Segment(id="A", start=0, end=50)
        pr.segments = [seg]
        s.set_video(pr, None)
        s.set_current_time(10)
        s.set_layout(GridLayout.halves())
        self.assertEqual(len(seg.grid.tiles), 2)
        self.assertEqual(len(pr.grid.tiles), 3, "全局网格不受影响")
        # 无段:全局
        pr2 = Project("D:\\y.mp4", 100)
        pr2.grid = GridLayout.thirds()
        s.set_video(pr2, None)
        s.set_layout(GridLayout.quarters())
        self.assertEqual(len(pr2.grid.tiles), 4)

    def test_set_layout_anchor_resolves_new_grid(self):
        """切布局不重算几何,但锚定补丁/复制解析到新网格(新语义)。"""
        s = make_state()
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        seg = Segment(id="A", start=0, end=50)
        pr.segments = [seg]
        p = Patch(anchor_grid="segment:A",
                  dst=Rect(0.45, 0.1, 0.1, 0.1), src=Rect(0.1, 0.1, 0.1, 0.1),
                  source_tile_idx=0)
        r = CopyRule(anchor_grid="segment:A", source_tile_idx=0,
                     target_tile_indices=[1])
        pr.patches, pr.copy_rules = [p], [r]
        s.set_video(pr, None)
        s.set_current_time(10)
        dst_before, src_before = p.dst, p.src
        s.set_layout(GridLayout.halves())
        self.assertEqual(len(seg.grid.tiles), 2)
        self.assertEqual(p.dst, dst_before)
        self.assertEqual(p.src, src_before)
        self.assertIs(pr.resolve_grid(p.anchor_grid), seg.grid)
        self.assertIs(pr.resolve_grid(r.anchor_grid), seg.grid)

    def test_scope_cache_reused_until_project_revision_changes(self):
        """scope 缓存:播放中多次查询不重建;结构变化后重建一次。"""
        s = make_state()
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        s.set_video(pr, None)
        with mock.patch("state.app_state.effective_scopes",
                        wraps=effective_scopes) as m:
            self.assertEqual(m.call_count, 0)
            for _ in range(5):
                s.set_current_time(10.0)
            self.assertEqual(m.call_count, 1, "同 revision 不应重建")
            pr.split_at(50.0, 30.0)      # bump revision
            s.current_scope()
            self.assertEqual(m.call_count, 2, "结构变化应只重建一次")
            s.current_scope()
            self.assertEqual(m.call_count, 2)

    def test_can_render_validates_once(self):
        """can_render 曾重复 validate;现在只调用一次。"""
        s = make_state()
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        s.set_video(pr, None)
        with mock.patch.object(pr, "validate", wraps=pr.validate) as m:
            ok, issues = s.can_render()
        self.assertTrue(ok)
        self.assertEqual(issues, [])
        self.assertEqual(m.call_count, 1)

    def test_can_render_gates(self):
        s = make_state()
        pr = Project("D:\\x.mp4", 100)
        pr.grid = GridLayout.thirds()
        pr.patches = [Patch(dst=Rect(0.45, 0.1, 0.1, 0.2), src=Rect(0.1, 0.1, 0.1, 0.2),
                            source_tile_idx=0)]
        s.set_video(pr, None)
        ok, issues = s.can_render()
        self.assertTrue(ok)
        pr.patches[0].dst = Rect(0.05, 0.9, 0.9, 0.09)   # 跨格
        ok, issues = s.can_render()
        self.assertFalse(ok)
        self.assertTrue(any("拆成两个补丁" in i for i in issues))

    def test_frame_index_snaps_vfr_boundaries(self):
        """FrameIndex 就绪后:范围/分割边界吸附到真实帧 PTS。"""
        s = make_state()
        pr = Project("D:\\vfr.mp4", 2.033333)
        pr.grid = GridLayout.thirds()
        s.set_video(pr, None)
        idx = FrameIndex([0, 9000, 18000, 27000, 30000],
                         Fraction(1, 90000), duration_ticks=33000)
        s.set_frame_index(idx)
        self.assertEqual(pr.process_range_frames, [0, 5])
        self.assertEqual(pr.process_range_pts_ticks, [0, 33000])
        s.split_at(0.24, 14.7)
        segs = sorted(pr.segments, key=lambda x: x.start)
        self.assertAlmostEqual(segs[0].end, 0.2, places=9)
        self.assertAlmostEqual(segs[1].start, 0.2, places=9)
        self.assertEqual(segs[0].end_frame, 2)
        self.assertEqual(segs[1].start_frame, 2)

    def test_settings_persist_isolated(self):
        """QSettings 隔离注入:测试键不污染真实 org 的 last_dir/volume。"""
        real_v0 = AppState(settings_org="FenshenFu",
                           settings_app="fenshenfu").volume()
        s = make_state()
        s.set_volume(42)
        s.set_last_dir("D:\\tmp")
        s2 = make_state()
        self.assertEqual(s2.volume(), 42)
        self.assertEqual(s2.last_dir(), "D:\\tmp")
        # 真实 org 键不受测试写入影响(前后对比,初始值可能是用户实际设置)
        self.assertEqual(AppState(settings_org="FenshenFu",
                                  settings_app="fenshenfu").volume(),
                         real_v0, "测试键不应污染真实 org")


if __name__ == "__main__":
    unittest.main()
