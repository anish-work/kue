import pytest

from kue.database.database import SCHEMA_VERSION, connect, schema_version
from kue.database.repositories import Store, TaskNotFound
from kue.events.logger import EventType
from kue.orchestration.state_machine import InvalidTransition
from kue.tasks.models import TaskProposal, TaskStatus


def proposal(ext="msg-1", source="discord", **kw):
    return TaskProposal(source=source, external_id=ext, title=kw.pop("title", "Fix bug"), **kw)


def test_schema_version(store):
    assert schema_version(store.conn) == SCHEMA_VERSION


def test_migrate_is_idempotent(config):
    connect(config.database.path).close()
    conn = connect(config.database.path)
    assert schema_version(conn) == SCHEMA_VERSION


def test_upsert_deduplicates_same_source_and_id(store):
    first, created1 = store.upsert_proposal(proposal())
    second, created2 = store.upsert_proposal(proposal(title="Different wording"))
    assert created1 and not created2
    assert first.id == second.id
    assert second.title == "Fix bug"
    assert second.last_seen_at >= first.last_seen_at
    assert len(store.list_tasks()) == 1
    created_events = [e for e in store.list_events() if e.event_type == EventType.TASK_CREATED]
    assert len(created_events) == 1


def test_upsert_matches_on_either_id_within_source(store):
    first, _ = store.upsert_proposal(proposal("discord:99", source="gooey", discord_message_id="99"))
    again, created = store.upsert_proposal(
        proposal("asana:7", source="gooey", asana_task_id="7", discord_message_id="99")
    )
    assert not created and again.id == first.id
    assert again.external_id == "discord:99" and again.asana_task_id == "7"  # the link is learned
    assert len(store.list_tasks()) == 1
    _, other_source = store.upsert_proposal(proposal("asana:7", source="manual", asana_task_id="7", discord_message_id="99"))
    assert other_source


def test_upsert_keeps_different_asana_tasks_apart(store):
    store.upsert_proposal(proposal("asana:111", source="gooey", asana_task_id="111", discord_message_id="99"))
    _, created = store.upsert_proposal(proposal("asana:222", source="gooey", asana_task_id="222", discord_message_id="99"))
    assert created
    assert sorted(t.asana_task_id for t in store.list_tasks()) == ["111", "222"]


def test_discord_task_links_to_one_asana_task_only(store):
    store.upsert_proposal(proposal("discord:99", source="gooey", discord_message_id="99"))
    _, linked = store.upsert_proposal(proposal("asana:1", source="gooey", asana_task_id="1", discord_message_id="99"))
    _, second = store.upsert_proposal(proposal("asana:2", source="gooey", asana_task_id="2", discord_message_id="99"))
    assert (linked, second) == (False, True)
    assert [e.payload for e in store.list_events() if e.event_type == EventType.TASK_LINKED] == [
        {"external_id": "asana:1", "asana_task_id": "1", "discord_message_id": None}
    ]


def test_same_external_id_different_source_is_separate(store):
    store.upsert_proposal(proposal(source="discord"))
    store.upsert_proposal(proposal(source="asana"))
    assert len(store.list_tasks()) == 2


def test_transition_records_event(store):
    task, _ = store.upsert_proposal(proposal())
    updated = store.transition(task.id, TaskStatus.REQUIRES_HUMAN_REVIEW, reason="unclear")
    assert updated.status is TaskStatus.REQUIRES_HUMAN_REVIEW
    assert updated.failure_reason == "unclear"
    event = store.list_events(task_id=task.id, limit=1)[0]
    assert event.event_type == EventType.TASK_STATUS_CHANGED
    assert event.payload == {"from": "DISCOVERED", "to": "REQUIRES_HUMAN_REVIEW", "reason": "unclear"}


def test_invalid_transition_changes_nothing(store):
    task, _ = store.upsert_proposal(proposal())
    before = store.count_events()
    with pytest.raises(InvalidTransition):
        store.transition(task.id, TaskStatus.COMPLETED)
    assert store.get_task(task.id).status is TaskStatus.DISCOVERED
    assert store.count_events() == before


def test_transition_missing_task(store):
    with pytest.raises(TaskNotFound):
        store.transition(999, TaskStatus.QUEUED)


def test_update_rejects_unknown_fields(store):
    task, _ = store.upsert_proposal(proposal())
    with pytest.raises(ValueError):
        store.update_task(task.id, status="COMPLETED")  # status only via transition
    with pytest.raises(ValueError):
        store.update_task(task.id, **{"title = 'x'; --": "y"})


def test_list_tasks_priority_order(store):
    store.upsert_proposal(proposal("a", priority=4, title="normal"))
    store.upsert_proposal(proposal("b", priority=1, title="prod bug"))
    assert [t.title for t in store.list_tasks()] == ["prod bug", "normal"]


def test_state_survives_restart(config, store):
    task, _ = store.upsert_proposal(proposal())
    store.set_state("paused", "1")
    store.conn.close()
    reopened = Store(connect(config.database.path))
    assert reopened.get_task(task.id).title == "Fix bug"
    assert reopened.is_paused()


def test_event_payload_secrets_are_redacted(store):
    store.add_event("TEST", payload={"api_key": "sk-123", "nested": {"github_token": "x"}, "ok": 1})
    payload = store.list_events(limit=1)[0].payload
    assert payload == {"api_key": "***", "nested": {"github_token": "***"}, "ok": 1}


def test_runs_and_cursor(store):
    task, _ = store.upsert_proposal(proposal())
    run = store.start_run(task.id, agent="claude-code", command="claude -p ...")
    assert run.status == "RUNNING"
    assert store.list_runs()[0].id == run.id
    store.set_cursor("discord", "abc")
    cursor, synced = store.get_cursor("discord")
    assert cursor == "abc" and synced
