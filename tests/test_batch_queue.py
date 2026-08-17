"""JobQueue 容器操作(状态机在 test_render_controller,UI 在 QueueWidget)。"""
import unittest

from core.project import Project
from services.batch_queue import JobQueue, JobStatus


class TestJobQueue(unittest.TestCase):
    def test_add_returns_queued_job(self):
        q = JobQueue()
        j = q.add(Project("D:\\x.mp4", 1.0))
        self.assertEqual(j.status, JobStatus.QUEUED)
        self.assertEqual(q.next_queued(), j)
        self.assertIsNone(q.find("nope"))

    def test_remove_only_queued(self):
        q = JobQueue()
        j1, j2, j3 = (q.add(Project(f"D:\\{c}.mp4", 1)) for c in "abc")
        j1.status = JobStatus.RUNNING
        j3.status = JobStatus.OK              # 已完成(用户要求可清理)
        self.assertFalse(q.remove(j1.id), "运行中不可移除")
        self.assertTrue(q.remove(j2.id), "排队中可移除")
        self.assertTrue(q.remove(j3.id), "已完成可移除")
        self.assertEqual(len(q.all()), 1)

    def test_reset_cancelled(self):
        q = JobQueue()
        j1, j2 = q.add(Project("D:\\a.mp4", 1)), q.add(Project("D:\\b.mp4", 1))
        j1.status = JobStatus.CANCELLED
        q.reset_cancelled()
        self.assertEqual(j1.status, JobStatus.QUEUED)
        self.assertEqual(j2.status, JobStatus.QUEUED)

    def test_mark_cancelled_all_only_queued(self):
        q = JobQueue()
        j1, j2, j3 = (q.add(Project(f"D:\\{i}.mp4", 1)) for i in range(3))
        j2.status = JobStatus.RUNNING
        q.mark_cancelled_all()
        self.assertEqual(j1.status, JobStatus.CANCELLED)
        self.assertEqual(j2.status, JobStatus.RUNNING)   # 运行中的不动
        self.assertEqual(j3.status, JobStatus.CANCELLED)


if __name__ == "__main__":
    unittest.main()
