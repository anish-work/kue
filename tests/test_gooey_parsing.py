import pytest

from kue.gooey.discovery import ReplyFormatError, build_prompt, extract_tasks, proposals_from_items

FENCE = "```"


@pytest.mark.parametrize("reply, expected", [
    (f'Here you go.\n{FENCE}json\n{{"tasks": [{{"title": "a"}}]}}\n{FENCE}\nThanks!', [{"title": "a"}]),
    (f'{FENCE}\n{{"tasks": []}}\n{FENCE}', []),
    (f'{FENCE}json\n{{"tasks": [{{"title": "old"}}]}}\n{FENCE}\nfixed:\n{FENCE}json\n{{"tasks": [{{"title": "new"}}]}}\n{FENCE}',
     [{"title": "new"}]),
    # backticks inside a JSON string (a pasted traceback) don't end the block
    (f'{FENCE}json\n{{"tasks": [{{"title": "a", "description": "{FENCE}\\nTraceback\\n{FENCE}"}}]}}\n{FENCE}',
     [{"title": "a", "description": f"{FENCE}\nTraceback\n{FENCE}"}]),
    # an inline fence in the prose before the block doesn't start one
    (f'I looked at {FENCE}routers/api.py{FENCE} first.\n{FENCE}json\n{{"tasks": []}}\n{FENCE}', []),
])
def test_extract_tasks(reply, expected):
    assert extract_tasks(reply) == expected


@pytest.mark.parametrize("reply, message", [
    ('{"tasks": []}', "no fenced JSON block"),
    (f'{FENCE}json\n{{"tasks": [\n{FENCE}', "isn't valid JSON"),
    (f"{FENCE}json\n[1, 2]\n{FENCE}", 'object with a "tasks" list'),
    (f'{FENCE}json\n{{"tasks": "x"}}\n{FENCE}', 'object with a "tasks" list'),
])
def test_extract_tasks_rejects_bad_replies(reply, message):
    with pytest.raises(ReplyFormatError, match=message):
        extract_tasks(reply)


def one(item):
    [proposal] = proposals_from_items([item], run_id="r1")
    return proposal


def test_full_item_becomes_proposal_keyed_on_asana():
    p = one({"title": "Fix it", "asana_task_id": 1203, "discord_message_id": "99",
             "repository": "gooey-server", "priority": 1, "extra": "ignored"})
    assert (p.source, p.external_id, p.asana_task_id, p.discord_message_id) == ("gooey", "asana:1203", "1203", "99")
    assert (p.title, p.repository, p.priority, p.review_reason) == ("Fix it", "gooey-server", 1, None)


def test_discord_only_item_is_keyed_on_discord():
    assert one({"title": "Crash on iOS", "discord_message_id": "99"}).external_id == "discord:99"


def test_item_without_ids_goes_to_review():
    p = one({"title": "Vague idea"})
    assert p.external_id == "gooey-run:r1:0"
    assert "Asana or Discord" in p.review_reason


@pytest.mark.parametrize("priority", [9, "high", None])
def test_bad_priority_falls_back_to_p4(priority):
    p = one({"title": "x", "priority": priority, "asana_task_id": "1"})
    assert (p.priority, p.review_reason) == (4, None)


def test_invalid_item_with_valid_id_is_keyed_on_the_id():
    p = one({"title": "", "asana_task_id": "1"})
    assert p.external_id == "asana:1" and p.review_reason
    assert p.asana_task_id == "1"


def test_items_without_usable_ids_are_keyed_by_position():
    bad_id, text = proposals_from_items([{"asana_task_id": "abc", "title": "x"}, "just text"], run_id="r1")
    assert (bad_id.external_id, text.external_id) == ("gooey-run:r1:0", "gooey-run:r1:1")
    assert bad_id.review_reason and text.review_reason
    assert text.title == "Unreadable task from Gooey"


def test_invalid_item_keeps_its_repository_and_priority():
    p = one({"title": "", "asana_task_id": "1", "repository": "gooey-server", "priority": 2})
    assert p.review_reason and (p.repository, p.priority) == ("gooey-server", 2)


def test_echoed_prompt_example_is_not_a_real_task():
    items = extract_tasks(build_prompt(None, ["gooey-server"]))
    assert items and all(p.review_reason for p in proposals_from_items(items, run_id="r1"))


def test_long_invalid_item_description_is_capped():
    p = one({"title": "", "description": "x" * 5000})
    assert p.review_reason and len(p.description) <= 4000
