"""Core domain models shared across the orchestrator."""
from __future__ import annotations

from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    TRIAGED = "TRIAGED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    IMPLEMENTING = "IMPLEMENTING"
    TESTING = "TESTING"
    COMMITTING = "COMMITTING"
    PR_CREATED = "PR_CREATED"
    VALIDATING = "VALIDATING"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    READY_TO_MERGE = "READY_TO_MERGE"
    MERGING = "MERGING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    REQUIRES_HUMAN_REVIEW = "REQUIRES_HUMAN_REVIEW"
    CANCELLED = "CANCELLED"


class Priority(IntEnum):
    P0 = 0  # production outage
    P1 = 1  # production bug
    P2 = 2  # explicitly assigned work
    P3 = 3  # due-soon task
    P4 = 4  # normal task
    P5 = 5  # nice-to-have


class Task(BaseModel):
    id: int
    external_id: str
    source: str
    title: str
    description: str = ""
    repository: str | None = None
    priority: int = Priority.P4
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    last_seen_at: datetime
    attempt_count: int = 0
    current_run_id: int | None = None
    branch_name: str | None = None
    original_branch: str | None = None
    pr_number: int | None = None
    asana_task_id: str | None = None
    discord_message_id: str | None = None
    autonomy_policy: str | None = None
    failure_reason: str | None = None


class TaskProposal(BaseModel):
    """What a discovery source hands to the orchestrator (filled from Gooey in Phase 2)."""

    source: str
    external_id: str
    title: str
    description: str = ""
    repository: str | None = None
    priority: int = Field(default=Priority.P4, ge=0, le=5)
    asana_task_id: str | None = None
    discord_message_id: str | None = None


class Run(BaseModel):
    id: int
    task_id: int
    started_at: datetime
    finished_at: datetime | None = None
    status: str
    agent: str
    command: str | None = None
    exit_code: int | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    summary: str | None = None


class Event(BaseModel):
    id: int
    task_id: int | None
    timestamp: datetime
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
