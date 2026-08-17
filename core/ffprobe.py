"""ffprobe 封装:读取视频信息 + 编码参数映射表(迁移自旧 ffprobe_util.py,原样保留)。

三级码率回退与色彩元数据透传是踩坑后的正确解法,有 e2e 测试背书。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field


def find_ffmpeg() -> str:
    """返回 ffmpeg 可执行文件路径(PATH 优先,无则裸名)。"""
    p = shutil.which("ffmpeg")
    if p:
        return p
    return "ffmpeg"


def find_ffprobe() -> str:
    p = shutil.which("ffprobe")
    if p:
        return p
    return "ffprobe"


def _parse_fps(s: str | None) -> float | None:
    if not s or s == "0/0":
        return None
    try:
        if "/" in s:
            num, den = s.split("/")
            return int(num) / int(den)
        return float(s)
    except (ValueError, ZeroDivisionError):
        return None


@dataclass
class MediaInfo:
    path: str = ""
    width: int = 0
    height: int = 0
    duration: float = 0.0
    fps: float = 0.0
    frame_count: int = 0              # 视频流 nb_frames(0 = 未知,按 duration×fps 派生)
    is_vfr: bool = False
    vcodec: str = ""
    vbitrate: int | None = None       # bps
    pix_fmt: str = ""
    has_audio: bool = False
    acodec: str | None = None
    abitrate: int | None = None       # bps
    audio_channels: int = 0
    sample_rate: int = 0
    color_primaries: str | None = None
    color_trc: str | None = None
    colorspace: str | None = None
    color_range: str | None = None
    file_size: int = 0
    format_bitrate: int | None = None
    warnings: list[str] = field(default_factory=list)

    # ---- 码率回退 ----
    def effective_video_bitrate(self) -> int:
        """三级回退:stream bit_rate → format.bit_rate − 音频 → 文件大小×8/时长。返回 bps。"""
        if self.vbitrate and self.vbitrate > 0:
            return self.vbitrate
        if self.format_bitrate and self.format_bitrate > 0:
            vb = self.format_bitrate - (self.abitrate or 0)
            if vb > 0:
                return vb
        if self.duration > 0 and self.file_size > 0:
            return int(self.file_size * 8 / self.duration)
        return 0

    def effective_audio_bitrate(self) -> int:
        if self.abitrate and self.abitrate > 0:
            return self.abitrate
        return 128_000  # 缺省 128k

    def is_hevc(self) -> bool:
        c = self.vcodec.lower()
        return "265" in c or "hevc" in c

    def is_av1(self) -> bool:
        return "av1" in self.vcodec.lower()


def probe(path: str) -> MediaInfo:
    """ffprobe -v error -print_format json -show_format -show_streams。

    任何失败都降级为 MediaInfo(warnings=...),不抛异常(调用方检查 width)。
    """
    cmd = [find_ffprobe(), "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    try:
        # Windows 下禁止 ffprobe 子进程弹出控制台黑窗(批量导入时逐视频探测)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                           encoding="utf-8", errors="replace",
                           creationflags=flags)
    except FileNotFoundError:
        return MediaInfo(warnings=["找不到 ffprobe，请安装 ffmpeg 并加入 PATH"])
    except subprocess.TimeoutExpired:
        return MediaInfo(warnings=[f"ffprobe 超时:{path}"])

    if r.returncode != 0:
        return MediaInfo(path=path, warnings=[f"ffprobe 失败:{r.stderr.strip()[:200]}"])
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return MediaInfo(path=path, warnings=["ffprobe 输出解析失败"])

    m = MediaInfo(path=path)
    fmt = data.get("format", {})
    m.duration = float(fmt.get("duration") or 0)
    m.file_size = int(fmt.get("size") or 0)
    m.format_bitrate = int(fmt.get("bit_rate")) if fmt.get("bit_rate") else None

    vstream = None
    astream = None
    for st in data.get("streams", []):
        if st.get("codec_type") == "video" and vstream is None:
            vstream = st
        elif st.get("codec_type") == "audio" and astream is None:
            astream = st

    if vstream:
        m.width = int(vstream.get("width") or 0)
        m.height = int(vstream.get("height") or 0)
        m.vcodec = vstream.get("codec_name") or ""
        m.pix_fmt = vstream.get("pix_fmt") or ""
        br = vstream.get("bit_rate")
        m.vbitrate = int(br) if br else None
        avg, rfr = _parse_fps(vstream.get("avg_frame_rate")), _parse_fps(vstream.get("r_frame_rate"))
        m.fps = avg or rfr or 0.0
        m.frame_count = int(vstream.get("nb_frames") or 0)
        m.is_vfr = bool(avg and rfr and abs(avg - rfr) > 0.01)
        # 色彩元数据:新版 ffprobe 在 stream 顶层,老版本在 side_data_list
        m.color_primaries = vstream.get("color_primaries")
        m.color_trc = vstream.get("color_transfer")
        m.colorspace = vstream.get("color_space")
        m.color_range = vstream.get("color_range")
        if not (m.color_primaries or m.color_trc or m.colorspace):
            for sd in vstream.get("side_data_list", []):
                for k in ("color_primaries", "color_transfer", "color_space", "color_range"):
                    if not getattr(m, k):
                        setattr(m, k, sd.get(k))

    if astream:
        m.has_audio = True
        m.acodec = astream.get("codec_name") or ""
        br = astream.get("bit_rate")
        m.abitrate = int(br) if br else None
        m.audio_channels = int(astream.get("channels") or 0)
        m.sample_rate = int(astream.get("sample_rate") or 0)

    return m


# ---- ffprobe → ffmpeg 参数映射 ----

def map_pix_fmt(src: str) -> str | None:
    """源像素格式 → 编码器可接受的格式(yuvj* → yuv* 兜底)。"""
    if not src:
        return None
    mapping = {
        "yuvj420p": "yuv420p", "yuvj422p": "yuv422p", "yuvj444p": "yuv444p",
    }
    return mapping.get(src, src)


def nvenc_pix_fmt(src_pix: str) -> str | None:
    """NVENC 用像素格式:10-bit 源用 p010le,8-bit 用 yuv420p。"""
    base = map_pix_fmt(src_pix) or "yuv420p"
    if "10le" in base or "12le" in base:
        return "p010le"
    if "444" in base:
        return "yuv444p"
    if "422" in base:
        return "yuv422p"
    return "yuv420p"


def color_args(m: MediaInfo) -> list[str]:
    """色彩元数据透传(字段存在且合法才传,避免播放器色偏)。"""
    args: list[str] = []
    pairs = [
        ("color_primaries", m.color_primaries, "-color_primaries"),
        ("color_trc", m.color_trc, "-color_trc"),
        ("colorspace", m.colorspace, "-colorspace"),
        ("color_range", m.color_range, "-color_range"),
    ]
    for _attr, val, opt in pairs:
        if val and val not in ("unknown", "unspecified"):
            args += [opt, val]
    return args
