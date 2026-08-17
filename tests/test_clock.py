"""新增结构性测试:PlaybackClock 显式状态机 + StepPlanner 步进意图。

这两块是旧版"靠调用顺序钉住"的不变式(_clock_paused_at 清理、步进三路分支)
的显式化,性质测试保证暂停残留/越界在结构上不可能。
"""
import time
import unittest

import numpy as np

from services.decoder import PlaybackClock, StepPlanner, _rate_samples


class TestPlaybackClock(unittest.TestCase):
    def test_reset_then_now_near_zero(self):
        c = PlaybackClock()
        c.reset_to(42.0)
        self.assertAlmostEqual(c.now(), 42.0, delta=0.05)

    def test_pause_freezes_resume_continues(self):
        """暂停期间墙钟流逝不计入播放位置。"""
        c = PlaybackClock()
        c.reset_to(0.0)
        time.sleep(0.05)
        c.pause()
        frozen = c.now()
        time.sleep(0.1)                 # 暂停 0.1s
        self.assertAlmostEqual(c.now(), frozen, delta=0.02, msg="暂停应冻结位置")
        c.resume()
        time.sleep(0.05)
        self.assertGreater(c.now(), frozen, "恢复后继续推进")
        self.assertLess(c.now(), frozen + 0.15, "暂停时长不应虚增")

    def test_reset_absorbs_pause_history(self):
        """重建(reset_to)吸收一切历史暂停——旧"必须同时清 _clock_paused_at"不变式。"""
        c = PlaybackClock()
        c.reset_to(10.0)
        c.pause()
        time.sleep(0.1)
        c.reset_to(20.0)                # seek/step 定位完成
        self.assertAlmostEqual(c.now(), 20.0, delta=0.05, msg="重建后暂停不得残留")
        c.resume()                      # 若暂停状态残留,这里会虚增 0.1s
        self.assertAlmostEqual(c.now(), 20.0, delta=0.05, msg="resume 不得虚增已吸收的暂停")

    def test_pause_idle_is_noop(self):
        c = PlaybackClock()
        c.pause()                       # idle 时 no-op
        self.assertEqual(c.state, PlaybackClock.State.IDLE)
        self.assertEqual(c.now(), 0.0)

    def test_wait_until_establishes_clock(self):
        c = PlaybackClock()
        t0 = time.perf_counter()
        c.wait_until(5.0, abort=lambda: False)   # 未建立 → 以 5.0 为基准建立
        self.assertAlmostEqual(c.now(), 5.0, delta=0.05)
        self.assertLess(time.perf_counter() - t0, 0.1, "建立时钟不应等待")

    def test_wait_until_abort_returns_early(self):
        c = PlaybackClock()
        c.reset_to(0.0)
        t0 = time.perf_counter()
        c.wait_until(10.0, abort=lambda: True)   # 有排队命令 → 立即返回
        self.assertLess(time.perf_counter() - t0, 0.1)


class TestStepPlanner(unittest.TestCase):
    def setUp(self):
        self.p = StepPlanner(fallback_fps=30.0)
        self.tb = 1 / 15360             # 时间基(常见 mp4)

    def _pts(self, seconds: float) -> int:
        return int(round(seconds / self.tb))

    def test_note_frame_keeps_three(self):
        for i in range(5):
            self.p.note_frame(i)
        self.assertEqual(self.p.hist_pts, [2, 3, 4])

    def test_measured_interval_uses_real_delta(self):
        self.p.note_frame(1000)
        self.p.note_frame(1032)         # VFR:32 pts 间隔
        self.assertEqual(self.p.measured_interval(self.tb), 32)

    def test_backward_uses_real_pts(self):
        """后退直接取目标帧真实 pts(VFR 精确)。"""
        hist = [1000, 1032, 1051]
        for h in hist:
            self.p.note_frame(h)
        target = self.p.plan(-1, last_emit_pts=1051 * self.tb, tb=self.tb,
                             waiting_seek=False, seek_pending=False)
        self.assertEqual(target, 1032, "后退应为上一帧真实 pts")

    def test_backward_short_history_estimates(self):
        """历史不足(如 seek 刚完成连点 -2)→ 估算不越界(回归:曾静默死亡)。"""
        self.p.note_frame(1000)
        target = self.p.plan(-2, last_emit_pts=1000 * self.tb, tb=self.tb,
                             waiting_seek=False, seek_pending=False)
        # 估算:1000 - 2*interval(无 2 样本 → 1/30s 兜底)≥ 0
        self.assertEqual(target, max(0, 1000 - 2 * self._pts(1 / 30)))

    def test_waiting_seek_merges_pending(self):
        """定位途中连点:合并到 pending,不打断当前解码。"""
        self.p.nav_target = 1000
        self.p.note_frame(1000)
        target = self.p.plan(3, last_emit_pts=1000 * self.tb, tb=self.tb,
                             waiting_seek=True, seek_pending=False)
        self.assertEqual(self.p.pending, target)
        self.assertEqual(target, 1000 + 3 * self.p.measured_interval(self.tb))

    def test_seek_pending_chains_on_nav_target(self):
        """同批连点:以最后请求的目标为基准累计(实测间隔)。"""
        self.p.nav_target = 1000
        self.p.note_frame(1000)
        self.p.note_frame(1032)
        t1 = self.p.plan(1, 1032 * self.tb, self.tb,
                         waiting_seek=False, seek_pending=True)
        self.assertEqual(t1, 1000 + 32)         # 基准 = nav_target(最后请求的目标)
        t2 = self.p.plan(1, 1032 * self.tb, self.tb,
                         waiting_seek=False, seek_pending=True)
        self.assertEqual(t2, t1 + 32, "连点应在 nav_target 上累计")

    def test_clear_pending(self):
        self.p.pending = 999
        self.p.clear_pending()
        self.assertIsNone(self.p.pending)

    def test_fill_from_seek_pass(self):
        self.p.fill_from_seek_pass([100, 150, 200, 250])
        self.assertEqual(self.p.hist_pts, [150, 200, 250])


class TestRateCommand(unittest.TestCase):
    """倍率切换命令:时钟锚点按新倍率重设(降倍率曾卡住画面)。"""

    def test_rate_resets_clock_anchor(self):
        """3x → 0.5x:锚点重设后 now() = 当前帧pts/新倍率,不等"播放位置"秒。"""
        from services.decoder import DecodeWorker

        worker = DecodeWorker()
        worker._clock.reset_to(50.0)        # 旧倍率 3x 稳定:now()=50s
        worker._last_emit_pts = 150.0       # 当前帧 pts = 150s
        worker.set_rate(0.5)
        worker._drain_commands()            # 白盒:直接消费命令
        self.assertEqual(worker._rate, 0.5)
        self.assertAlmostEqual(worker._clock.now(), 300.0, delta=0.05,
                               msg="now() 应从 150/0.5=300s 继续(旧实现要等 150s+)")
        worker.stop()

    def test_rate_keeps_paused_state(self):
        """暂停中切倍率:暂停态保持,冻结位置按新倍率缩放。"""
        from services.decoder import DecodeWorker

        worker = DecodeWorker()
        worker._clock.reset_to(50.0)
        worker._clock.pause()
        worker._last_emit_pts = 150.0
        worker.set_rate(2.0)
        worker._drain_commands()
        self.assertTrue(worker._clock.state is PlaybackClock.State.PAUSED,
                        "暂停态应保持")
        self.assertAlmostEqual(worker._clock.now(), 75.0, delta=0.05,
                               msg="暂停冻结位置 = 150/2 = 75s")
        worker.stop()


class TestRateSamples(unittest.TestCase):
    """倍速音频样本抽稀/重复(交错 (samples, channels) 数组)。"""

    def test_rate_1_unchanged(self):
        arr = np.array([[1, 2], [3, 4], [5, 6]], dtype=np.float32)   # 3 样本 2 声道
        out = _rate_samples(arr, 1.0)
        self.assertIs(out, arr, "1x 应原样返回")

    def test_rate_2_decimates(self):
        arr = np.array([[1, 2], [3, 4], [5, 6], [7, 8]], dtype=np.float32)
        out = _rate_samples(arr, 2.0)
        self.assertEqual(out.shape, (2, 2), "2x 样本数减半,声道保持")
        np.testing.assert_array_equal(out, arr[::2])

    def test_rate_3_decimates(self):
        arr = np.arange(9 * 2, dtype=np.float32).reshape(9, 2)
        out = _rate_samples(arr, 3.0)
        self.assertEqual(out.shape, (3, 2))
        np.testing.assert_array_equal(out, arr[::3])

    def test_rate_half_repeats(self):
        arr = np.array([[1, 2], [3, 4]], dtype=np.float32)
        out = _rate_samples(arr, 0.5)
        self.assertEqual(out.shape, (4, 2), "0.5x 样本数翻倍,声道保持")
        np.testing.assert_array_equal(out, np.repeat(arr, 2, axis=0))

    def test_emits_int16_correct_length(self):
        """端到端:抽稀后的数组转 int16 字节长度 = 样本数 × 声道 × 2。"""
        arr = np.full((100, 2), 0.5, dtype=np.float32)
        for r, expected in ((2.0, 50), (3.0, 34), (0.5, 200)):
            out = _rate_samples(arr, r)
            data = (np.clip(out, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
            self.assertEqual(len(data), expected * 2 * 2, f"{r}x 字节长度")


if __name__ == "__main__":
    unittest.main()
