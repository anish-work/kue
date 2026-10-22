"""One orchestration cycle: recover -> discover -> triage -> pick -> preflight.

Phase 1 stops after preflight: there is no coding worker yet, so the selected
task stays QUEUED and an EXECUTION_DEFERRED event records what would happen.
"""
from __future__ import annotations

import fcntl
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from kue.coding.workspace import RepoState, branch_name_for, inspect_repository
from kue.config.models import AppConfig
from kue.database.repositories import Store, iso, utcnow
from kue.events.logger import EventType, get_logger
from kue.orchestration.sources import DiscoverySource
from kue.orchestration.state_machine import WORKER_BUSY
from kue.tasks.models import Task, TaskStatus

log = get_logger("orchestrator")


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def cycle_lock(lock_path: Path) -> Iterator[None]:
    """Process-level lock so two cycles never touch the same repo at once."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning("Another cycle is already running.") from exc
        yield


@dataclass
class TriageOutcome:
    task_id: int
    title: str
    status: TaskStatus
    reason: str | None = None


@dataclass
class CycleReport:
    dry_run: bool
    started_at: datetime
    finished_at: datetime | None = None
    new_tasks: int = 0
    seen_again: int = 0
    proposal_previews: list[str] = field(default_factory=list)
    source_errors: dict[str, str] = field(default_factory=dict)
    recovered: list[int] = field(default_factory=list)
    triaged: list[TriageOutcome] = field(default_factory=list)
    paused: bool = False
    selected: Task | None = None
    repo_state: RepoState | None = None
    planned_branch: str | None = None
    skip_reason: str | None = None
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "new_tasks": self.new_tasks,
            "seen_again": self.seen_again,
            "source_errors": list(self.source_errors),
            "recovered": self.recovered,
            "triaged": len(self.triaged),
            "selected_task": self.selected.id if self.selected else None,
            "skip_reason": self.skip_reason,
        }


def triage_decision(config: AppConfig, repository: str | None) -> tuple[TaskStatus, str | None]:
    if not repository:
        return TaskStatus.REQUIRES_HUMAN_REVIEW, "No repository identified for this task."
    repo = config.repositories.get(repository)
    if repo is None:
        return TaskStatus.REQUIRES_HUMAN_REVIEW, f"Repository '{repository}' is not in the config."
    if not repo.enabled:
        return TaskStatus.REQUIRES_HUMAN_REVIEW, f"Repository '{repository}' is disabled."
    return TaskStatus.QUEUED, None


def select_next(candidates: Iterable[Task]) -> Task | None:
    """Deterministic: lowest priority number, then oldest, then lowest id."""
    ordered = sorted(candidates, key=lambda t: (t.priority, t.created_at, t.id))
    return ordered[0] if ordered else None


def start_of_local_day(now: datetime) -> datetime:
    return now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)


class Orchestrator:
    def __init__(
        self,
        config: AppConfig,
        store: Store,
        sources: list[DiscoverySource] | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.config = config
        self.store = store
        self.sources = sources or []
        self.clock = clock

    async def run_cycle(self, *, dry_run: bool = False, only_task_id: int | None = None) -> CycleReport:
        if dry_run:  # read-only: no lock, no writes
            return await self._cycle(dry_run=True, only_task_id=only_task_id)
        with cycle_lock(self.config.lock_path):
            return await self._cycle(dry_run=False, only_task_id=only_task_id)

    async def _cycle(self, *, dry_run: bool, only_task_id: int | None) -> CycleReport:
        write = not dry_run
        report = CycleReport(dry_run=dry_run, started_at=self.clock())
        log.info("cycle started", extra={"data": {"dry_run": dry_run, "only_task": only_task_id}})
        if write:
            self.store.add_event(EventType.CYCLE_STARTED, payload={"only_task": only_task_id})
            report.recovered = self._recover_interrupted()
        await self._discover(report, write)
        self._triage(report, write)
        self._plan_execution(report, write, only_task_id)
        report.finished_at = self.clock()
        if write:
            self.store.set_state("last_cycle_at", iso(report.finished_at))
            self.store.add_event(EventType.CYCLE_FINISHED, payload=report.summary())
        log.info("cycle finished", extra={"data": report.summary()})
        return report

    # ------------------------------------------------------------------ steps
    def _recover_interrupted(self) -> list[int]:
        """A task left mid-work means we crashed/restarted. Never resume blindly."""
        recovered = []
        for task in self.store.list_tasks(statuses=WORKER_BUSY):
            self.store.transition(
                task.id,
                TaskStatus.REQUIRES_HUMAN_REVIEW,
                reason=(
                    f"Interrupted while {task.status}. Check the repository "
                    f"(branch {task.branch_name or 'unknown'}) before approving."
                ),
            )
            recovered.append(task.id)
        return recovered

    async def _discover(self, report: CycleReport, write: bool) -> None:
        if not self.sources:
            report.notes.append("No discovery sources configured yet (Gooey arrives in Phase 2).")
        for source in self.sources:
            try:
                proposals = await source.fetch()
            except Exception as exc:  # recorded, never silent
                report.source_errors[source.name] = str(exc)
                log.exception("source sync failed", extra={"data": {"source": source.name}})
                if write:
                    self.store.add_event(
                        EventType.SOURCE_SYNC_FAILED,
                        payload={"source": source.name, "error": str(exc)},
                    )
                continue
            for proposal in proposals:
                if write:
                    _, created = self.store.upsert_proposal(proposal)
                else:
                    created = self.store.find_by_external(proposal.source, proposal.external_id) is None
                    label = "new" if created else "already known"
                    report.proposal_previews.append(f"[{proposal.source}] {proposal.title} ({label})")
                if created:
                    report.new_tasks += 1
                else:
                    report.seen_again += 1
            if write:
                self.store.set_cursor(source.name, None)

    def _triage(self, report: CycleReport, write: bool) -> None:
        pending = self.store.list_tasks(statuses=[TaskStatus.DISCOVERED, TaskStatus.TRIAGED])
        for task in pending:
            status, reason = triage_decision(self.config, task.repository)
            report.triaged.append(TriageOutcome(task.id, task.title, status, reason))
            if not write:
                continue
            if status is TaskStatus.QUEUED:
                if task.status is TaskStatus.DISCOVERED:
                    self.store.transition(task.id, TaskStatus.TRIAGED)
                self.store.transition(task.id, TaskStatus.QUEUED)
            else:
                self.store.transition(task.id, status, reason=reason)

    def _plan_execution(self, report: CycleReport, write: bool, only_task_id: int | None) -> None:
        store = self.store
        busy = store.list_tasks(statuses=WORKER_BUSY)
        if busy:
            report.skip_reason = f"Task #{busy[0].id} is still in progress."
            return
        if store.is_paused():
            report.paused = True
            report.skip_reason = "Paused. No new tasks will start (run `kue resume`)."
            return
        runs_today = store.count_runs_since(start_of_local_day(self.clock()))
        limit = self.config.limits.max_runs_per_day
        if runs_today >= limit:
            report.skip_reason = f"Daily run limit reached ({runs_today}/{limit})."
            return

        candidates = store.list_tasks(statuses=[TaskStatus.QUEUED])
        if not write:  # include tasks this cycle would have queued
            would_queue = {o.task_id for o in report.triaged if o.status is TaskStatus.QUEUED}
            candidates += [t for t in map(store.get_task, would_queue) if t]
        if only_task_id is not None:
            candidates = [t for t in candidates if t.id == only_task_id]
            if not candidates:
                report.skip_reason = f"Task #{only_task_id} is not queued."
                return

        task = select_next(candidates)
        if task is None:
            report.skip_reason = "Nothing queued."
            return
        report.selected = task

        repo_cfg = self.config.resolve_repository(task.repository)
        if repo_cfg is None:
            reason = f"Repository '{task.repository}' is missing or disabled in the config."
            report.skip_reason = reason
            if write:
                store.transition(task.id, TaskStatus.REQUIRES_HUMAN_REVIEW, reason=reason)
            return

        state = inspect_repository(repo_cfg.path)
        report.repo_state = state
        if state.problem:
            report.skip_reason = state.problem
            if write:
                if state.dirty and self.config.workspace.dirty_tree_action == "skip":
                    store.add_event(
                        EventType.WORKSPACE_NOT_READY,
                        task.id,
                        {"problem": state.problem, "dirty_files": state.dirty_files[:20]},
                    )
                else:
                    store.transition(task.id, TaskStatus.REQUIRES_HUMAN_REVIEW, reason=state.problem)
            return

        report.planned_branch = branch_name_for(task.id, task.title)
        report.notes.append("Coding worker arrives in Phase 4, so the task stays queued.")
        if write:
            store.add_event(
                EventType.EXECUTION_DEFERRED,
                task.id,
                {
                    "planned_branch": report.planned_branch,
                    "base_branch": state.branch,
                    "reason": "No coding worker until Phase 4.",
                },
            )
