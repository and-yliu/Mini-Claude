from __future__ import annotations

import asyncio
from pathlib import Path

from mini_claude.core.trace.record import TraceRecord

class TraceWriter:
    # initialize TraceWriter, path will not be created until start()
    def __init__(self, path: Path):
        self._path = path
        self._queue: asyncio.Queue[TraceRecord] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
    
    # create directory and start drain task
    async def start(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._drain())

    # wait until queue is empty and then 
    async def stop(self) -> None:
        await self._queue.join()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
    
    def emit(self, record: TraceRecord):
        self._queue.put_nowait(record)

    async def _drain(self) -> None:
        with open(self._path, "a") as f:
            while True:
                record: TraceRecord = await self._queue.get()
                try:
                    f.write(record.model_dump_json() + "\n")
                    f.flush()
                finally:
                    self._queue.task_done()