"""测试共享夹具。

make_media:MediaInfo 参数化构造(原两份拷贝默认值已漂移,收敛一处)。
make_project/make_scope 保持各测试文件原位——各域的默认值不同,
统一签名反而需要调用点显式传参;本文件只放真正同语义的共享夹具。
"""
from __future__ import annotations

from core.ffprobe import MediaInfo


def make_media(**kw) -> MediaInfo:
    base = dict(path="D:\\视频\\直播.mp4", width=1920, height=1080, duration=100.0,
                fps=30.0, vcodec="h264", vbitrate=6_200_000, pix_fmt="yuv420p",
                has_audio=True, acodec="aac", abitrate=192_000, sample_rate=48000,
                color_primaries="bt709", color_trc="bt709", colorspace="bt709",
                color_range="limited")
    base.update(kw)
    return MediaInfo(**base)
