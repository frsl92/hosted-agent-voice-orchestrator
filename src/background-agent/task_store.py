"""In-process registry of background tasks, keyed by session/conversation id."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from calltrace import elapsed_ms, emit

logger = logging.getLogger(__name__)

# (topic, params) -> speakable summary
WorkerRunner = Callable[[str, dict[str, Any]], Awaitable[str]]


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    FAILED = "failed"


ACTIVE = (TaskStatus.QUEUED, TaskStatus.RUNNING)


def signature(topic: str, params: dict[str, Any]) -> str:
    return f"{topic}:{json.dumps(params, sort_keys=True, default=str)}"


@dataclass
class Task:
    id: str
    session_key: str
    topic: str
    params: dict[str, Any]
    status: TaskStatus = TaskStatus.QUEUED
    result: str | None = None
    error: str | None = None
    delivered: bool = False

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    @property
    def signature(self) -> str:
        return signature(self.topic, self.params)


class TaskStore:
    """Runs worker coroutines as asyncio tasks and hands out each result exactly once."""

    def __init__(self, runner: WorkerRunner) -> None:
        self._runner = runner
        self._tasks: dict[str, Task] = {}
        self._by_session: dict[str, list[str]] = {}
        # Strong refs: the event loop only keeps weak refs to running tasks.
        self._running: set[asyncio.Task[None]] = set()
        self._handles: dict[str, asyncio.Task[None]] = {}

    def launch(self, session_key: str, topic: str, params: dict[str, Any]) -> Task:
        """Start a background task, or return the already-active identical one."""
        candidate = Task(uuid.uuid4().hex[:8], session_key, topic, dict(params))
        for existing in self.active(session_key):
            if existing.signature == candidate.signature:
                return existing
        self._tasks[candidate.id] = candidate
        self._by_session.setdefault(session_key, []).append(candidate.id)
        handle = asyncio.create_task(self._run(candidate), name=f"worker-{topic}-{candidate.id}")
        self._handles[candidate.id] = handle
        self._running.add(handle)
        handle.add_done_callback(self._running.discard)
        return candidate

    async def _run(self, task: Task) -> None:
        task.status = TaskStatus.RUNNING
        started = time.perf_counter()
        emit("task.start", task_id=task.id, topic=task.topic, params=task.params)
        try:
            task.result = (await self._runner(task.topic, task.params)).strip()
            task.status = TaskStatus.COMPLETED
            emit("task.done", task_id=task.id, topic=task.topic, result=task.result, ms=elapsed_ms(started))
        except asyncio.CancelledError:
            task.status = TaskStatus.CANCELLED
            emit("task.cancelled", task_id=task.id, topic=task.topic, ms=elapsed_ms(started))
            raise
        except Exception as exc:  # worker failures must never crash the call
            logger.exception("worker %s failed", task.id)
            task.error = str(exc)
            task.status = TaskStatus.FAILED
            emit("task.failed", task_id=task.id, topic=task.topic, error=task.error, ms=elapsed_ms(started))

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def tasks(self, session_key: str) -> list[Task]:
        return [self._tasks[i] for i in self._by_session.get(session_key, [])]

    def active(self, session_key: str) -> list[Task]:
        return [t for t in self.tasks(session_key) if t.active]

    def collect_completed(self, session_key: str) -> list[Task]:
        """Return finished (completed or failed) tasks not yet delivered; each is returned once."""
        ready = [
            t for t in self.tasks(session_key)
            if not t.delivered and t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
        ]
        for t in ready:
            t.delivered = True
            if t.status == TaskStatus.COMPLETED:
                t.status = TaskStatus.DELIVERED
        return ready

    def cancel(self, session_key: str) -> list[Task]:
        """Cancel all active tasks of a session (explicit customer request only)."""
        cancelled = self.active(session_key)
        for t in cancelled:
            t.status = TaskStatus.CANCELLED
            t.delivered = True
            handle = self._handles.get(t.id)
            if handle and not handle.done():
                handle.cancel()
        return cancelled

    async def drain(self) -> None:
        """Wait for all workers (used by tests / shutdown)."""
        if self._running:
            await asyncio.gather(*list(self._running), return_exceptions=True)
