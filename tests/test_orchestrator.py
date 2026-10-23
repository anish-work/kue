import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from kue.config.models import GooeyConfig
from kue.config.settings import ConfigError, Secrets
from kue.events.logger import EventType
from kue.orchestration.sources import configured_sources
from kue.orchestration.orchestrator import (
    AlreadyRunning,
    Orchestrator,
    cycle_lock,
    select_next,
)
from kue.tasks.models import Task, TaskProposal, TaskStatus as S


class FakeSource:
    def __init__(self, name="fake", proposals=None, error=None):
        self.name = name
        self.proposals = proposals or []
        self.error = error
        self.calls = []

    async def fetch(self, since, *, dry_run):
        self.calls.append((since, dry_run))
        if self.error:
            raise self.error
        return list(self.proposals)


def run(orch, **kw):
    return asyncio.run(orch.run_cycle(**kw))


def add(store, ext, repo="gooey-server", priority=4, title=None):
    task, _ = store.upsert_proposal(
        TaskProposal(source="manual", external_id=ext, title=title or ext, repository=repo, priority=priority)
    )
    return task


def events_of(store, event_type):
    return [e for e in store.list_events(limit=500) if e.event_type == event_type]


def test_empty_cycle_records_start_and_finish(config, store):
    report = run(Orchestrator(config, store))
    assert report.skip_reason == "Nothing queued."
    assert events_of(store, EventType.CYCLE_STARTED) and events_of(store, EventType.CYCLE_FINISHED)
    assert store.get_state("last_cycle_at")


def test_task_is_triaged_selected_and_deferred(config, store):
    task = add(store, "t1", title="Fix widget")
    report = run(Orchestrator(config, store))
    assert report.selected.id == task.id
    assert report.planned_branch == f"kue/KUE-{task.id}-fix-widget"
    assert store.get_task(task.id).status is S.QUEUED
    deferred = events_of(store, EventType.EXECUTION_DEFERRED)
    assert deferred[0].payload["planned_branch"] == report.planned_branch
    assert deferred[0].payload["base_branch"] == "main"


def test_dry_run_writes_nothing(config, store):
    add(store, "t1")
    before_events = store.count_events()
    report = run(Orchestrator(config, store, sources=[FakeSource(proposals=[
        TaskProposal(source="discord", external_id="m1", title="new thing", repository="gooey-server")
    ])]), dry_run=True)
    assert store.count_events() == before_events
    assert [t.status for t in store.list_tasks()] == [S.DISCOVERED]
    assert report.selected is not None and report.planned_branch
    assert report.proposal_previews == ["[discord] new thing (new)"]
    assert store.get_state("last_cycle_at") is None


@pytest.mark.parametrize("repo", [None, "unknown-repo", "old-repo"])
def test_unresolvable_repository_needs_review(config, store, repo):
    task = add(store, "t1", repo=repo)
    run(Orchestrator(config, store))
    updated = store.get_task(task.id)
    assert updated.status is S.REQUIRES_HUMAN_REVIEW
    assert updated.failure_reason


def test_priority_beats_age(config, store):
    add(store, "old-normal", priority=4)
    urgent = add(store, "new-urgent", priority=1)
    assert run(Orchestrator(config, store)).selected.id == urgent.id


def test_select_next_tiebreak_is_oldest():
    now = datetime.now(UTC)

    def t(i, created):
        return Task(id=i, external_id=str(i), source="m", title="x", priority=2, status=S.QUEUED,
                    created_at=created, updated_at=created, last_seen_at=created)

    assert select_next([t(2, now), t(1, now - timedelta(hours=1))]).id == 1
    assert select_next([]) is None


def test_dirty_repo_skips_and_keeps_task_queued(config, store, git_repo):
    task = add(store, "t1")
    (git_repo / "wip.py").write_text("x = 1\n")
    report = run(Orchestrator(config, store))
    assert "uncommitted" in report.skip_reason
    assert store.get_task(task.id).status is S.QUEUED
    assert events_of(store, EventType.WORKSPACE_NOT_READY)[0].payload["dirty_files"] == ["wip.py"]
    assert not events_of(store, EventType.EXECUTION_DEFERRED)


def test_dirty_repo_can_require_review(config, store, git_repo):
    config.workspace.dirty_tree_action = "require_review"
    task = add(store, "t1")
    (git_repo / "wip.py").write_text("x = 1\n")
    run(Orchestrator(config, store))
    assert store.get_task(task.id).status is S.REQUIRES_HUMAN_REVIEW


def test_missing_repo_path_requires_review(config, store, git_repo, tmp_path):
    config.repositories["gooey-server"].path = tmp_path / "moved"
    task = add(store, "t1")
    run(Orchestrator(config, store))
    assert store.get_task(task.id).status is S.REQUIRES_HUMAN_REVIEW


def test_pause_blocks_new_work(config, store):
    add(store, "t1")
    store.set_state("paused", "1")
    report = run(Orchestrator(config, store))
    assert report.paused and report.selected is None
    assert not events_of(store, EventType.EXECUTION_DEFERRED)


def test_interrupted_task_is_recovered_for_review(config, store):
    task = add(store, "t1")
    for s in (S.TRIAGED, S.QUEUED, S.RUNNING, S.IMPLEMENTING):
        store.transition(task.id, s)
    report = run(Orchestrator(config, store))
    assert report.recovered == [task.id]
    assert store.get_task(task.id).status is S.REQUIRES_HUMAN_REVIEW


def test_busy_task_blocks_selection_in_dry_run(config, store):
    busy = add(store, "busy")
    for s in (S.TRIAGED, S.QUEUED, S.RUNNING):
        store.transition(busy.id, s)
    add(store, "waiting")
    report = run(Orchestrator(config, store), dry_run=True)
    assert "still in progress" in report.skip_reason


def test_daily_run_limit(config, store):
    config.limits.max_runs_per_day = 1
    task = add(store, "t1")
    store.start_run(task.id, agent="claude-code")
    report = run(Orchestrator(config, store))
    assert "Daily run limit" in report.skip_reason


def test_only_task_filter(config, store):
    add(store, "a", priority=1)
    b = add(store, "b", priority=4)
    assert run(Orchestrator(config, store), only_task_id=b.id).selected.id == b.id
    assert "not queued" in run(Orchestrator(config, store), only_task_id=999).skip_reason


def test_discovery_deduplicates_across_cycles(config, store):
    source = FakeSource(proposals=[
        TaskProposal(source="discord", external_id="m1", title="iOS error", repository="gooey-server", priority=1)
    ])
    orch = Orchestrator(config, store, sources=[source])
    first = run(orch)
    second = run(orch)
    assert (first.new_tasks, second.new_tasks, second.seen_again) == (1, 0, 1)
    assert len(store.list_tasks()) == 1
    assert store.get_cursor("fake")[1] is not None


def test_failing_source_is_recorded_and_cycle_continues(config, store):
    add(store, "t1")
    good = FakeSource(name="good", proposals=[
        TaskProposal(source="asana", external_id="a1", title="task", repository="gooey-server")
    ])
    bad = FakeSource(name="bad", error=RuntimeError("Gooey unavailable"))
    report = run(Orchestrator(config, store, sources=[bad, good]))
    assert report.source_errors == {"bad": "Gooey unavailable"}
    assert report.new_tasks == 1 and report.selected is not None
    assert events_of(store, EventType.SOURCE_SYNC_FAILED)[0].payload["source"] == "bad"


NOON = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def test_cursor_is_passed_and_advanced_only_after_success(config, store):
    source = FakeSource()
    run(Orchestrator(config, store, sources=[source], clock=lambda: NOON))
    run(Orchestrator(config, store, sources=[source], clock=lambda: NOON + timedelta(hours=1)))
    assert [since for since, _ in source.calls] == [None, NOON]
    assert datetime.fromisoformat(store.get_cursor("fake")[0]) == NOON + timedelta(hours=1)

    bad = FakeSource(name="bad", error=RuntimeError("Gooey unavailable"))
    run(Orchestrator(config, store, sources=[bad], clock=lambda: NOON))
    assert store.get_cursor("bad") == (None, None)


def test_dry_run_passes_flag_and_keeps_cursor(config, store):
    source = FakeSource()
    run(Orchestrator(config, store, sources=[source]), dry_run=True)
    assert source.calls == [(None, True)]
    assert store.get_cursor("fake") == (None, None)


def test_review_proposal_lands_in_review(config, store):
    reason = "Gooey's reply couldn't be read"
    source = FakeSource(proposals=[TaskProposal(source="gooey", external_id="gooey-run:r2",
                                                title=reason, review_reason=reason)])
    orch = Orchestrator(config, store, sources=[source])
    run(orch)
    run(orch)
    [task] = store.list_tasks()
    assert (task.status, task.failure_reason) == (S.REQUIRES_HUMAN_REVIEW, reason)
    assert len(events_of(store, EventType.TASK_STATUS_CHANGED)) == 1


def test_dry_run_preview_shows_review_reason(config, store):
    source = FakeSource(proposals=[TaskProposal(source="gooey", external_id="gooey-run:r2",
                                                title="Unreadable", review_reason="no ids")])
    report = run(Orchestrator(config, store, sources=[source]), dry_run=True)
    assert "needs review: no ids" in report.proposal_previews[0]


def test_corrupt_cursor_is_ignored(config, store):
    store.set_cursor("fake", "not-a-date")
    source = FakeSource()
    report = run(Orchestrator(config, store, sources=[source]))
    assert source.calls == [(None, False)] and report.finished_at is not None


def test_configured_sources(config):
    assert configured_sources(config, Secrets()) == []
    with_agent = config.model_copy(update={"gooey": GooeyConfig(agent_example_id="ex1")})
    with pytest.raises(ConfigError, match="GOOEY_API_KEY"):
        configured_sources(with_agent, Secrets())
    secrets = Secrets(gooey_api_url="https://api.example.test", gooey_api_key=SecretStr("k"))
    [source] = configured_sources(with_agent, secrets)
    assert source.name == "gooey"


def test_concurrent_cycle_is_refused(config, store):
    with cycle_lock(config.lock_path):
        with pytest.raises(AlreadyRunning):
            run(Orchestrator(config, store))
    run(Orchestrator(config, store))  # lock released afterwards
