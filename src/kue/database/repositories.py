"""All reads/writes to SQLite go through Store."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from kue.database.database import transaction
from kue.events.logger import EventType, redact
from kue.orchestration.state_machine import NEEDS_ATTENTION, assert_transition
from kue.tasks.models import (
    Event,
    Run,
    Task,
    TaskProposal,
    TaskStatus,
)

# Fields that may be changed through update_task / transition(**fields).
_UPDATABLE = frozenset(
    {
        "title", "description", "repository", "priority", "attempt_count",
        "current_run_id", "branch_name", "original_branch", "pr_number",
        "asana_task_id", "discord_message_id", "autonomy_policy", "failure_reason",
    }
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


class TaskNotFound(LookupError):
    pass


class Store:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ---------------------------------------------------------------- tasks
    def find_by_external(self, source: str, external_id: str) -> Task | None:
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE source = ? AND external_id = ?", (source, external_id)
        ).fetchone()
        return Task.model_validate(dict(row)) if row else None

    def upsert_proposal(self, p: TaskProposal) -> tuple[Task, bool]:
        """Create a task, or just mark it seen if (source, external_id) already exists."""
        now = iso(utcnow())
        with transaction(self.conn):
            existing = self.find_by_external(p.source, p.external_id)
            if existing:
                self.conn.execute(
                    "UPDATE tasks SET last_seen_at = ? WHERE id = ?", (now, existing.id)
                )
                return self._require(existing.id), False
            cur = self.conn.execute(
                """INSERT INTO tasks (external_id, source, title, description, repository,
                       priority, status, created_at, updated_at, last_seen_at,
                       asana_task_id, discord_message_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    p.external_id, p.source, p.title, p.description, p.repository,
                    p.priority, TaskStatus.DISCOVERED.value, now, now, now,
                    p.asana_task_id, p.discord_message_id,
                ),
            )
            task_id = cur.lastrowid
            self._insert_event(
                EventType.TASK_CREATED,
                task_id,
                {"source": p.source, "external_id": p.external_id, "title": p.title},
            )
        return self._require(task_id), True

    def get_task(self, task_id: int) -> Task | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return Task.model_validate(dict(row)) if row else None

    def _require(self, task_id: int) -> Task:
        task = self.get_task(task_id)
        if task is None:
            raise TaskNotFound(f"Task #{task_id} does not exist.")
        return task

    def list_tasks(
        self, statuses: Iterable[TaskStatus] | None = None, limit: int | None = None
    ) -> list[Task]:
        sql, params = "SELECT * FROM tasks", []
        if statuses is not None:
            values = [TaskStatus(s).value for s in statuses]
            if not values:
                return []
            sql += f" WHERE status IN ({','.join('?' * len(values))})"
            params.extend(values)
        sql += " ORDER BY priority ASC, created_at ASC, id ASC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [Task.model_validate(dict(r)) for r in self.conn.execute(sql, params)]

    def update_task(self, task_id: int, **fields: Any) -> Task:
        with transaction(self.conn):
            self._require(task_id)
            self._update(task_id, fields)
        return self._require(task_id)

    def transition(
        self, task_id: int, target: TaskStatus, *, reason: str | None = None, **fields: Any
    ) -> Task:
        """Validate and apply a status change, recording an event atomically."""
        target = TaskStatus(target)
        with transaction(self.conn):
            task = self._require(task_id)
            assert_transition(task.status, target)
            if reason and target in NEEDS_ATTENTION | {TaskStatus.FAILED}:
                fields.setdefault("failure_reason", reason)
            self._update(task_id, fields, status=target)
            self._insert_event(
                EventType.TASK_STATUS_CHANGED,
                task_id,
                {"from": task.status.value, "to": target.value, "reason": reason},
            )
        return self._require(task_id)

    def _update(self, task_id: int, fields: dict[str, Any], status: TaskStatus | None = None) -> None:
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"Cannot update task fields: {sorted(unknown)}")
        values = dict(fields)
        if status is not None:
            values["status"] = status.value
        values["updated_at"] = iso(utcnow())
        assignments = ", ".join(f"{k} = ?" for k in values)
        self.conn.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?", [*values.values(), task_id]
        )

    def count_by_status(self) -> dict[TaskStatus, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")
        return {TaskStatus(s): n for s, n in rows}

    def count_completed_since(self, since: datetime) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = ? AND updated_at >= ?",
            (TaskStatus.COMPLETED.value, iso(since)),
        ).fetchone()[0]

    # --------------------------------------------------------------- events
    def add_event(
        self, event_type: str, task_id: int | None = None, payload: dict[str, Any] | None = None
    ) -> None:
        self._insert_event(event_type, task_id, payload)

    def _insert_event(
        self, event_type: str, task_id: int | None, payload: dict[str, Any] | None
    ) -> None:
        self.conn.execute(
            "INSERT INTO events (task_id, timestamp, event_type, payload) VALUES (?, ?, ?, ?)",
            (task_id, iso(utcnow()), event_type, json.dumps(redact(payload or {}), default=str)),
        )

    def list_events(
        self, task_id: int | None = None, since: datetime | None = None, limit: int = 50
    ) -> list[Event]:
        sql, clauses, params = "SELECT * FROM events", [], []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(iso(since))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        events = []
        for row in self.conn.execute(sql, params):
            data = dict(row)
            data["payload"] = json.loads(data["payload"] or "{}")
            events.append(Event.model_validate(data))
        return events

    def count_events(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    # ----------------------------------------------------------------- runs
    def start_run(self, task_id: int, agent: str, command: str | None = None) -> Run:
        cur = self.conn.execute(
            "INSERT INTO runs (task_id, started_at, status, agent, command) VALUES (?, ?, ?, ?, ?)",
            (task_id, iso(utcnow()), "RUNNING", agent, command),
        )
        return self.get_run(cur.lastrowid)  # type: ignore[return-value]

    def get_run(self, run_id: int) -> Run | None:
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return Run.model_validate(dict(row)) if row else None

    def list_runs(self, limit: int = 20) -> list[Run]:
        rows = self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        return [Run.model_validate(dict(r)) for r in rows]

    def count_runs_since(self, since: datetime) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM runs WHERE started_at >= ?", (iso(since),)
        ).fetchone()[0]

    # ---------------------------------------------------------- daemon state
    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM daemon_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key: str, value: str | None) -> None:
        self.conn.execute(
            """INSERT INTO daemon_state (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (key, value, iso(utcnow())),
        )

    def is_paused(self) -> bool:
        return self.get_state("paused") == "1"

    # -------------------------------------------------------------- cursors
    def get_cursor(self, source: str) -> tuple[str | None, str | None]:
        row = self.conn.execute(
            "SELECT cursor, last_synced_at FROM discovery_cursor WHERE source = ?", (source,)
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def set_cursor(self, source: str, cursor: str | None) -> None:
        self.conn.execute(
            """INSERT INTO discovery_cursor (source, cursor, last_synced_at) VALUES (?, ?, ?)
               ON CONFLICT(source) DO UPDATE SET cursor = excluded.cursor,
                                                 last_synced_at = excluded.last_synced_at""",
            (source, cursor, iso(utcnow())),
        )
