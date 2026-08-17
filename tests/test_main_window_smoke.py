"""主窗口冒烟测试(迁移自旧 8 项):打开视频/补丁流程/分割/守卫/队列门/网格同步。

接口变化:win._project → win._state.project;_split_at → _on_split;
effective_segments → effective_scopes;帧视图白盒 → _interactor。
"""
import os
import shutil
import subprocess
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from core.grid import GridLayout
from core.planning import effective_scopes
from core.project import Patch, Project, Rect, Segment
from ui.main_window import MainWindow


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


@unittest.skipUnless(_has_ffmpeg(), "需要 ffmpeg")
class TestMainWindowSmoke(unittest.TestCase):

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

    def _wait(self, ms=4000):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def _open(self) -> MainWindow:
        win = MainWindow(settings_org="FenshenFuTest", settings_app="test")
        win.show()
        self._wait(300)
        win._load_video(self.video)
        self._wait(1200)
        return win

    def _close(self, win: MainWindow) -> None:
        win._worker.stop()
        win.close()

    def test_open_video_and_patch_flow(self):
        win = self._open()
        proj = win._state.project
        self.assertIsNotNone(proj, "打开视频未创建工程")
        self.assertEqual(proj.duration, 3.0)

        win._player.seek(1.5)
        self._wait(1200)
        dst = Rect(0.4, 0.1, 0.12, 0.2)
        patch = Patch(dst=dst, src=proj.grid.align_rect(dst, 0) or Rect(0, 0, 0, 0),
                      source_tile_idx=0)
        win._on_patch_created(patch)
        self.assertEqual(len(proj.patches), 1)
        # 画补丁不自动创建片段,隐式全段生效
        self.assertEqual(proj.segments, [])
        self.assertEqual(effective_scopes(proj)[0].patch_ids, (patch.id,))
        # 自动对齐:源矩形应在左格
        self.assertAlmostEqual(patch.src.nx, dst.nx - 1 / 3, delta=0.001)

        # 分割片段:无段时创建两段(各含全部)
        win._on_split(1.0)
        self.assertEqual(len(proj.segments), 2)
        self.assertEqual(proj.segments[0].patch_ids, [patch.id])

        # 保存/重载工程
        proj_path = os.path.join(self._td, "proj.json")
        proj.save(proj_path)
        reloaded = Project.load(proj_path)
        self.assertEqual(len(reloaded.patches), 1)
        self.assertEqual(len(reloaded.segments), 2)
        self._close(win)

    def test_validate_gates_queue(self):
        win = self._open()
        proj = win._state.project
        ok, issues = win._state.can_render()
        self.assertTrue(ok, f"无补丁工程应可渲染:{issues}")
        self.assertEqual(proj.validate(), [])
        self._close(win)

    def test_queue_progress_updates_ui(self):
        """进度事件 → 队列进度条实时刷新(回归:只更新状态栏文字,进度条不动)。"""
        win = self._open()
        win._queue_add_current()
        job = win._controller.jobs()[0]
        job.progress = 0.5                # 真实流程:controller 先更新 job 再 emit
        win._on_job_progress(job.id, 0.5)
        self.app.processEvents()
        bar = win._queue_widget._table.cellWidget(0, 2)
        self.assertGreaterEqual(bar.value(), 50, "进度条应实时更新")
        self._close(win)

    def test_queue_validate_cleared_input(self):
        """生效的编码输入框被清空 → 添加队列提示且不入队(回归)。"""
        from unittest import mock

        win = self._open()
        # 默认 quality_mode = match → 生效框是码率系数
        win._info._factor_spin.lineEdit().setText("")   # 清空(编辑中)
        with mock.patch("ui.main_window.show_error") as err:
            win._queue_add_current()
        self.assertEqual(err.call_count, 1, "生效输入框清空应提示")
        self.assertIn("编码参数", str(err.call_args))
        self.assertEqual(win._controller.jobs(), [], "校验失败不应入队")
        self._close(win)

    def test_queue_remove_selected_updates_ui(self):
        """移除选中 → 队列删行;未选中/不可移除有提示(回归:无反应)。"""
        win = self._open()
        win._queue_add_current()
        win._queue_add_current()
        self.app.processEvents()
        self.assertEqual(win._queue_widget._table.rowCount(), 2)
        win._queue_widget._table.selectRow(0)
        win._queue_remove_selected()
        self.app.processEvents()
        self.assertEqual(win._queue_widget._table.rowCount(), 1, "移除选中后应删行")
        self.assertEqual(len(win._controller.jobs()), 1)
        # 未选中:状态栏提示(不静默)
        win._queue_remove_selected()
        self.assertIn("请先选中", win._state_label.text())
        self._close(win)

    def test_segment_patch_count_updates(self):
        """补丁增删启停 → 片段页补丁数实时变化(回归:只刷补丁/复制页)。"""
        win = self._open()
        win._on_split(1.0)
        dst = Rect(0.4, 0.1, 0.12, 0.2)
        patch = Patch(dst=dst, src=Rect(0.066, 0.1, 0.12, 0.2), source_tile_idx=0)
        win._on_patch_created(patch)          # 新增 → scope_changed → refresh_segments
        self.app.processEvents()
        self.assertEqual(win._patch_panel._seg_table.item(1, 2).text(), "1",
                         "新增补丁后片段页补丁数应更新")
        win._on_patch_toggled(patch.id, False)   # 停用(播放头在段 0 内)
        self.app.processEvents()
        self.assertEqual(win._patch_panel._seg_table.item(1, 2).text(), "0",
                         "停用补丁后补丁数应实时变化")
        self._close(win)

    def test_segment_copy_count_updates(self):
        """复制规则增删 → 片段页复制数列实时变化。"""
        win = self._open()
        win._on_split(1.0)
        win._on_copy_add()          # 播放头在段 0 → 复制规则加入段 0
        self.app.processEvents()
        self.assertEqual(win._patch_panel._seg_table.item(1, 3).text(), "1",
                         "新增复制规则后复制数应更新")
        rid = win._state.project.copy_rules[0].id
        win._on_copy_deleted(rid)
        self.app.processEvents()
        self.assertEqual(win._patch_panel._seg_table.item(1, 3).text(), "0",
                         "删除复制规则后复制数应实时变化")
        self._close(win)

    def test_copy_flip_checkbox_updates_rule(self):
        """复制规则行右侧左右翻转勾选 → CopyRule.flip_horizontal 同步。"""
        win = self._open()
        try:
            win._on_copy_add()
            flip_cb = win._patch_panel._copy_table.cellWidget(0, 4)
            self.assertIsNotNone(flip_cb, "复制表第5列应为左右翻转复选框")
            self.assertFalse(win._state.project.copy_rules[0].flip_horizontal)
            flip_cb.setChecked(True)
            self.assertTrue(win._state.project.copy_rules[0].flip_horizontal)
            self.assertEqual(
                win._state.project.copy_rules[0].anchor_grid,
                win._state.current_scope().grid_key,
                "复制规则应锚定创建时的作用域网格")
        finally:
            self._close(win)

    def test_copy_table_index_column(self):
        """复制页序号列:与补丁页"补丁N"同逻辑。"""
        win = self._open()
        win._on_copy_add()
        self.app.processEvents()
        self.assertEqual(win._patch_panel._copy_table.item(0, 1).text(), "复制1",
                         "复制页应显示序号")
        win._on_copy_add()
        self.app.processEvents()
        self.assertEqual(win._patch_panel._copy_table.item(1, 1).text(), "复制2")
        self._close(win)

    def test_unused_patch_index_red(self):
        """未被任何段启用的补丁序号标红;重新启用恢复默认色。"""
        win = self._open()
        win._on_split(1.0)     # 两段
        dst = Rect(0.4, 0.1, 0.12, 0.2)
        patch = Patch(dst=dst, src=Rect(0.066, 0.1, 0.12, 0.2), source_tile_idx=0)
        win._on_patch_created(patch)          # 播放头在段 0 → 段 0 启用
        self.app.processEvents()
        item = win._patch_panel._patch_table.item(0, 1)
        c = item.foreground().color()
        self.assertFalse(c.red() > 150 and c.green() < 150, "启用中不应标红")
        win._on_patch_toggled(patch.id, False)   # 取消 → 未在任何段启用
        self.app.processEvents()
        item = win._patch_panel._patch_table.item(0, 1)
        c = item.foreground().color()
        self.assertGreater(c.red(), 150, "未启用序号应标红")
        self.assertLess(c.green(), 150, "红色应无绿成分")
        win._on_patch_toggled(patch.id, True)    # 重新启用
        self.app.processEvents()
        item = win._patch_panel._patch_table.item(0, 1)
        c = item.foreground().color()
        self.assertFalse(c.red() > 150 and c.green() < 150, "重新启用后恢复默认色")
        self._close(win)

    def test_clear_targets_then_change_source_then_recheck(self):
        """回归:全取消目标格 → 点来源格 → 重勾选目标格,补丁必须恢复显示。

        曾:全取消(dst 清零)后点来源格,lock_align 用零 dst 重算把 src 也
        清零 → 重勾选时 align_from_src(零 src) 无法恢复 → 补丁永久隐藏。
        """
        win = self._open()
        dst = Rect(0.4, 0.1, 0.12, 0.2)
        patch = Patch(dst=dst, src=Rect(0.066, 0.1, 0.12, 0.2),
                      source_tile_idx=1, dst_tile_idx=2)
        win._on_patch_created(patch)
        # 全取消目标格
        win._on_patch_targets_changed(patch.id, -1, [])
        self.assertEqual(patch.dst.nw, 0, "全取消后 dst 应清零")
        # 点来源格(lock_align 重算路径)
        win._on_patch_source_changed(patch.id, 0)
        self.assertGreater(patch.src.nw, 0, "点来源格不应破坏 src(零 dst 跳过重算)")
        # 重勾选目标格 → dst 必须恢复
        win._on_patch_targets_changed(patch.id, 1, [])
        self.assertGreater(patch.dst.nw, 0, "重勾选目标格后 dst 应恢复(补丁重新显示)")
        self._close(win)

    def test_queue_add_updates_ui(self):
        """添加当前工程 → 队列 UI 立即显示一行(回归:queue_changed 未接线无反应)。"""
        win = self._open()
        win._queue_add_current()
        self.assertEqual(win._queue_widget._table.rowCount(), 1,
                         "添加当前工程应显示一行")
        win._queue_add_current()
        self.assertEqual(win._queue_widget._table.rowCount(), 2, "再次添加应增行")
        self.assertEqual(win._controller.jobs()[0].status.value, "queued")
        self._close(win)

    def test_edge_move_blocked_prompts(self):
        """有补丁的当前片段尝试移动边界 → 弹出提示(文案按用户要求)。"""
        from unittest import mock

        from PySide6.QtCore import Qt

        win = self._open()
        dst = Rect(0.4, 0.1, 0.12, 0.2)
        patch = Patch(dst=dst, src=Rect(0.066, 0.1, 0.12, 0.2), source_tile_idx=0)
        win._on_patch_created(patch)
        win._frame_view._hover_grid_edge = 1
        with mock.patch("ui.main_window.QMessageBox.information") as info:
            win._frame_view._interactor.nudge_grid_edge(Qt.Key.Key_Right)
        self.assertEqual(info.call_count, 1, "应弹一次提示")
        text = str(info.call_args)
        self.assertIn("请删除补丁/复制后重试", text)
        self._close(win)

    def test_patch_and_copy_apply_to_current_segment_only(self):
        """两段时在第一段画补丁/加复制 → 只应用第一段(分割 list 独立回归)。"""
        win = self._open()
        win._on_split(1.5)
        win._player.seek(0.5)                 # 播放头移到第一段
        self._wait(1000)
        dst = Rect(0.4, 0.1, 0.12, 0.2)
        patch = Patch(dst=dst, src=Rect(0.066, 0.1, 0.12, 0.2), source_tile_idx=0)
        win._on_patch_created(patch)
        s0, s1 = win._state.project.segments
        self.assertIn(patch.id, s0.patch_ids)
        self.assertNotIn(patch.id, s1.patch_ids, "补丁不应应用到第二段")
        win._on_copy_add()
        rid = win._state.project.copy_rules[0].id
        self.assertIn(rid, s0.copy_rule_ids)
        self.assertNotIn(rid, s1.copy_rule_ids, "复制规则不应应用到第二段")
        # list 独立(防共享引用回归)
        self.assertIsNot(s0.patch_ids, s1.patch_ids)
        self.assertIsNot(s0.copy_rule_ids, s1.copy_rule_ids)
        self._close(win)

    def test_clear_all_targets_invalidates_patch(self):
        """目标格全取消 → 补丁无效(dst 清零、预览不显示);重勾选恢复。"""
        win = self._open()
        dst = Rect(0.4, 0.1, 0.12, 0.2)
        patch = Patch(dst=dst, src=Rect(0.066, 0.1, 0.12, 0.2),
                      source_tile_idx=0, dst_tile_idx=1)
        win._on_patch_created(patch)
        win._on_patch_targets_changed(patch.id, -1, [])   # 全取消
        self.assertEqual(patch.dst.nw, 0, "全取消后目标矩形应清零(补丁无效)")
        self.assertEqual(patch.dst_tile_idx, -1)
        # 重勾选 → 重算恢复
        win._on_patch_targets_changed(patch.id, 2, [])
        self.assertGreater(patch.dst.nw, 0, "重勾选应重算恢复目标矩形")
        self.assertEqual(patch.dst_tile_idx, 2)
        self._close(win)

    def test_split_clones_grids(self):
        """分割已有独立网格的段 → 两段各自持有独立网格(分界线按段独立)。"""
        win = self._open()
        win._state.project.grid = GridLayout.thirds()
        win._on_split(1.0)                                    # 分支1:两段 grid=None
        win._state.project.segments[0].grid = GridLayout.thirds()   # 段0 独立网格
        win._on_split(0.5)                                    # 分割段0 → 分支2 clone
        s0, s1 = win._state.project.segments[0], win._state.project.segments[1]
        self.assertIsNotNone(s0.grid)
        self.assertIsNot(s0.grid, s1.grid, "两段网格应为独立对象")
        self.assertAlmostEqual(s0.grid.tiles[1].nx, s1.grid.tiles[1].nx, places=6,
                               msg="分割起点网格几何应一致")
        self._close(win)

    def test_patch_source_change_uses_patch_grid(self):
        """段内补丁改来源格 → 用补丁自己的网格对齐(曾误用全局网格导致 src 错位)。"""
        from PySide6.QtCore import Qt

        win = self._open()
        win._on_split(1.0)
        seg = win._state.project.segments[0]
        seg.grid = GridLayout.thirds()
        seg.grid.move_edge(1, 0.25)      # 段网格分界线拖到 25%(≠ 全局 1/3)
        win._refresh_scope()             # 推送段网格上下文(anchor 赋值用)

        dst = Rect(0.02, 0.1, 0.2, 0.3)
        patch = Patch(dst=dst, src=Rect(0.02, 0.1, 0.2, 0.3),
                      source_tile_idx=0, dst_tile_idx=0,
                      anchor_grid=f"segment:{seg.id}")   # 锚定段网格(与 rubber 创建一致)
        win._on_patch_created(patch)
        win._on_patch_source_changed(patch.id, 1)   # 改来源格到格2

        expected = seg.grid.align_rect(patch.dst, 1)
        self.assertIsNotNone(expected)
        self.assertAlmostEqual(patch.src.nx, expected.nx, places=6,
                               msg="src 应按补丁自己的网格对齐")
        wrong = win._state.project.grid.align_rect(patch.dst, 1)
        self.assertNotAlmostEqual(patch.src.nx, wrong.nx, places=6,
                                  msg="段网格与全局几何不同时应算出不同位置")
        self._close(win)

    def test_edge_edit_syncs_all_grids(self):
        """编辑边缘线 → project.grid 首边更新,所有段网格外边界同步(全程统一)。"""
        from PySide6.QtCore import Qt

        win = self._open()
        pr = win._state.project
        pr.grid = GridLayout.thirds()
        win._on_split(1.0)
        pr.segments[0].grid = GridLayout.thirds()
        pr.segments[1].grid = GridLayout.thirds()

        fv = win._frame_view
        win._refresh_scope()                              # 播放头 0 → 段0 上下文
        fv._hover_grid_edge = -1                          # 左边缘线
        fv._interactor.nudge_grid_edge(Qt.Key.Key_Right)

        self.assertGreater(pr.grid.crop_left, 0, "project.grid 首边应更新")
        for s in pr.segments:
            if s.grid is not None:
                self.assertAlmostEqual(s.grid.crop_left, pr.grid.crop_left, places=6,
                                       msg="段网格外边界应同步")
        self._close(win)


class TestMainWindowToolbar(unittest.TestCase):
    """工具栏(与 V1 一致):打开视频/保存工程/打开工程 按钮 + 快捷键。

    V2 重写时工具栏整体丢失(只剩快捷键),用户要求 1:1 保留界面交互。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_toolbar_has_three_file_actions(self):
        from PySide6.QtGui import QAction

        win = MainWindow(settings_org="FenshenFuTest", settings_app="test")
        try:
            actions = win.findChildren(QAction)
            texts = [a.text() for a in actions]
            for t in ("打开视频 (Ctrl+O)", "保存工程 (Ctrl+S)",
                      "打开工程 (Ctrl+Shift+O)"):
                self.assertIn(t, texts, f"工具栏应包含按钮 {t}")
            for label, key in (("打开视频 (Ctrl+O)", "Ctrl+O"),
                               ("保存工程 (Ctrl+S)", "Ctrl+S"),
                               ("打开工程 (Ctrl+Shift+O)", "Ctrl+Shift+O")):
                a = next(x for x in actions if x.text() == label)
                self.assertEqual(a.shortcut().toString(), key,
                                 f"{label} 的快捷键应由 QAction 承担")
            # 模板按钮在传输栏(倍速右边),不在工具栏
            from PySide6.QtWidgets import QPushButton
            btns = [b.text() for b in win.findChildren(QPushButton)]
            for t in ("保存模板", "应用模板"):
                self.assertIn(t, btns, f"传输栏应包含按钮 {t}")
        finally:
            win._worker.stop()
            win.close()

    def test_save_template_uses_current_segment(self):
        """保存模板 = 当前分段的网格 + 该段启用的补丁/复制(无段 = 全局)。"""
        import tempfile
        from unittest import mock

        from core.template import load_template

        win = MainWindow(settings_org="FenshenFuTest", settings_app="test")
        try:
            pr = Project("D:\\x.mp4", 100)
            pr.grid = GridLayout.thirds()
            p1 = Patch(dst=Rect(0.45, 0.2, 0.1, 0.1), src=Rect(0.1, 0.2, 0.1, 0.1),
                       source_tile_idx=0, anchor_grid="project")
            p2 = Patch(dst=Rect(0.55, 0.2, 0.1, 0.1), src=Rect(0.2, 0.2, 0.1, 0.1),
                       source_tile_idx=0, anchor_grid="project")
            pr.patches = [p1, p2]
            seg = Segment(id="A", start=0, end=50, patch_ids=[p1.id],
                          grid=GridLayout.quarters())
            pr.segments = [seg]
            win._state.set_video(pr, None)
            win._state.set_current_time(10)     # 播放头在段 A
            with tempfile.TemporaryDirectory() as td:
                tpl_path = f"{td}\\t.fstpl.json"
                with mock.patch("PySide6.QtWidgets.QInputDialog.getText",
                                return_value=("tpl", True)), \
                     mock.patch("ui.main_window_services.default_template_path",
                                return_value=tpl_path):
                    win._save_template()
                tpl = load_template(tpl_path)
                self.assertEqual(tpl["grid"], GridLayout.quarters(),
                                 "应保存当前段的网格(而非全局 thirds)")
                self.assertEqual([p.id for p in tpl["patches"]], [p1.id],
                                 "只应包含该段启用的补丁 p1,不含 p2")
        finally:
            win._worker.stop()
            win.close()

    def test_queue_finished_shows_single_summary(self):
        """多个任务完成:不逐任务弹窗,queueFinished 时统一汇总一次。"""
        from unittest import mock

        win = MainWindow(settings_org="FenshenFuTest", settings_app="test")
        try:
            # 两个任务完成(一成一败)→ 不应弹两个窗
            win._on_job_finished("j1", True, "任务1 完成")
            win._on_job_finished("j2", False, "ffmpeg 失败")
            with mock.patch("PySide6.QtWidgets.QMessageBox.warning") as w, \
                 mock.patch("PySide6.QtWidgets.QMessageBox.information") as i:
                win._on_queue_finished()
            w.assert_called_once()
            i.assert_not_called()
            self.assertIn("完成 1 个,失败 1 个", w.call_args[0][2])
            self.assertIn("j2", w.call_args[0][2])
            # 结果已重置:下次队列完成不重复弹
            win._on_queue_finished()
            i.assert_not_called()
            w.assert_called_once()
        finally:
            win._worker.stop()
            win.close()

    def test_batch_import_rejects_segmented_project(self):
        """批量导入门禁:当前工程有分段 → 拒绝并提示(不静默)。"""
        from unittest import mock

        win = MainWindow(settings_org="FenshenFuTest", settings_app="test")
        try:
            pr = Project("D:\\x.mp4", 100)
            pr.grid = GridLayout.thirds()
            pr.segments = [Segment(id="A", start=0, end=50)]
            win._state.set_video(pr, None)
            with mock.patch("ui.main_window.show_error") as err:
                win._on_batch_import()
            err.assert_called_once()
            self.assertIn("分段", err.call_args[0][2], "提示应说明需删除分段")
        finally:
            win._worker.stop()
            win.close()

    def test_speed_buttons_default_1x(self):
        """倍速按钮:4 档存在、默认 1倍选中;点击 2倍 → worker rate 生效。"""
        win = MainWindow(settings_org="FenshenFuTest", settings_app="test")
        try:
            self.assertEqual(set(win._speed_btns), {0.5, 1.0, 2.0, 3.0, 5.0},
                             "应有 0.5/1/2/3/5 五档倍速")
            self.assertTrue(win._speed_btns[1.0].isChecked(), "默认 1倍选中")
            win._speed_btns[2.0].click()
            QTest.qWait(80)   # set_rate 是队列命令,worker 异步消费
            self.assertEqual(win._player._worker._rate, 2.0, "点击 2倍应生效到 worker")
            self.assertTrue(win._speed_btns[2.0].isChecked(), "2倍按钮应保持选中")
            win._speed_btns[0.5].click()
            QTest.qWait(80)
            self.assertEqual(win._player._worker._rate, 0.5)
        finally:
            win._worker.stop()
            win.close()


if __name__ == "__main__":
    unittest.main()
