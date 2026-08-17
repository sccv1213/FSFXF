"""ffmpeg 命令行构建(纯函数,重点单测对象)。迁移自旧 render_commands.py。

签名变化:函数参数从 Segment 改 Scope(planning.Scope)——补丁/复制规则
经 scope 解析;补丁网格一律 project.resolve_grid(p.anchor_grid)。
关键契约(有测试背书,原样保留):
- crop 标签被补丁 crop 与 overlay 多消费者共享时,overlay main 必须显式
  scale 固定尺寸,否则 ffmpeg 把 main 推断回原始分辨率(裁剪失效)
- -ss 在 -i 后(输出 seek):本 ffmpeg 构建输入 seek + -t + B 帧会多丢时长
  → concat 拼接点空洞
- 码率目标模式绝无 -cq(NVENC vbr+cq 是质量优先,冲满 maxrate 码率虚高 47%)
所有参数以 list 传递(避免引号转义坑)。
"""
from __future__ import annotations

from typing import NamedTuple

from .ffprobe import MediaInfo, color_args, map_pix_fmt, nvenc_pix_fmt
from .planning import Scope
from .project import EncoderSettings, Project


def fmt_ts(t: float) -> str:
    """时间参数高精度格式化(禁止 .3f:60fps 帧边界会因毫秒圆整丢/重帧)。"""
    if t <= 0:
        return "0"
    s = f"{float(t):.9f}".rstrip("0").rstrip(".")
    return s or "0"


class OverlayOp(NamedTuple):
    """一次覆盖操作:来源像素矩形 → 目标位置,可选水平翻转。

    继承 NamedTuple 保持旧 6 元组解包/索引兼容,同时携带 flip_x。
    """

    sx: int
    sy: int
    sw: int
    sh: int
    dx: int
    dy: int
    flip_x: bool = False


def patches_to_px(project: Project, patch_ids: list[str], fw: int, fh: int):
    """补丁 id → 像素矩形列表 [(sx,sy,sw,sh,dx,dy)],已取偶。

    含多目标补丁(主 dst + 额外目标格派生 dst)。
    """
    out = []
    for pid in patch_ids:
        p = project.patch(pid)
        if p is None:
            continue
        _append_patch_ops(project, p, fw, fh, out)
    return out


def _append_patch_ops(project: Project, p, fw: int, fh: int, out: list) -> None:
    """展开一个补丁的全部覆盖操作(主目标 + 额外目标格)。"""
    g = project.resolve_grid(p.anchor_grid)
    targets = [p.dst]
    if g and g.tiles:
        for ti in p.extra_tile_indices:
            d = g.align_from_src(p.src, ti)
            if d is not None:
                targets.append(d)
    for dst in targets:
        sx, sy, sw, sh = p.src.to_px(fw, fh)
        dx, dy, dw, dh = dst.to_px(fw, fh)
        if sw <= 0 or sh <= 0 or dw <= 0 or dh <= 0:
            continue
        # 强制同尺寸(防御兜底;等宽性已由 to_px 结构性保证,overlay 尺寸
        # 不一致会自动缩放补丁,破坏逐帧一致)
        w = min(sw, dw)
        h = min(sh, dh)
        out.append(OverlayOp(sx, sy, w, h, dx, dy))


def copy_rule_ops(project: Project, scope: Scope, fw: int, fh: int,
                  out: list | None = None) -> list:
    """展开 scope 启用的复制规则:来源格整格 → 每个目标格整格。

    复制规则用自身 anchor_grid 解析网格(与补丁同一条 resolve_grid 路径),
    flip_horizontal 时每个目标 op 带 hflip 标志。
    """
    out = out if out is not None else []
    for rid in scope.copy_rule_ids:
        r = project.copy_rule(rid)
        if r is None:
            continue
        g = project.resolve_grid(r.anchor_grid)
        if not g.tiles:
            continue
        src_t = g.tile(r.source_tile_idx)
        if src_t is None:
            continue
        for ti in r.target_tile_indices:
            dst_t = g.tile(ti)
            if dst_t is None or ti == r.source_tile_idx:
                continue
            sx, sy, sw, sh = src_t.to_px(fw, fh)
            dx, dy, dw, dh = dst_t.to_px(fw, fh)
            if sw <= 0 or sh <= 0 or dw <= 0 or dh <= 0:
                continue
            w = min(sw, dw)
            h = min(sh, dh)
            out.append(OverlayOp(sx, sy, w, h, dx, dy, r.flip_horizontal))
    return out


def overlay_ops(project: Project, scope: Scope, fw: int, fh: int):
    """统一展开:补丁(主+额外目标)与复制规则的全部覆盖操作。

    返回裁剪后坐标(按左右边缘线),越出裁剪区的操作跳过(不是裁剪)。
    """
    g = scope.grid
    # 边缘线全程统一:裁剪边界取 project.grid 首/尾边(所有段网格已同步)
    left_px = int(round(project.grid.crop_left * fw)) if g and g.tiles else 0
    right_px = int(round(project.grid.crop_right * fw)) if g and g.tiles else fw
    out: list = []
    for pid in scope.patch_ids:
        p = project.patch(pid)
        if p is not None:
            _append_patch_ops(project, p, fw, fh, out)
    copy_rule_ops(project, scope, fw, fh, out)
    if left_px > 0 or right_px < fw:
        if right_px - left_px <= 0:
            return []
        converted = []
        for op in out:
            sx, sy, sw, sh, dx, dy = op[:6]
            if (sx < left_px or dx < left_px
                    or sx + sw > right_px or dx + sw > right_px):
                continue    # 越出裁剪区(含被裁掉的源/目标),跳过
            converted.append(OverlayOp(sx - left_px, sy, sw, sh,
                                       dx - left_px, dy, op.flip_x))
        return converted
    return out


def build_filter_complex(patches_px, crop=None,
                        frame_select: "tuple[int, int] | None" = None) -> str | None:
    """多补丁链式 [select]crop[+hflip]+overlay 滤镜串。

    crop = (left_px, right_px, fh):先按左右边缘线裁剪,后续滤镜在裁剪后画面上工作。
    frame_select = (start_frame, end_frame):VFR 精确帧切割,先按帧号选出
    [start,end),再 setpts=PTS-STARTPTS;不解码依赖 -ss 时间圆整。
    patches_px 元素支持旧 6 元组或 OverlayOp(flip_x=True 时 crop 后接 hflip)。
    """
    parts: list[str] = []
    if frame_select is not None:
        sf, ef = frame_select
        parts.append(f"[0:v]select='between(n,{sf},{ef - 1})',"
                     f"setpts=PTS-STARTPTS[vsel]")
        src = "[vsel]"
    else:
        src = "[0:v]"
    cw = ch = 0
    if crop:
        left_px, right_px, fh = crop
        cw, ch = right_px - left_px, fh
        parts.append(f"{src}crop={cw}:{ch}:{left_px}:0[c]")
        src = "[c]"
    if not patches_px:
        if crop is not None and frame_select is None:
            left_px, right_px, fh = crop
            return f"[0:v]crop={right_px - left_px}:{fh}:{left_px}:0[v]"
        if not parts:
            return None
        parts.append(f"{src}null[v]" if crop is None else f"{src}copy[v]")
        return ";".join(parts)
    for i, op in enumerate(patches_px):
        sx, sy, sw, sh, dx, dy = op[:6]
        flip = op.flip_x if hasattr(op, "flip_x") else False
        chain = f"{src}crop={sw}:{sh}:{sx}:{sy}"
        if flip:
            chain += ",hflip"
        parts.append(f"{chain}[p{i}]")
    # main 流显式固定尺寸:[c] 标签被补丁 crop 与 overlay 多消费者共享时,
    # ffmpeg 会把 overlay 的 main 尺寸推断回原始分辨率(裁剪失效、输出原尺寸)
    main = src
    if crop:
        parts.append(f"{src}scale={cw}:{ch}[m]")
        main = "[m]"
    cur = main
    for i, op in enumerate(patches_px):
        dx, dy = op[4], op[5]
        label = "[v]" if i == len(patches_px) - 1 else f"[t{i}]"
        parts.append(f"{cur}[p{i}]overlay={dx}:{dy}{label}")
        cur = label
    return ";".join(parts)


def codec_args(media: MediaInfo, settings: EncoderSettings) -> list[str]:
    """编码器参数(含码率模式)。"""
    hw = settings.encoder_mode == "hw"
    if hw:
        if media.is_hevc():
            vc = "hevc_nvenc"
        elif media.is_av1():
            vc = "av1_nvenc"
        else:
            vc = "h264_nvenc"
        args = ["-c:v", vc, "-preset", "p6"]
        if settings.quality_mode == "crf":
            args += ["-rc", "vbr", "-cq", str(settings.crf), "-b:v", "0"]
        else:
            b = _target_kbps(media, settings)
            # 码率目标模式:不带 -cq(NVENC 的 vbr+cq 是质量优先,会冲满 maxrate
            # 导致输出码率虚高)
            args += ["-rc", "vbr",
                     "-b:v", f"{b}k", "-maxrate", f"{int(b * 1.5)}k",
                     "-bufsize", f"{int(b * 2)}k", "-multipass", "qres", "-spatial_aq", "1"]
    else:
        if media.is_hevc():
            vc = "libx265"
        elif media.is_av1():
            vc = "libsvtav1"
        else:
            vc = "libx264"
        args = ["-c:v", vc, "-preset", settings.preset]
        if settings.quality_mode == "crf":
            args += ["-crf", str(settings.crf)]
        else:
            b = _target_kbps(media, settings)
            args += ["-b:v", f"{b}k", "-maxrate", f"{int(b * 1.5)}k",
                     "-bufsize", f"{int(b * 2)}k"]
    return args


def _target_kbps(media: MediaInfo, settings: EncoderSettings) -> int:
    if settings.quality_mode == "custom":
        return max(100, int(settings.custom_kbps))
    return max(100, int(media.effective_video_bitrate() / 1000 * settings.factor))


def pix_fmt_args(media: MediaInfo, settings: EncoderSettings) -> list[str]:
    hw = settings.encoder_mode == "hw"
    pf = nvenc_pix_fmt(media.pix_fmt) if hw else map_pix_fmt(media.pix_fmt)
    return ["-pix_fmt", pf or "yuv420p"]


def audio_args(media: MediaInfo, settings: EncoderSettings) -> list[str]:
    if not media.has_audio:
        return []
    if settings.audio_reencode:
        ab = _target_audio_kbps(media, settings)
        return ["-c:a", "aac", "-b:a", f"{ab}k",
                "-ar", str(media.sample_rate or 48000)]
    return ["-c:a", "copy"]


def _target_audio_kbps(media: MediaInfo, settings: EncoderSettings) -> int:
    """重编码目标码率:用户显式设置优先;否则按源音频码率 + 10% 余量。

    参照原本的音频质量,避免二次有损编码听感下降。
    """
    if settings.audio_kbps > 0:
        return max(32, int(settings.audio_kbps))
    src = max(32, int(media.effective_audio_bitrate() / 1000))
    return max(32, int(src * 1.1))


def fps_mode_arg(media: MediaInfo) -> list[str]:
    return ["-fps_mode", "passthrough" if media.is_vfr else "cfr"]


def build_segment_cmd(ffmpeg: str, media: MediaInfo, scope: Scope,
                      patches_px, settings: EncoderSettings,
                      input_path: str, out_path: str,
                      crop: tuple | None = None,
                      include_audio: bool = True,
                      video_timescale: "int | None" = None) -> list[str]:
    """单段编码命令(有补丁段与无补丁段参数完全一致,保证 concat 兼容)。

    crop = (left_px, right_px, fh):左右边缘线裁剪。
    include_audio=False:只编码视频(音频单独处理,避免 AAC 包边界漂移)。
    CFR 视频追加 -frames:v 强制该段精确帧数(帧号方案)。
    """
    dur = max(0.0, scope.end - scope.start)
    vfr_cut = (media.is_vfr and scope.start_frame is not None
               and scope.end_frame is not None and scope.frame_count > 0)
    frame_select = ((scope.start_frame, scope.end_frame) if vfr_cut else None)
    if vfr_cut:
        # VFR:不用 -ss/-t 时间圆整,按帧号 select 精确取帧;音频已由
        # RenderController 单独处理(此命令应为视频-only)
        cmd = [ffmpeg, "-hide_banner", "-y", "-xerror", "-i", input_path]
    else:
        # -ss 在 -i 后(输出 seek):本 ffmpeg 构建输入 seek + -t + 音频/B 帧会
        # 多丢(切割点-上一关键帧)时长 → 段内视频比音频短 → concat 拼接点空洞
        cmd = [ffmpeg, "-hide_banner", "-y", "-xerror",
               "-i", input_path, "-ss", fmt_ts(scope.start), "-t", fmt_ts(dur)]
    fc = build_filter_complex(patches_px, crop, frame_select)
    if fc:
        cmd += ["-filter_complex", fc, "-map", "[v]"]
    else:
        cmd += ["-map", "0:v"]
    if include_audio:
        cmd += ["-map", "0:a?"]
        cmd += ["-map", "0:s?"]
    cmd += codec_args(media, settings)
    cmd += pix_fmt_args(media, settings)
    cmd += color_args(media)
    cmd += fps_mode_arg(media)
    if include_audio:
        cmd += audio_args(media, settings)
    if not media.is_vfr:
        frame_count = scope.frame_count
        if frame_count is None:
            frame_count = int(round((scope.end - scope.start) * media.fps))
        if frame_count > 0:
            cmd += ["-frames:v", str(frame_count)]
    elif video_timescale:
        # VFR 段输出 timebase 与源一致,便于 setts 按源 ticks 修正末帧时长
        cmd += ["-video_track_timescale", str(video_timescale)]
    cmd += ["-movflags", "+faststart", "-avoid_negative_ts", "make_zero",
            "-progress", "pipe:1", "-nostats", "-loglevel", "error",
            out_path]
    return cmd


def build_audio_only_cmd(ffmpeg: str, media: MediaInfo, settings: EncoderSettings,
                         input_path: str, out_path: str,
                         start: float, duration: float,
                         start_sample: "int | None" = None,
                         end_sample: "int | None" = None) -> list[str]:
    """整段音频单独重编码(处理范围裁剪时使用),只编码一次,避免逐段 AAC 包边界漂移。

    码率参照源音频质量(用户显式设置优先),采样率保持源采样率。
    start_sample/end_sample 提供时用 atrim 采样级精确切割(按视频真实 PTS 计算),
    不使用 -ss/-t 时间圆整。
    """
    cmd = [ffmpeg, "-hide_banner", "-y", "-xerror", "-i", input_path]
    if start_sample is not None and end_sample is not None:
        cmd += ["-af", f"atrim=start_sample={start_sample}:end_sample={end_sample},"
                        f"asetpts=PTS-STARTPTS"]
    else:
        cmd += ["-ss", fmt_ts(start), "-t", fmt_ts(duration)]
    cmd += ["-vn", "-map", "0:a?"]
    cmd += ["-c:a", "aac", "-b:a", f"{_target_audio_kbps(media, settings)}k",
            "-ar", str(media.sample_rate or 48000)]
    cmd += ["-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", "-loglevel", "error",
            out_path]
    return cmd


def build_vfr_fix_cmd(ffmpeg: str, input_path: str, out_path: str,
                      last_pts_ticks: int, last_duration_ticks: int) -> list[str]:
    """VFR 段末尾帧时长修正:select/setpts 后最后显示帧的 duration 会掉到
    默认帧间隔;按 FrameIndex 的真实帧时长用 setts 回写(stream copy,无重编码)。
    """
    expr = f"setts=duration='if(eq(PTS,{last_pts_ticks}),{last_duration_ticks},DURATION)'"
    return [ffmpeg, "-hide_banner", "-y", "-xerror",
            "-i", input_path, "-c", "copy", "-bsf:v", expr,
            "-progress", "pipe:1", "-nostats", "-loglevel", "error",
            out_path]


def build_mux_original_audio_cmd(ffmpeg: str, video_path: str,
                                 original_path: str, out_path: str) -> list[str]:
    """全片范围不变:拼接后的视频 + 原视频整条音轨(流复制,零损失)。"""
    return [ffmpeg, "-hide_banner", "-y",
            "-i", video_path, "-i", original_path,
            "-map", "0:v", "-map", "1:a?", "-map", "1:s?",
            "-c", "copy", "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", "-loglevel", "error",
            out_path]


def build_mux_audio_file_cmd(ffmpeg: str, video_path: str,
                             audio_path: str, out_path: str) -> list[str]:
    """范围裁剪:拼接后的视频 + 单独重编码的音轨。"""
    return [ffmpeg, "-hide_banner", "-y",
            "-i", video_path, "-i", audio_path,
            "-map", "0:v", "-map", "1:a?",
            "-c", "copy", "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", "-loglevel", "error",
            out_path]


def build_concat_cmd(ffmpeg: str, list_file: str, out_path: str) -> list[str]:
    """concat demuxer 拼接:视频/音频/字幕全流复制(各段已统一编码参数)。

    -progress pipe:1:concat 阶段也输出进度(总进度加权模型依赖它)。
    """
    return [ffmpeg, "-hide_banner", "-y", "-f", "concat", "-safe", "0",
            "-i", list_file, "-c", "copy", "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", out_path]


def build_preview_cmd(ffmpeg: str, input_path: str, t: float,
                      patches_px, out_png: str,
                      crop: tuple | None = None) -> list[str]:
    """单帧渲染预览图。"""
    cmd = [ffmpeg, "-hide_banner", "-y", "-ss", fmt_ts(t), "-i", input_path,
           "-frames:v", "1"]
    fc = build_filter_complex(patches_px, crop)
    if fc:
        cmd += ["-filter_complex", fc, "-map", "[v]"]
    else:
        cmd += ["-map", "0:v"]
    cmd += ["-loglevel", "error", out_png]
    return cmd


def verify_output(media: MediaInfo, out_path: str, settings: EncoderSettings,
                  expected_size: tuple | None = None,
                  expected_duration: float | None = None,
                  expected_frame_count: "int | None" = None) -> list[str]:
    """渲染完成后核验输出参数,不达标返回告警列表(核验只告警,不阻断成功)。

    expected_size:左右边缘线裁剪后的预期分辨率(None = 期望与源一致)。
    expected_duration:处理范围的预期时长(裁剪范围时不再与全片时长比较)。
    expected_frame_count:CFR/VFR 各 scope frame_count 之和;不一致仅告警。
    """
    from .ffprobe import probe
    m = probe(out_path)
    if not m.width:
        return [f"无法读取输出 {out_path}"]
    warns = []
    if m.vcodec != media.vcodec:
        warns.append(f"编码器不一致:源 {media.vcodec} → 输出 {m.vcodec}")
    if expected_size:
        if (m.width, m.height) != expected_size:
            warns.append(f"分辨率不一致:预期 {expected_size[0]}×{expected_size[1]} "
                         f"→ 输出 {m.width}×{m.height}")
    elif (m.width, m.height) != (media.width, media.height):
        warns.append(f"分辨率不一致:源 {media.width}×{media.height} → 输出 {m.width}×{m.height}")
    if not media.is_vfr and m.fps and media.fps and abs(m.fps - media.fps) > max(0.5, media.fps * 0.03):
        warns.append(f"帧率不一致:源 {media.fps:.2f} → 输出 {m.fps:.2f}")
    if settings.quality_mode == "match" and m.effective_video_bitrate():
        src_br = media.effective_video_bitrate()
        out_br = m.effective_video_bitrate()
        if abs(out_br - src_br) > src_br * 0.15:
            warns.append(f"码率偏差过大:源 {src_br / 1000:.0f}k → 输出 {out_br / 1000:.0f}k")
    expected_dur = expected_duration if expected_duration is not None else media.duration
    if m.duration and expected_dur:
        if abs(m.duration - expected_dur) > max(0.2, expected_dur * 0.02):
            warns.append(f"时长不一致:预期 {expected_dur:.1f}s → 输出 {m.duration:.1f}s")
    if expected_frame_count is not None and m.frame_count:
        if m.frame_count != expected_frame_count:
            warns.append(f"帧数不一致:预期 {expected_frame_count} → 输出 {m.frame_count}")
    return warns
