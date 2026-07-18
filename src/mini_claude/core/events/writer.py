from __future__ import annotations

import logging
from pathlib import Path
from typing import IO

from pydantic import BaseModel

from mini_claude.core.events.bus import EventBus

logger = logging.getLogger(__name__)

class EventWriter:
    def __init__(self, path: Path):
        self._path: Path = path
        self._file: IO[str] | None = None

    # open event file for async with
    async def __aenter__(self) -> EventWriter:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._path, "a", encoding="utf-8")
        return self

    # close event file
    async def __aexit__(self, *args: object) -> EventWriter:
        if self._file is not None:
            self._file.close()
            self._file = None
    
    # write success or fail events to the file in json 
    async def handle(self, event: BaseModel) -> None:
        if self._file is None:
            return
        try:
            self._file.write(event.model_dump_json() + "\n")
            self._file.flush()
        except (OSError, ValueError) as e:
            logger.error("EventWriter: failed to write event: %s", e)
    
    # make the handler subscribe to event bus
    def subscribe(self, bus: EventBus):
        bus.subscribe(self.handle)
    


