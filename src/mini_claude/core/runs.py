from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

RUNS_DIR = Path("runs")


# return run dir for a specific run id
def run_dir(run_id: str) -> Path:
    return RUNS_DIR / run_id


# return the event log for a run_id
def events_file(run_id: str) -> Path:
    return run_dir(run_id) / "events.jsonl"


# generate a YYYYMMDD-HHMMSS-xxxxxx run ID that is unique
def new_run_id() -> str:
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"{ts}-{suffix}"


# create the run_dir for a run id
def ensure_run_dir(run_id: str) -> Path:
    path = run_dir(run_id)
    path.mkdir(parents=True, exist_ok=True)
    return path