"""MainWindow 业务动作服务:文件/工程/模板/预览/批量导入。

从 main_window.py 抽出,函数统一接收 win,避免 MainWindow 继续膨胀。
测试与旧调用方仍通过 MainWindow 的薄包装方法使用,行为不变。
"""
from __future__ import annotations

import os
import subprocess
import tempfile

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFileDialog, QMessageBox

from core.ffprobe import find_ffmpeg, probe
from core.grid import GridLayout
from core.project import Project
from core.render_commands import build_preview_cmd, overlay_ops
from core.template import default_template_path, load_template, save_template
from .widgets import ScaledPixmapLabel

_VIDEO_FILTER = "视频 (*.mp4 *.mkv *.ts *.flv *.mov *.avi);;所有文件 (*)"


# ---- 文件 / 工程 ----

def open_video_dialog(win) -> None:
    path, _ = QFileDialog.getOpenFileName(
        win, "打开视频", win._state.last_dir() or os.path.expanduser("~"),
        _VIDEO_FILTER)
    if path:
        win._load_video(path)


def load_video(win, path: str) -> None:
    media = probe(path)
    if not media.width:
        win._show_error("无法打开视频",
                   media.warnings[0] if media.warnings else "视频文件无效")
        return
    proj = Project(path, media.duration)
    proj.grid = GridLayout.thirds()
    win._state.set_video(proj, media)
    win._info.show_info(media)
    win._info.set_range(*proj.process_range)
    win._info.apply_settings(proj.settings)
    win._frame_view.set_project(proj)
    win._player.open(path)
    win._refresh_scope()
    win._state.set_last_dir(os.path.dirname(path))
    win._set_mode("select")
    win._start_frame_indexer(path)


def save_project(win) -> None:
    proj = win._state.project
    if proj is None:
        return
    # 默认文件名 = 导入视频名(与 V1 一致):video.mp4 → video.fsfx.json
    base = os.path.splitext(os.path.basename(proj.video_path))[0]
    default = os.path.join(win._state.last_dir() or "", f"{base}.fsfx.json")
    path, _ = QFileDialog.getSaveFileName(
        win, "保存工程", default, "工程文件 (*.fsfx.json)")
    if path:
        proj.save(path)


def open_project_dialog(win) -> None:
    path, _ = QFileDialog.getOpenFileName(
        win, "打开工程", win._state.last_dir() or os.path.expanduser("~"),
        "工程文件 (*.fsfx.json)")
    if not path:
        return
    try:
        proj = Project.load(path)
    except ValueError as e:
        win._show_error("工程不兼容", f"{e}\n旧版工程请在新版中重建")
        return
    if not os.path.exists(proj.video_path):
        ret = QMessageBox.question(
            win, "视频文件缺失",
            f"工程引用的视频不存在:\n{proj.video_path}\n\n"
            "是否重新选择视频文件?")
        if ret != QMessageBox.StandardButton.Yes:
            return
        vpath, _ = QFileDialog.getOpenFileName(
            win, "选择视频文件", os.path.dirname(proj.video_path), _VIDEO_FILTER)
        if not vpath:
            return
        proj.video_path = vpath
    media = probe(proj.video_path)
    if not media.width:
        win._show_error("无法打开视频",
                   media.warnings[0] if media.warnings else "视频文件无效")
        return
    win._state.set_video(proj, media)
    win._info.show_info(media)
    win._info.set_range(*proj.process_range)
    win._info.apply_settings(proj.settings)
    win._frame_view.set_project(proj)
    win._player.open(proj.video_path)
    win._refresh_scope()
    win._state.set_last_dir(os.path.dirname(path))
    win._start_frame_indexer(proj.video_path)


# ---- 模板 ----

def save_template_action(win) -> None:
    proj = win._state.project
    if proj is None:
        return
    seg = win._state.current_user_segment()
    if seg is not None:
        grid = seg.grid if seg.grid is not None else proj.grid
        patches = [p for p in proj.patches if p.id in seg.patch_ids]
        copy_rules = [r for r in proj.copy_rules if r.id in seg.copy_rule_ids]
    else:
        grid, patches, copy_rules = proj.grid, proj.patches, proj.copy_rules
    from PySide6.QtWidgets import QInputDialog
    name, ok = QInputDialog.getText(win, "保存模板", "模板名称:")
    if not ok or not name.strip():
        return
    path = default_template_path(name.strip())
    save_template(grid, patches, copy_rules, path)
    win._state_label.setText(
        f"模板已保存({('分段' if seg is not None else '全局')}):{os.path.basename(path)}")


def apply_template_action(win) -> None:
    proj = win._state.project
    if proj is None:
        return
    path, _ = QFileDialog.getOpenFileName(
        win, "应用模板", os.path.dirname(default_template_path("x")),
        "模板文件 (*.fstpl.json)")
    if not path:
        return
    try:
        tpl = load_template(path)
    except (ValueError, OSError) as e:
        win._show_error("模板文件无效", str(e))
        return
    seg = win._state.current_user_segment()
    proj.apply_template(tpl, seg)
    win._state.project_changed.emit()
    win._state_label.setText(f"模板已应用到{'分段' if seg is not None else '全局'}")


# ---- 预览 ----

def preview_frame(win) -> None:
    proj, media = win._state.project, win._state.video_meta
    if proj is None or media is None:
        return
    scope = win._state.current_scope()
    if scope is None or not scope.has_work:
        QMessageBox.information(win, "预览", "当前时间没有启用补丁或复制规则，无需修复")
        return
    t = win._player.current_time
    pxs = overlay_ops(proj, scope, media.width, media.height)
    crop = None
    if proj.grid.crop_left > 0 or proj.grid.crop_right < 1.0:
        left_px = int(round(proj.grid.crop_left * media.width))
        right_px = int(round(proj.grid.crop_right * media.width))
        crop = (left_px, right_px, media.height)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with tempfile.TemporaryDirectory() as td:
        out_png = os.path.join(td, "preview.png")
        orig_png = os.path.join(td, "original.png")
        cmds = [
            build_preview_cmd(find_ffmpeg(), proj.video_path, t, pxs,
                              out_png, crop=crop),
            build_preview_cmd(find_ffmpeg(), proj.video_path, t, [],
                              orig_png, crop=crop),
        ]
        for cmd in cmds:
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=60,
                                   creationflags=flags)
            except FileNotFoundError:
                win._show_error("预览失败", "找不到 ffmpeg，请安装并加入 PATH")
                return
            if r.returncode != 0:
                win._show_error("预览失败",
                           r.stderr.decode("utf-8", "replace")[-500:])
                return
        if not os.path.exists(out_png) or not os.path.exists(orig_png):
            win._show_error("预览失败", "预览图未生成")
            return
        show_preview_dialog(win, orig_png, out_png)


def show_preview_dialog(win, orig_path: str, fixed_path: str) -> None:
    from PySide6.QtWidgets import QDialog, QHBoxLayout, QVBoxLayout
    pm_orig = QPixmap(orig_path)
    pm_fixed = QPixmap(fixed_path)
    dlg = QDialog(win)
    dlg.setWindowTitle("预览 — 左:原帧  右:修复后")
    lay = QVBoxLayout(dlg)
    row = QHBoxLayout()
    row.addWidget(preview_label(pm_orig, "原帧"))
    row.addWidget(preview_label(pm_fixed, "修复后"))
    lay.addLayout(row)
    saved = win._state.get_setting("preview_size", "")
    if saved:
        try:
            w, h = (int(x) for x in saved.split(","))
            if w > 300 and h > 200:
                dlg.resize(w, h)
        except ValueError:
            pass
    else:
        dlg.resize(max(400, pm_orig.width() + pm_fixed.width() + 60),
                   max(200, max(pm_orig.height(), pm_fixed.height()) + 60))
    dlg.exec()
    win._state.set_setting("preview_size", f"{dlg.width()},{dlg.height()}")


def preview_label(pm, title: str):
    from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget
    w = QWidget()
    lay = QVBoxLayout(w)
    t = QLabel(title)
    t.setAlignment(Qt.AlignmentFlag.AlignCenter)
    img = ScaledPixmapLabel(pm)
    lay.addWidget(img, 1)
    lay.addWidget(t)
    return w


# ---- 队列 / 批量 ----

def queue_add_current(win) -> None:
    proj = win._state.project
    if proj is None:
        return
    if win._info.active_input_cleared():
        win._show_error("编码参数未填写",
                   "请填写生效的编码参数(CRF值/目标码率/码率系数)")
        return
    ok, issues = win._state.can_render()
    if not ok:
        win._show_error("无法加入队列", "\n".join(issues))
        return
    win._controller.submit(proj, auto_start=False)
    win._state.set_queue_jobs(win._controller.jobs())


def batch_import(win) -> None:
    proj = win._state.project
    if proj is None:
        return
    if proj.segments:
        win._show_error("批量处理",
                   "当前工程有分段,请先删除分段\n(批量按全局的补丁/复制/网格/设置处理)")
        return
    if win._info.active_input_cleared():
        win._show_error("编码参数未填写",
                   "请填写生效的编码参数(CRF值/目标码率/码率系数)")
        return
    ok, issues = win._state.can_render()
    if not ok:
        win._show_error("无法批量处理", "\n".join(issues))
        return
    files, _ = QFileDialog.getOpenFileNames(
        win, "批量导入视频(按当前工程设置处理)",
        win._state.last_dir() or os.path.expanduser("~"), _VIDEO_FILTER)
    if not files:
        return
    added = skipped = 0
    for path in files:
        media = probe(path)
        if not media.width:
            skipped += 1
            reason = media.warnings[0] if media.warnings else "无法读取视频信息"
            win._controller.logLine.emit(f"批量跳过 {os.path.basename(path)}:{reason}")
            continue
        pr = proj.clone_for_video(path, media.duration)
        pr.process_range_frames = [0, media.frame_count or int(round(media.duration * media.fps))]
        issues = pr.validate()
        if issues:
            skipped += 1
            win._controller.logLine.emit(f"批量跳过 {os.path.basename(path)}:{issues[0]}")
        else:
            win._controller.submit(pr, auto_start=False)
            added += 1
    win._state.set_queue_jobs(win._controller.jobs())
    win._state_label.setText(f"批量导入:已添加 {added} 个视频,跳过 {skipped} 个")
