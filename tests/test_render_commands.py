"""render_commands.py 单测(迁移自旧 27 项):命令构建/取偶/码率/色彩映射。

差异:Segment 参数改 Scope;crop 经 project.set_crop(不再有 crop_left 字段)。
"""
import unittest
from unittest import mock

from tests.helpers import make_media
from core.grid import GridLayout
from core.planning import Scope, effective_scopes
from core.project import CopyRule, EncoderSettings, Patch, Project, Rect, Segment
from core.render_commands import (audio_args, build_audio_only_cmd, build_concat_cmd,
                                               build_vfr_fix_cmd,
                                               build_filter_complex, build_mux_original_audio_cmd,
                                               build_preview_cmd, build_segment_cmd,
                                               codec_args, color_args, copy_rule_ops,
                                               overlay_ops, patches_to_px, pix_fmt_args,
                                               verify_output)


def make_project() -> Project:
    pr = Project("D:\\视频\\直播.mp4", 100)
    pr.grid = GridLayout.thirds()
    pr.patches = [Patch(dst=Rect(0.45, 0.2, 0.1, 0.1), src=Rect(0.1, 0.2, 0.1, 0.1),
                        source_tile_idx=0)]
    return pr


def make_scope(project: Project, patch_ids=(), copy_rule_ids=(),
               start=0.0, end=10.0, grid=None) -> Scope:
    """手动构造 scope(测试显式指定生效集合,不依赖工程状态)。"""
    g = grid or project.grid
    return Scope(f"segment:test", start, end, g, "project",
                 tuple(patch_ids), tuple(copy_rule_ids))


class TestFilter(unittest.TestCase):
    def test_single_patch(self):
        fc = build_filter_complex([(192, 216, 192, 108, 864, 216)])
        self.assertEqual(fc, "[0:v]crop=192:108:192:216[p0];[0:v][p0]overlay=864:216[v]")

    def test_multi_patch_chain(self):
        fc = build_filter_complex([(1, 2, 3, 4, 100, 200), (5, 6, 7, 8, 300, 400)])
        self.assertEqual(
            fc,
            "[0:v]crop=3:4:1:2[p0];[0:v]crop=7:8:5:6[p1];"
            "[0:v][p0]overlay=100:200[t0];[t0][p1]overlay=300:400[v]")

    def test_no_patches(self):
        self.assertIsNone(build_filter_complex([]))

    def test_crop_filter(self):
        """左右边缘线裁剪:crop 前缀 + 后续滤镜在裁剪后画面工作。

        overlay main 显式 scale:crop 标签被补丁 crop 与 overlay 多消费者
        共享时 ffmpeg 会把 main 尺寸推断回原始分辨率(裁剪失效)。
        """
        fc = build_filter_complex([(20, 40, 100, 50, 300, 200)], crop=(10, 90, 360))
        self.assertEqual(
            fc,
            "[0:v]crop=80:360:10:0[c];[c]crop=100:50:20:40[p0];"
            "[c]scale=80:360[m];[m][p0]overlay=300:200[v]")
        # 只有裁剪、无补丁
        fc2 = build_filter_complex([], crop=(10, 90, 360))
        self.assertEqual(fc2, "[0:v]crop=80:360:10:0[v]")

    def test_overlay_ops_crop_conversion(self):
        """裁剪坐标转换:op 减去 crop_left,越出裁剪区跳过。

        边缘线全程统一:set_crop 同步全部网格首/尾边。
        """
        pr = make_project()
        pr.set_crop(0.05, 0.95)                 # left_px = 96
        scope = make_scope(pr, patch_ids=[pr.patches[0].id])
        ops = overlay_ops(pr, scope, 1920, 1080)
        self.assertEqual(len(ops), 1)
        sx, sy, sw, sh, dx, dy = ops[0][:6]
        self.assertEqual(sx, 192 - 96)      # 源矩形 x=192 → 96
        self.assertEqual(dx, 864 - 96)      # 目标 x=864 → 768
        # 越出裁剪区的补丁被跳过
        pr.patches[0].dst = Rect(0.02, 0.2, 0.1, 0.1)   # x=38 < left=96
        self.assertEqual(overlay_ops(pr, scope, 1920, 1080), [])


class TestPatchesToPx(unittest.TestCase):
    def test_even_and_same_size(self):
        pr = make_project()
        pxs = patches_to_px(pr, [pr.patches[0].id], 1920, 1080)
        self.assertEqual(len(pxs), 1)
        sx, sy, sw, sh, dx, dy = pxs[0][:6]
        self.assertEqual((sx % 2, sy % 2, sw % 2, sh % 2, dx % 2, dy % 2),
                         (0, 0, 0, 0, 0, 0))
        self.assertEqual((sw, sh), (192, 108))

    def test_skip_unknown_patch(self):
        pr = make_project()
        self.assertEqual(patches_to_px(pr, ["nope"], 1920, 1080), [])

    def test_overlay_ops_multi_target_patch(self):
        """多目标补丁:同一 src 展开到主格与额外格。"""
        pr = make_project()
        pr.patches[0].dst_tile_idx = 1
        pr.patches[0].extra_tile_indices = [2]
        scope = make_scope(pr, patch_ids=[pr.patches[0].id])
        ops = overlay_ops(pr, scope, 1920, 1080)
        self.assertEqual(len(ops), 2)
        # 同一源矩形、两个不同目标位置
        self.assertEqual((ops[0][0], ops[0][1]), (ops[1][0], ops[1][1]))
        self.assertNotEqual((ops[0][3], ops[0][4]), (ops[1][3], ops[1][4]))

    def test_copy_rule_ops(self):
        """复制规则:来源格整格 → 多目标格。"""
        pr = make_project()
        r = CopyRule(source_tile_idx=0, target_tile_indices=[1, 2])
        r.id = "r1"
        pr.copy_rules = [r]
        scope = make_scope(pr, copy_rule_ids=("r1",))
        ops = copy_rule_ops(pr, scope, 1920, 1080)
        self.assertEqual(len(ops), 2)
        # 来源格整格 (0,0,640,1080) 取偶 → (0,0,640,1080)
        self.assertEqual((ops[0][0], ops[0][1], ops[0][2], ops[0][3]), (0, 0, 640, 1080))
        # 目标格 x 坐标 = 640 / 1280
        self.assertEqual(ops[0][4], 640)
        self.assertEqual(ops[1][4], 1280)

    def test_copy_rule_ops_skips_missing_grid(self):
        pr = make_project()
        r = CopyRule(source_tile_idx=5, target_tile_indices=[1])
        r.id = "r1"
        pr.copy_rules = [r]
        scope = make_scope(pr, copy_rule_ids=("r1",))
        self.assertEqual(copy_rule_ops(pr, scope, 1920, 1080), [])

    def test_overlay_ops_with_copy_rules(self):
        """overlay_ops = 补丁 + 复制规则 合并展开。"""
        pr = make_project()
        r = CopyRule(source_tile_idx=0, target_tile_indices=[2])
        r.id = "r1"
        pr.copy_rules = [r]
        scope = make_scope(pr, patch_ids=[pr.patches[0].id], copy_rule_ids=("r1",))
        ops = overlay_ops(pr, scope, 1920, 1080)
        self.assertEqual(len(ops), 2)   # 1 补丁 + 1 复制


class TestCodecArgs(unittest.TestCase):
    def test_nvenc_h264(self):
        m = make_media()
        s = EncoderSettings(encoder_mode="hw", quality_mode="match")
        args = codec_args(m, s)
        self.assertIn("h264_nvenc", args)
        self.assertIn("-b:v", args)
        self.assertIn("6200k", args)          # 源 6.2 Mbps
        self.assertIn("-maxrate", args)
        self.assertIn("-multipass", args)

    def test_nvenc_hevc(self):
        m = make_media(vcodec="hevc", vbitrate=8_000_000)
        s = EncoderSettings(encoder_mode="hw")
        self.assertIn("hevc_nvenc", codec_args(m, s))

    def test_nvenc_crf_uses_cq(self):
        """CRF 模式才允许 -cq;码率目标模式绝无 -cq(NVENC vbr+cq 冲满 maxrate)。"""
        m = make_media()
        s = EncoderSettings(encoder_mode="hw", quality_mode="crf", crf=18)
        args = codec_args(m, s)
        self.assertIn("-cq", args)
        self.assertIn("-b:v", args)
        s2 = EncoderSettings(encoder_mode="hw", quality_mode="match")
        args2 = codec_args(m, s2)
        self.assertNotIn("-cq", args2, "码率目标模式绝不能带 -cq")

    def test_software_crf(self):
        m = make_media()
        s = EncoderSettings(encoder_mode="sw", quality_mode="crf", crf=18)
        args = codec_args(m, s)
        self.assertIn("libx264", args)
        self.assertIn("-crf", args)
        self.assertIn("18", args)
        self.assertNotIn("-b:v", args)

    def test_custom_bitrate(self):
        m = make_media()
        s = EncoderSettings(encoder_mode="sw", quality_mode="custom", custom_kbps=4000)
        args = codec_args(m, s)
        self.assertIn("-b:v", args)
        self.assertIn("4000k", args)

    def test_bitrate_factor(self):
        m = make_media(vbitrate=6_000_000)
        s = EncoderSettings(quality_mode="match", factor=0.8)
        args = codec_args(m, s)
        self.assertIn("4800k", args)


class TestAuxArgs(unittest.TestCase):
    def test_audio_copy_default(self):
        m = make_media()
        self.assertEqual(audio_args(m, EncoderSettings()), ["-c:a", "copy"])

    def test_audio_reencode(self):
        m = make_media()
        s = EncoderSettings(audio_reencode=True, audio_kbps=0)
        args = audio_args(m, s)
        self.assertIn("aac", args)
        self.assertIn("211k", args)           # 192k 源 + 10% 重编码余量

    def test_color_args(self):
        m = make_media()
        args = color_args(m)
        self.assertEqual(args, ["-color_primaries", "bt709", "-color_trc", "bt709",
                                "-colorspace", "bt709", "-color_range", "limited"])

    def test_color_args_empty(self):
        m = make_media(color_primaries=None, color_trc=None, colorspace=None,
                       color_range=None)
        self.assertEqual(color_args(m), [])

    def test_pix_fmt_10bit_nvenc(self):
        m = make_media(pix_fmt="yuv420p10le")
        self.assertIn("p010le", pix_fmt_args(m, EncoderSettings(encoder_mode="hw")))


class TestSegmentCmd(unittest.TestCase):
    def test_command_structure(self):
        m = make_media()
        s = EncoderSettings(encoder_mode="sw", quality_mode="match")
        scope = Scope("segment:test", 10.0, 22.5, GridLayout.thirds(), "project", ("p1",))
        cmd = build_segment_cmd("ffmpeg", m, scope, [(192, 216, 192, 108, 864, 216)],
                                s, "D:\\in.mp4", "D:\\seg.mp4")
        self.assertEqual(cmd[0], "ffmpeg")
        self.assertIn("-ss", cmd)
        self.assertIn("10", cmd)
        self.assertIn("-t", cmd)
        self.assertIn("12.5", cmd)
        i_ss = cmd.index("-ss")
        i_i = cmd.index("-i")
        # -ss 必须在 -i 之后(输出端 seek):输入 seek + -t + B 帧会多丢帧
        # (拼接点空洞根因,见 test_junction_no_hole_mid_gop)
        self.assertGreater(i_ss, i_i, "-ss 必须在 -i 之后(输出端 seek)")
        self.assertIn("crop=192:108:192:216", cmd[cmd.index("-filter_complex") + 1])
        self.assertIn("-map", cmd)
        self.assertIn("[v]", cmd)
        self.assertIn("6200k", cmd)
        self.assertIn("9300k", cmd)           # maxrate 1.5x
        self.assertIn("12400k", cmd)          # bufsize 2x
        self.assertIn("-c:a", cmd)
        self.assertIn("copy", cmd)
        self.assertIn("-xerror", cmd)
        self.assertIn("-progress", cmd)
        self.assertIn("pipe:1", cmd)
        self.assertIn("-color_primaries", cmd)
        self.assertEqual(cmd[-1], "D:\\seg.mp4")

    def test_no_patch_segment_maps_0v(self):
        m = make_media()
        scope = Scope("segment:test", 0.0, 5.0, GridLayout.thirds(), "project")
        cmd = build_segment_cmd("ffmpeg", m, scope, [],
                                EncoderSettings(), "in.mp4", "out.mp4")
        self.assertNotIn("-filter_complex", cmd)
        self.assertIn("-map", cmd)
        self.assertIn("0:v", cmd)


class TestConcatPreview(unittest.TestCase):
    def test_concat(self):
        cmd = build_concat_cmd("ffmpeg", "C:\\t\\list.txt", "C:\\out.mp4")
        self.assertIn("-f", cmd)
        self.assertIn("concat", cmd)
        self.assertIn("-safe", cmd)
        self.assertIn("-c", cmd)
        self.assertIn("copy", cmd)

    def test_preview(self):
        cmd = build_preview_cmd("ffmpeg", "in.mp4", 42.0, [(1, 2, 3, 4, 5, 6)], "p.png")
        self.assertIn("-ss", cmd)
        self.assertIn("42", cmd)
        self.assertIn("-frames:v", cmd)
        self.assertIn("crop=3:4:1:2", cmd[cmd.index("-filter_complex") + 1])
        self.assertEqual(cmd[-1], "p.png")


class TestVerify(unittest.TestCase):
    def test_verify_mismatch_detected(self):
        m = make_media()
        # 伪造一个 probe 结果:分辨率/码率/时长都不一致
        fake = make_media(width=1280, height=720, vcodec="h264",
                          vbitrate=3_000_000, duration=90.0)
        with mock.patch("core.ffprobe.probe", return_value=fake):
            warns = verify_output(m, "out.mp4", EncoderSettings(quality_mode="match"))
        texts = "\n".join(warns)
        self.assertIn("分辨率", texts)
        self.assertIn("码率", texts)
        self.assertIn("时长", texts)

    def test_verify_frame_count_warns_only(self):
        m = make_media(frame_count=120)
        fake = make_media(frame_count=121)
        with mock.patch("core.ffprobe.probe", return_value=fake):
            warns = verify_output(m, "out.mp4", EncoderSettings(),
                                  expected_frame_count=120)
        self.assertTrue(any("帧数不一致" in w for w in warns), warns)
        # 只告警,verify_output 本身不抛错/不阻断
        with mock.patch("core.ffprobe.probe", return_value=m):
            self.assertEqual(verify_output(m, "out.mp4", EncoderSettings(),
                                           expected_frame_count=120), [])

    def test_verify_ok(self):
        m = make_media()
        with mock.patch("core.ffprobe.probe", return_value=m):
            self.assertEqual(verify_output(m, "out.mp4", EncoderSettings()), [])


class TestFrameAccurateCommands(unittest.TestCase):
    def test_short_scope_not_extended_and_frame_count(self):
        """帧号方案:不再 max(0.05),高精度 -ss/-t,CFR 带 -frames:v。"""
        m = make_media(fps=60.0)
        scope = Scope("segment:test", 0.0, 1 / 60, GridLayout.thirds(), "project",
                      start_frame=0, end_frame=1)
        cmd = build_segment_cmd("ffmpeg", m, scope, [], EncoderSettings(),
                                "in.mp4", "out.mp4")
        self.assertNotIn("0.050", cmd)
        self.assertIn("0.016666667", cmd)      # fmt_ts 高精度
        self.assertIn("-frames:v", cmd)
        self.assertEqual(cmd[cmd.index("-frames:v") + 1], "1")

    def test_vfr_select_by_frame_exact(self):
        """VFR 分段:不用 -ss/-t,按帧号 select + setpts 精确取帧。"""
        m = make_media(fps=60.0, is_vfr=True)
        scope = Scope("segment:test", 0.1, 1.0, GridLayout.thirds(), "project",
                      start_frame=10, end_frame=29)
        cmd = build_segment_cmd("ffmpeg", m, scope, [], EncoderSettings(),
                                "in.mp4", "out.mp4")
        self.assertNotIn("-ss", cmd)
        self.assertNotIn("-t", cmd)
        self.assertNotIn("-frames:v", cmd)
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("select='between(n,10,28)'", fc)
        self.assertIn("setpts=PTS-STARTPTS", fc)

    def test_vfr_fix_cmd_sets_last_duration(self):
        cmd = build_vfr_fix_cmd("ffmpeg", "raw.mp4", "fixed.mp4", 84000, 6000)
        self.assertIn("-c", cmd)
        self.assertIn("copy", cmd)
        self.assertEqual(cmd[cmd.index("-bsf:v") + 1],
                         "setts=duration='if(eq(PTS,84000),6000,DURATION)'")

    def test_vfr_segment_timescale_option(self):
        m = make_media(fps=60.0, is_vfr=True)
        scope = Scope("segment:test", 0.1, 1.0, GridLayout.thirds(), "project",
                      start_frame=10, end_frame=29)
        cmd = build_segment_cmd("ffmpeg", m, scope, [], EncoderSettings(),
                                "in.mp4", "out.mp4", video_timescale=90000)
        self.assertEqual(cmd[cmd.index("-video_track_timescale") + 1], "90000")

    def test_audio_atrim_sample_accurate(self):
        m = make_media()
        cmd = build_audio_only_cmd("ffmpeg", m, EncoderSettings(audio_reencode=True),
                                   "in.mp4", "a.m4a", 2.5, 10.0,
                                   start_sample=120000, end_sample=480000)
        self.assertIn("atrim=start_sample=120000:end_sample=480000,asetpts=PTS-STARTPTS", cmd)
        self.assertNotIn("-ss", cmd)
        self.assertNotIn("-t", cmd)

    def test_vfr_no_frame_limit(self):
        m = make_media(fps=60.0, is_vfr=True)
        scope = Scope("segment:test", 0.0, 1.0, GridLayout.thirds(), "project")
        cmd = build_segment_cmd("ffmpeg", m, scope, [], EncoderSettings(),
                                "in.mp4", "out.mp4")
        self.assertNotIn("-frames:v", cmd)

    def test_copy_flip_uses_anchor_grid_and_hflip(self):
        pr = Project("D:\\in.mp4", 100)
        pr.grid = GridLayout.thirds()
        pr.segments = [Segment(id="A", start=0, end=50,
                               copy_rule_ids=["r1"], grid=GridLayout.halves())]
        # 规则锚定全局三格,即使当前段是两格,仍按三格渲染
        pr.copy_rules = [CopyRule(id="r1", source_tile_idx=0, target_tile_indices=[2],
                                  flip_horizontal=True)]
        scope = effective_scopes(pr, 30.0)[0]
        ops = copy_rule_ops(pr, scope, 1920, 1080)
        self.assertEqual(len(ops), 1)
        self.assertTrue(ops[0].flip_x)
        fc = build_filter_complex(ops)
        self.assertIn("crop=640:1080:0:0,hflip[p0]", fc)

    def test_audio_only_and_mux_cmds(self):
        m = make_media()
        cmd = build_audio_only_cmd("ffmpeg", m, EncoderSettings(audio_reencode=True),
                                   "in.mp4", "a.m4a", 2.5, 10.0)
        self.assertIn("-vn", cmd)
        self.assertIn("-map", cmd)
        self.assertIn("aac", cmd)
        self.assertNotIn("-frames:v", cmd)
        self.assertEqual(cmd[-1], "a.m4a")
        mux = build_mux_original_audio_cmd("ffmpeg", "v.mp4", "orig.mp4", "out.mp4")
        self.assertIn("1:a?", mux)
        self.assertIn("-c", mux)
        self.assertIn("copy", mux)


if __name__ == "__main__":
    unittest.main()
