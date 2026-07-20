from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from mini_claude.core.task.model import Task, TaskStatus


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TaskManager:
    # make sure tasks_dir exist, and the next id based on current tasks
    def __init__(self, tasks_dir: Path):
        self._dir = tasks_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._next_id = self._max_id() + 1

    # return the max current id
    def _max_id(self) -> int:
        ids = []
        for file in self._dir.glob("task_*.json"):
            task_id = file.stem().split("_")[1]
            if task_id.isdigit():
                ids.append(task_id)
        
        return max(ids) if ids else 0
    
    # load task from file
    def _load(self, task_id: int) -> Task:
        path = self._dir / f"task_{task_id}.json"
        if not path.exists():
            raise ValueError(f"task {task_id} not found")
        return Task.from_dict(json.loads(path.read_text()))
    
    # same task to file
    def _save(self, task: Task) -> None:
        path = self._dir / f"task_{task.id}.json"
        path.write_text(json.dumps(task.to_dict(), indent=2, ensure_ascii=False))

    # create task and save to json file and return task object
    def create(self, subject: str, description: str = "", blocked_by: list[int] | None = None) -> Task:
        for dep_id in blocked_by:
            if not (self._dir / f"task_{dep_id}.json").exists():
                raise ValueError(f"blocked_by task {dep_id} not found")
        
        now = _now()
        task = Task(
            id=self._next_id,
            subject=subject,
            description=description,
            status="pending",
            created_at=now,
            updated_at=now
        )

        self._save(task)
        self._next_id += 1

        return task

    # get a specific task
    def get(self, task_id: int) -> Task:
        return self._load(task_id)
    
    # update task status and block information
    def update(
        self,
        task_id: int,
        *,
        status: TaskStatus | None = None,
        add_blocked_by: list[int] | None = None,
        remove_blocked_by: list[int] | None = None
    ) -> Task:
        task = self._load(task_id)
        if status is not None:
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"invalid status: {status!r}")
            task.status = status
            if status == "completed":
                self._clear_dependency(task_id)
        if add_blocked_by:
            task.blocked_by = list(set(task.blocked_by + add_blocked_by))
        if remove_blocked_by:
            task.blocked_by = list(set(task.blocked_by) - set(remove_blocked_by))
        
        task.updated_at=_now()
        self._save(task)
        return task

    # list of task related to a run
    def list_all(self) -> list[Task]:
        tasks = []
        for f in sorted(self._dir.glob("task_*.json"), key=lambda p: int(p.stem.split("_")[1])):
            try:
                tasks.append(Task.from_dict(json.loads(f.read_text())))
            except (ValueError, KeyError):
                pass
        return tasks
    
    # clear dependency after a task is done
    def _clear_dependency(self, task_id: str) -> None:
        for f in self._dir.glob("task_*.json"):
            try:
                task = json.loads(f.read_text())
            except (ValueError, json.JSONDecodeError):
                pass
            
            blocked_by = [int(x) for x in task.get("blocked_by", [])]
            if task_id in blocked_by:
                task["blocked_by"] = [x for x in blocked_by if x != task_id]
                task["updated_at"] = _now()
            
            f.write_text(json.dumps(task, indent=2, ensure_ascii=False))

    # format list to agent     
    def format_list(self) -> str:
        tasks = self.list_all()
        if not tasks:
            return "No tasks."
        marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        lines = []
        for t in tasks:
            blocked = f" (blocked by: {t.blocked_by})" if t.blocked_by else ""
            lines.append(f"{marker.get(t.status, '[?]')} #{t.id}: {t.subject}{blocked}")
        return "\n".join(lines)

    

