"""队列容器:Job 实体 + 纯数据操作。

从旧 render_controller 的 _jobs 列表拆出,逻辑(状态机)在 RenderController,
UI 在 QueueWidget——三层各管一段,队列操作可独立单测。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.project import Project


class JobStatus(Enum):
    """任务状态(值 = 旧版字符串,UI 状态色表用)。"""
    QUEUED = "queued"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETRY_PENDING = "retry_pending"


@dataclass
class Job:
    id: str
    project: Project
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0
    message: str = ""


class JobQueue:
    """任务队列容器(纯数据;信号与状态机在 RenderController)。"""

    def __init__(self):
        self._jobs: list[Job] = []

    def add(self, project: Project) -> Job:
        job = Job(f"job{len(self._jobs) + 1}", project)
        self._jobs.append(job)
        return job

    def remove(self, job_id: str) -> bool:
        """移除任务(排队中/已完成可移除;运行中不可——处理中途移除语义不清);返回是否成功。"""
        for j in self._jobs:
            if j.id == job_id:
                if j.status not in (JobStatus.QUEUED, JobStatus.OK):
                    return False
                self._jobs.remove(j)
                return True
        return False

    def reset_cancelled(self) -> None:
        """开始处理:被取消的任务重置为排队(可再次处理)。"""
        for j in self._jobs:
            if j.status is JobStatus.CANCELLED:
                j.status = JobStatus.QUEUED
                j.progress = 0.0
                j.message = ""

    def mark_cancelled_all(self) -> None:
        """取消全部:任务保留在队列(标记 CANCELLED),之后可再次开始处理。"""
        for j in self._jobs:
            if j.status is JobStatus.QUEUED:
                j.status = JobStatus.CANCELLED

    def next_queued(self) -> Job | None:
        for j in self._jobs:
            if j.status is JobStatus.QUEUED:
                return j
        return None

    def find(self, job_id: str) -> Job | None:
        for j in self._jobs:
            if j.id == job_id:
                return j
        return None

    def all(self) -> list[Job]:
        return list(self._jobs)
