import asyncio
from datetime import UTC, datetime

from kue.gooey.discovery import GooeySource, build_prompt

VALID = 'Found one.\n```json\n{"tasks": [{"title": "Fix it", "asana_task_id": "1203"}]}\n```'


def source(fake_gooey, tmp_path):
    return GooeySource(fake_gooey.client(), "ex1", ["gooey-server", "gooey-web-widget"], tmp_path / "replies")


def fetch(src, since=None, dry_run=False):
    return asyncio.run(src.fetch(since, dry_run=dry_run))


def test_build_prompt():
    first = build_prompt(None, ["gooey-server", "gooey-web-widget"])
    later = build_prompt(datetime(2026, 10, 23, 9, 30, tzinfo=UTC), ["gooey-server"])
    assert "last 24 hours" in first
    assert "gooey-server" in first and "gooey-web-widget" in first
    assert "2026-10-23T09:30:00+00:00" in later
    assert "```json" in first


def test_fetch_returns_proposals_and_saves_reply(fake_gooey, tmp_path):
    fake_gooey.started("r1")
    fake_gooey.completed(VALID, "r1")
    [proposal] = fetch(source(fake_gooey, tmp_path))
    assert (proposal.external_id, proposal.title) == ("asana:1203", "Fix it")
    assert (tmp_path / "replies" / "r1.txt").read_text() == VALID
    assert fake_gooey.bodies("start")[0]["messages"] == []


def test_unreadable_reply_is_retried_once_with_history(fake_gooey, tmp_path):
    fake_gooey.started("r1")
    fake_gooey.started("r2")
    fake_gooey.completed("no json here", "r1")
    fake_gooey.completed(VALID, "r2")
    [proposal] = fetch(source(fake_gooey, tmp_path))
    assert proposal.external_id == "asana:1203"
    first, second = fake_gooey.bodies("start")
    assert second["messages"] == [
        {"role": "user", "content": first["input_prompt"]},
        {"role": "assistant", "content": "no json here"},
    ]
    assert "no fenced JSON block" in second["input_prompt"]


def test_unreadable_twice_becomes_one_review_proposal(fake_gooey, tmp_path):
    fake_gooey.started("r1")
    fake_gooey.started("r2")
    fake_gooey.completed("no json here", "r1")
    fake_gooey.completed("still no json " * 800, "r2")
    [proposal] = fetch(source(fake_gooey, tmp_path))
    assert proposal.external_id == "gooey-run:r2"
    assert proposal.title == "Gooey's reply couldn't be read"
    assert "https://gooey.example/r1" in proposal.description and "https://gooey.example/r2" in proposal.description
    assert len(proposal.description) <= 4000
    assert proposal.review_reason
    assert sorted(p.name for p in (tmp_path / "replies").iterdir()) == ["r1.txt", "r2.txt"]


def test_dry_run_saves_nothing(fake_gooey, tmp_path):
    fake_gooey.started("r1")
    fake_gooey.completed(VALID, "r1")
    assert len(fetch(source(fake_gooey, tmp_path), dry_run=True)) == 1
    assert not (tmp_path / "replies").exists()
