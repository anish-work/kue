"""The `kue` command-line interface."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from kue.config.models import AppConfig
from kue.config.settings import (
    ConfigError,
    default_config_path,
    load_config,
    load_secrets,
    template_text,
)
from kue.database.database import connect
from kue.database.repositories import Store, TaskNotFound, utcnow
from kue.environment.doctor import run_checks
from kue.events.logger import EventType, setup_logging
from kue.orchestration.orchestrator import (
    AlreadyRunning,
    CycleReport,
    Orchestrator,
    start_of_local_day,
)
from kue.orchestration.state_machine import (
    NEEDS_ATTENTION,
    TERMINAL,
    WORKER_BUSY,
    InvalidTransition,
)
from kue.tasks.models import Task, TaskProposal, TaskStatus

app = typer.Typer(
    help="Kue: your local autonomous engineer for discovered engineering work.",
    no_args_is_help=True,
    add_completion=False,
)


class _Ctx:
    config_path: Path | None = None


ctx = _Ctx()


@app.callback()
def _main(
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Config file (default ~/.kue/config.yaml)")
    ] = None,
) -> None:
    ctx.config_path = config


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(code)


def _load() -> tuple[AppConfig, Store]:
    try:
        cfg = load_config(ctx.config_path)
    except ConfigError as exc:
        raise _fail(str(exc), 2) from exc
    setup_logging(cfg.logging.dir, cfg.logging.level)
    return cfg, Store(connect(cfg.database.path))


def _local(dt: datetime | None, fmt: str = "%H:%M") -> str:
    return dt.astimezone().strftime(fmt) if dt else "never"


def _task_line(t: Task) -> str:
    repo = (t.repository or "-")[:18]
    title = t.title if len(t.title) <= 50 else t.title[:47] + "..."
    return f"{t.id:>5}  P{t.priority}  {t.status.value:<22} {repo:<18} {title}"


# ------------------------------------------------------------------ setup
@app.command()
def init(force: Annotated[bool, typer.Option(help="Overwrite an existing config")] = False) -> None:
    """Create a config file from the example template."""
    path = (ctx.config_path or default_config_path()).expanduser()
    if path.exists() and not force:
        raise _fail(f"{path} already exists (use --force to overwrite).")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template_text(), encoding="utf-8")
    typer.echo(f"Created {path}")
    typer.echo("Next: set your repository paths in it, then run `kue doctor`.")


@app.command()
def doctor() -> None:
    """Check tools, credentials, database and repositories."""
    config, error = None, None
    try:
        config = load_config(ctx.config_path)
    except ConfigError as exc:
        error = str(exc).splitlines()[0]
    checks = run_checks(config, error, load_secrets())
    for c in checks:
        mark = "OK  " if c.ok else ("FAIL" if c.required else "WARN")
        typer.echo(f"[{mark}] {c.name:<28} {c.detail}")
    if any(c.required and not c.ok for c in checks):
        raise typer.Exit(1)


# ---------------------------------------------------------------- viewing
@app.command()
def status() -> None:
    """Show daemon state and task counts."""
    cfg, store = _load()
    counts = store.count_by_status()
    last = store.get_state("last_cycle_at")
    last_dt = datetime.fromisoformat(last) if last else None
    busy = store.list_tasks(statuses=WORKER_BUSY, limit=1)

    typer.echo("KUE\n")
    typer.echo(f"Status:     {'PAUSED' if store.is_paused() else 'ACTIVE'}")
    typer.echo("Daemon:     not available yet (Phase 3), use `kue run-once`")
    typer.echo(f"Last cycle: {_local(last_dt, '%Y-%m-%d %H:%M')}")
    typer.echo(f"Interval:   every {cfg.scheduler.interval_minutes} min (once the daemon exists)\n")

    def n(*statuses: TaskStatus) -> int:
        return sum(counts.get(s, 0) for s in statuses)

    typer.echo("Tasks:")
    typer.echo(f"  In progress:     {n(*WORKER_BUSY)}")
    typer.echo(f"  Queued:          {n(TaskStatus.QUEUED)}")
    typer.echo(f"  Awaiting review: {n(TaskStatus.AWAITING_REVIEW)}")
    typer.echo(f"  Needs attention: {n(*NEEDS_ATTENTION)}")
    typer.echo(f"  Failed:          {n(TaskStatus.FAILED)}")
    typer.echo(f"  Completed today: {store.count_completed_since(start_of_local_day(utcnow()))}")
    if busy:
        t = busy[0]
        typer.echo(f"\nCurrent:\n  #{t.id} {t.title}\n  Repository: {t.repository}\n  Stage: {t.status}")


@app.command()
def tasks(
    status_filter: Annotated[str | None, typer.Option("--status", help="e.g. QUEUED")] = None,
    show_all: Annotated[bool, typer.Option("--all", help="Include completed/cancelled")] = False,
) -> None:
    """List tasks in priority order."""
    _, store = _load()
    if status_filter:
        try:
            statuses = [TaskStatus(status_filter.upper())]
        except ValueError as exc:
            raise _fail(f"Unknown status {status_filter!r}.") from exc
    elif show_all:
        statuses = list(TaskStatus)
    else:
        statuses = [s for s in TaskStatus if s not in TERMINAL]
    items = store.list_tasks(statuses=statuses)
    if not items:
        typer.echo("No tasks.")
        return
    typer.echo(f"{'ID':>5}  PRI {'STATUS':<22} {'REPO':<18} TITLE")
    for t in items:
        typer.echo(_task_line(t))


@app.command()
def task(task_id: int) -> None:
    """Show one task and its recent history."""
    _, store = _load()
    t = store.get_task(task_id)
    if t is None:
        raise _fail(f"Task #{task_id} does not exist.")
    typer.echo(f"#{t.id} {t.title}")
    for label, value in [
        ("Status", t.status.value),
        ("Priority", f"P{t.priority}"),
        ("Repository", t.repository),
        ("Source", f"{t.source} ({t.external_id})"),
        ("Attempts", t.attempt_count),
        ("Branch", t.branch_name),
        ("PR", t.pr_number),
        ("Asana", t.asana_task_id),
        ("Reason", t.failure_reason),
        ("Created", _local(t.created_at, "%Y-%m-%d %H:%M")),
    ]:
        if value not in (None, ""):
            typer.echo(f"  {label + ':':<12}{value}")
    if t.description:
        typer.echo(f"\n{t.description}")
    typer.echo("\nHistory (newest first):")
    for e in store.list_events(task_id=task_id, limit=10):
        typer.echo(f"  {_local(e.timestamp, '%m-%d %H:%M')}  {e.event_type}  {json.dumps(e.payload)}")


@app.command()
def logs(
    task_id: Annotated[int | None, typer.Option("--task")] = None,
    today: Annotated[bool, typer.Option("--today")] = False,
    limit: Annotated[int, typer.Option("--limit")] = 30,
) -> None:
    """Show the event history."""
    _, store = _load()
    since = start_of_local_day(utcnow()) if today else None
    events = store.list_events(task_id=task_id, since=since, limit=limit)
    if not events:
        typer.echo("No events.")
    for e in reversed(events):
        who = f"#{e.task_id}" if e.task_id else "-"
        typer.echo(
            f"{_local(e.timestamp, '%Y-%m-%d %H:%M:%S')}  {e.event_type:<22} {who:>5}  "
            f"{json.dumps(e.payload)}"
        )


@app.command()
def runs(limit: Annotated[int, typer.Option("--limit")] = 20) -> None:
    """Show coding-agent runs (populated from Phase 4)."""
    _, store = _load()
    items = store.list_runs(limit)
    if not items:
        typer.echo("No runs yet.")
    for r in items:
        typer.echo(
            f"{r.id:>5}  task #{r.task_id:<5} {r.status:<10} {r.agent:<12} "
            f"{_local(r.started_at, '%m-%d %H:%M')}  exit={r.exit_code}"
        )


# ---------------------------------------------------------------- actions
@app.command()
def add(
    title: str,
    repo: Annotated[str | None, typer.Option("--repo", help="Repository name from config")] = None,
    priority: Annotated[int, typer.Option("--priority", min=0, max=5)] = 4,
    description: Annotated[str, typer.Option("--description")] = "",
) -> None:
    """Add a manual task (useful before Gooey discovery exists)."""
    _, store = _load()
    proposal = TaskProposal(
        source="manual",
        external_id=f"manual-{uuid.uuid4().hex[:12]}",
        title=title,
        description=description,
        repository=repo,
        priority=priority,
    )
    t, _ = store.upsert_proposal(proposal)
    typer.echo(f"Created task #{t.id} ({t.status}). It will be triaged on the next cycle.")


@app.command("run-once")
def run_once(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would happen; change nothing")] = False,
    task_id: Annotated[int | None, typer.Option("--task", help="Only consider this task")] = None,
) -> None:
    """Run a single orchestration cycle."""
    cfg, store = _load()
    orchestrator = Orchestrator(cfg, store, sources=[])
    try:
        report = asyncio.run(orchestrator.run_cycle(dry_run=dry_run, only_task_id=task_id))
    except AlreadyRunning as exc:
        raise _fail(str(exc)) from exc
    _print_report(report)


def _print_report(r: CycleReport) -> None:
    typer.echo("RUN-ONCE (dry run: nothing was changed)" if r.dry_run else "RUN-ONCE")
    typer.echo(f"\nDiscovery: {r.new_tasks} new, {r.seen_again} already known")
    for line in r.proposal_previews:
        typer.echo(f"  {line}")
    for name, err in r.source_errors.items():
        typer.secho(f"  {name} failed: {err}", fg=typer.colors.RED)
    if r.recovered:
        typer.echo(f"\nRecovered interrupted tasks (now need review): {r.recovered}")
    if r.triaged:
        typer.echo("\nTriage:" if not r.dry_run else "\nTriage (would apply):")
        for o in r.triaged:
            suffix = f": {o.reason}" if o.reason else ""
            typer.echo(f"  #{o.task_id} {o.title} -> {o.status}{suffix}")
    if r.selected:
        t = r.selected
        typer.echo(f"\nNext task: #{t.id} [P{t.priority}] {t.title} ({t.repository})")
        if r.repo_state and r.repo_state.exists:
            s = r.repo_state
            typer.echo(f"  Repo: {s.path} on {s.branch or 'detached HEAD'}, "
                       f"{'dirty' if s.dirty else 'clean'}")
        if r.planned_branch:
            verb = "Would create" if r.dry_run else "Planned"
            typer.echo(f"  {verb} branch: {r.planned_branch}")
    if r.skip_reason:
        typer.secho(f"\nNot starting: {r.skip_reason}", fg=typer.colors.YELLOW)
    for note in r.notes:
        typer.echo(f"\nNote: {note}")


@app.command()
def pause() -> None:
    """Emergency stop: no new tasks start until `kue resume`."""
    _, store = _load()
    store.set_state("paused", "1")
    store.add_event(EventType.DAEMON_PAUSED)
    typer.echo("Paused. No new tasks will start.")


@app.command()
def resume() -> None:
    """Allow new tasks to start again."""
    _, store = _load()
    store.set_state("paused", "0")
    store.add_event(EventType.DAEMON_RESUMED)
    typer.echo("Resumed.")


def _move(task_id: int, allowed_from: set[TaskStatus], target: TaskStatus, verb: str, **fields) -> None:
    _, store = _load()
    t = store.get_task(task_id)
    if t is None:
        raise _fail(f"Task #{task_id} does not exist.")
    if t.status not in allowed_from:
        names = ", ".join(sorted(s.value for s in allowed_from))
        raise _fail(f"Task #{task_id} is {t.status}; can only {verb} tasks in: {names}.")
    try:
        store.transition(task_id, target, reason=f"{verb} via CLI", **fields)
    except (InvalidTransition, TaskNotFound) as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"Task #{task_id}: {t.status} -> {target}")


@app.command()
def retry(task_id: int) -> None:
    """Re-queue a failed or blocked task."""
    _move(task_id, {TaskStatus.FAILED, TaskStatus.BLOCKED}, TaskStatus.QUEUED, "retry",
          failure_reason=None)


@app.command()
def approve(task_id: int) -> None:
    """Approve a task that was waiting for human review."""
    _move(task_id, {TaskStatus.REQUIRES_HUMAN_REVIEW}, TaskStatus.QUEUED, "approve",
          failure_reason=None)


@app.command()
def cancel(task_id: int) -> None:
    """Cancel a task that is not currently being worked on."""
    allowed = {s for s in TaskStatus if s not in TERMINAL and s not in WORKER_BUSY}
    _move(task_id, allowed, TaskStatus.CANCELLED, "cancel")
