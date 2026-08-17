"""视频信息面板:文件信息 / 处理范围 / 布局 / 编码设置(迁移自旧版,原样保留)。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from core.ffprobe import MediaInfo
from core.project import EncoderSettings
from .widgets import PaddedSpin

_LAYOUTS = [
    ("三格 9:16", "thirds"),
    ("两格 8:9", "halves"),
    ("四格", "quarters"),
    ("无网格(手动)", "none"),
]

_AUDIO_KBPS = [("匹配源码率", 0), ("96 kbps", 96), ("128 kbps", 128),
               ("160 kbps", 160), ("192 kbps", 192), ("256 kbps", 256),
               ("320 kbps", 320)]


class VideoInfoPanel(QWidget):
    openRequested = Signal()
    layoutChanged = Signal(str)                 # 布局 key: thirds/halves/none
    rangeChanged = Signal(float, float)
    settingsChanged = Signal()
    useCurrentForStart = Signal()
    useCurrentForEnd = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(300)
        self._duration = 0.0
        self._fps = 30.0
        lay = QVBoxLayout(self)

        # ---- 视频文件 ----
        grp = QGroupBox("视频")
        gl = QVBoxLayout(grp)
        row = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setReadOnly(True)
        self._path_edit.setPlaceholderText("未打开")
        btn_open = QPushButton("打开…")
        btn_open.clicked.connect(self.openRequested)
        row.addWidget(self._path_edit, 1)
        row.addWidget(btn_open)
        gl.addLayout(row)
        self._info_label = QLabel("打开视频后显示编码信息")
        self._info_label.setWordWrap(True)
        self._info_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        gl.addWidget(self._info_label)
        lay.addWidget(grp)

        # ---- 布局 ----
        grp = QGroupBox("布局(拼屏格数)")
        gl = QFormLayout(grp)
        self._layout_combo = QComboBox()
        for label, key in _LAYOUTS:
            self._layout_combo.addItem(label, key)
        self._layout_combo.currentIndexChanged.connect(
            lambda i: self.layoutChanged.emit(self._layout_combo.itemData(i)))
        gl.addRow("布局", self._layout_combo)
        tip = QLabel("选完布局后可回到画面,在\"选择\"模式下拖动分界线微调;\n"
                     "画红色目标矩形时源矩形会自动对齐。")
        tip.setWordWrap(True)
        tip.setObjectName("dim")
        gl.addRow(tip)
        lay.addWidget(grp)

        # ---- 处理范围(时:分:秒:帧 格式,与跳转框一致;帧从 1 开始) ----
        grp = QGroupBox("处理范围(输出起止)")
        gl = QFormLayout(grp)
        self._range_start_spins = self._make_time_spins()
        self._range_end_spins = self._make_time_spins()
        for sp in self._range_start_spins + self._range_end_spins:
            sp.valueChanged.connect(self._emit_range)
        # 起点行:4 框 + 当前按钮;终点行:4 框 + 当前按钮;开头/结尾独立一行(小按钮)
        row1 = QHBoxLayout()
        for sp in self._range_start_spins:
            row1.addWidget(sp)
        b1 = QPushButton("当前")
        b1.setToolTip("把播放头时间设为起点")
        b1.clicked.connect(self.useCurrentForStart)
        row1.addWidget(b1)
        self._btn_current_start = b1
        row2 = QHBoxLayout()
        for sp in self._range_end_spins:
            row2.addWidget(sp)
        b2 = QPushButton("当前")
        b2.setToolTip("把播放头时间设为终点")
        b2.clicked.connect(self.useCurrentForEnd)
        row2.addWidget(b2)
        self._btn_current_end = b2
        row3 = QHBoxLayout()
        b0 = QPushButton("开头")
        b0.setToolTip("起点设为视频开头(0)")
        b0.clicked.connect(lambda: self._set_range_notify(0.0, self.get_range()[1]))
        b_end = QPushButton("结尾")
        b_end.setToolTip("终点设为视频结尾")
        b_end.clicked.connect(lambda: self._set_range_notify(self.get_range()[0], self._duration))
        row3.addWidget(b0)
        row3.addWidget(b_end)
        self._btn_begin = b0
        self._btn_end = b_end
        row3.addStretch(1)
        gl.addRow("起点", row1)
        gl.addRow("终点", row2)
        gl.addRow(row3)
        lay.addWidget(grp)

        # ---- 编码设置 ----
        grp = QGroupBox("编码设置")
        gl = QFormLayout(grp)
        self._encoder_combo = QComboBox()
        self._encoder_combo.addItem("硬件优先(NVENC)", "hw")
        self._encoder_combo.addItem("纯软件(x264/x265)", "sw")
        self._quality_combo = QComboBox()
        self._quality_combo.addItem("匹配原码率(体积≈原视频)", "match")
        self._quality_combo.addItem("CRF 恒定质量(画质优先)", "crf")
        self._quality_combo.addItem("自定义码率", "custom")
        self._quality_combo.currentIndexChanged.connect(self._update_quality_enabled)
        self._crf_spin = QSpinBox()
        self._crf_spin.setRange(0, 51)
        self._crf_spin.setValue(18)
        self._crf_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)   # 去上下箭头
        self._crf_spin.lineEdit().setPlaceholderText("18")   # 清空时灰字显示默认值
        self._bitrate_spin = QSpinBox()
        self._bitrate_spin.setRange(100, 200000)
        self._bitrate_spin.setValue(6000)
        self._bitrate_spin.setSuffix(" kbps")               # kbps 固定在框体(只读后缀)
        self._bitrate_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self._bitrate_spin.lineEdit().setPlaceholderText("6000 kbps")
        self._factor_spin = QDoubleSpinBox()
        self._factor_spin.setRange(0.5, 2.0)
        self._factor_spin.setSingleStep(0.1)
        self._factor_spin.setValue(1.0)
        self._factor_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self._factor_spin.lineEdit().setPlaceholderText("1.0")
        self._factor_spin.setToolTip("匹配原码率时乘以的系数")
        self._preset_combo = QComboBox()
        for pr in ("ultrafast", "superfast", "veryfast", "faster", "fast",
                   "medium", "slow", "slower", "veryslow"):
            label = "medium(默认)" if pr == "medium" else pr
            self._preset_combo.addItem(label, pr)
        self._preset_combo.setCurrentIndex(
            self._preset_combo.findData("medium"))
        self._audio_check = QCheckBox("音频重编码(默认流复制,零损失)")
        self._audio_check.toggled.connect(lambda _: self.settingsChanged.emit())
        self._audio_check.toggled.connect(self._update_audio_enabled)
        self._audio_kbps_combo = QComboBox()
        for label, v in _AUDIO_KBPS:
            self._audio_kbps_combo.addItem(label, v)
        self._output_edit = QLineEdit()
        self._output_edit.setPlaceholderText("默认:源视频所在目录")
        btn_out = QPushButton("…")
        btn_out.clicked.connect(self._pick_output_dir)

        self._encoder_combo.currentIndexChanged.connect(lambda _: self.settingsChanged.emit())
        self._quality_combo.currentIndexChanged.connect(self._on_any_setting_changed)
        self._crf_spin.valueChanged.connect(self._on_any_setting_changed)
        self._bitrate_spin.valueChanged.connect(self._on_any_setting_changed)
        self._factor_spin.valueChanged.connect(self._on_any_setting_changed)
        self._preset_combo.currentIndexChanged.connect(lambda _: self.settingsChanged.emit())
        self._audio_kbps_combo.currentIndexChanged.connect(
            lambda _: self.settingsChanged.emit())
        self._output_edit.textChanged.connect(lambda _: self.settingsChanged.emit())

        gl.addRow("编码器", self._encoder_combo)
        gl.addRow("质量", self._quality_combo)
        gl.addRow("CRF 值", self._crf_spin)
        gl.addRow("目标码率", self._bitrate_spin)
        gl.addRow("码率系数", self._factor_spin)
        gl.addRow("软件预设", self._preset_combo)
        gl.addRow(self._audio_check)
        gl.addRow("音频码率", self._audio_kbps_combo)
        audio_note = QLabel("处理范围不是全片时,音频会重编码以对齐视频边界"
                            "(码率参照源音频质量)。")
        audio_note.setWordWrap(True)
        audio_note.setObjectName("dim")
        gl.addRow(audio_note)
        out_row = QHBoxLayout()
        out_row.addWidget(self._output_edit, 1)
        out_row.addWidget(btn_out)
        gl.addRow("输出目录", out_row)
        self._update_quality_enabled()
        self._update_audio_enabled()
        lay.addWidget(grp)

        lay.addStretch(1)

    @staticmethod
    def _make_time_spins() -> list:
        """时:分:秒:帧 四框(无上下箭头,复用 PaddedSpin)。"""
        spins = [PaddedSpin(99), PaddedSpin(59), PaddedSpin(59), PaddedSpin(60)]
        for sp in spins:
            sp.setFixedWidth(42)
        return spins


    # ---------- 信息 ----------
    def show_info(self, m: MediaInfo) -> None:
        self._duration = m.duration
        self._fps = m.fps or 30.0
        self._path_edit.setText(m.path)
        lines = [
            f"分辨率:{m.width}×{m.height}",
            f"时长:{m.duration / 60:.1f} 分钟",
            f"帧率:{m.fps:.2f} fps{'（可变帧率）' if m.is_vfr else ''}",
            f"编码:{m.vcodec} / {m.pix_fmt or '?'}",
        ]
        if m.vbitrate:
            lines.append(f"视频码率:{m.vbitrate / 1000:.0f} kbps")
        if m.has_audio:
            lines.append(f"音频:{m.acodec}({m.sample_rate}Hz)")
        self._info_label.setText("\n".join(lines))

    # ---------- 范围(时:分:秒:帧,帧从 1 开始) ----------
    def _t_to_spins(self, t: float) -> list[int]:
        fps = max(1, int(round(self._fps)))
        total = max(0, int(round(t * fps)))
        f = total % fps
        total_s = total // fps
        return [total_s // 3600, total_s % 3600 // 60, total_s % 60, f + 1]

    def _spins_to_t(self, spins) -> float:
        h, m, s, f = (sp.value() for sp in spins)
        return h * 3600 + m * 60 + s + (f - 1) / max(1, int(round(self._fps)))

    def set_range(self, lo: float, hi: float) -> None:
        for sp, v in zip(self._range_start_spins, self._t_to_spins(lo)):
            sp.blockSignals(True)
            sp.setValue(v)
            sp.blockSignals(False)
        for sp, v in zip(self._range_end_spins, self._t_to_spins(hi)):
            sp.blockSignals(True)
            sp.setValue(v)
            sp.blockSignals(False)

    def get_range(self) -> tuple[float, float]:
        return (self._spins_to_t(self._range_start_spins),
                self._spins_to_t(self._range_end_spins))

    def _emit_range(self, *_) -> None:
        self.rangeChanged.emit(self.get_range()[0], self.get_range()[1])

    def _set_range_notify(self, lo: float, hi: float) -> None:
        """程序侧改范围并通知(rangeChanged → 时间轴刷新;修复:按钮曾只 set_range 不通知)。"""
        self.set_range(lo, hi)
        self.rangeChanged.emit(lo, hi)

    def active_input_cleared(self) -> bool:
        """当前质量模式下生效的输入框是否被用户清空(空 = 未填有效数值)。"""
        mode = self._quality_combo.currentData()
        spin = {"crf": self._crf_spin, "custom": self._bitrate_spin,
                "match": self._factor_spin}.get(mode)
        if spin is None:
            return False
        return spin.lineEdit().text().strip() in ("", "-", "+")

    # ---------- 布局 ----------
    def set_layout_key(self, key: str) -> None:
        """程序侧更新下拉显示(blockSignals 防触发 layoutChanged 循环)。"""
        idx = self._layout_combo.findData(key)
        if idx >= 0:
            self._layout_combo.blockSignals(True)
            self._layout_combo.setCurrentIndex(idx)
            self._layout_combo.blockSignals(False)

    # ---------- 编码设置 ----------
    def _on_any_setting_changed(self, *_) -> None:
        self.settingsChanged.emit()

    def _update_quality_enabled(self, *_) -> None:
        mode = self._quality_combo.currentData()
        self._crf_spin.setEnabled(mode == "crf")
        self._bitrate_spin.setEnabled(mode == "custom")
        self._factor_spin.setEnabled(mode == "match")

    def _update_audio_enabled(self, *_) -> None:
        self._audio_kbps_combo.setEnabled(self._audio_check.isChecked())

    def get_settings(self) -> EncoderSettings:
        s = EncoderSettings()
        s.encoder_mode = self._encoder_combo.currentData()
        s.quality_mode = self._quality_combo.currentData()
        s.crf = self._crf_spin.value()
        s.custom_kbps = self._bitrate_spin.value()
        s.factor = self._factor_spin.value()
        s.preset = self._preset_combo.currentData() or "medium"
        s.audio_reencode = self._audio_check.isChecked()
        s.audio_kbps = self._audio_kbps_combo.currentData() or 0
        s.output_dir = self._output_edit.text().strip()
        return s

    def apply_settings(self, s: EncoderSettings) -> None:
        self._encoder_combo.setCurrentIndex(self._encoder_combo.findData(s.encoder_mode))
        self._quality_combo.setCurrentIndex(self._quality_combo.findData(s.quality_mode))
        self._crf_spin.setValue(s.crf)
        self._bitrate_spin.setValue(s.custom_kbps)
        self._factor_spin.setValue(s.factor)
        self._preset_combo.setCurrentText(s.preset)
        self._audio_check.setChecked(s.audio_reencode)
        idx = self._audio_kbps_combo.findData(s.audio_kbps)
        self._audio_kbps_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._output_edit.setText(s.output_dir)
        self._update_quality_enabled()

    def _pick_output_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择输出目录",
                                             self._output_edit.text() or "")
        if d:
            self._output_edit.setText(d)
