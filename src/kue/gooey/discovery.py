"""Gooey discovery: the prompt Kue sends, the reply contract, and parsing replies into proposals.

Kue owns the prompt and the contract because they change together with the parser. The
agent's own instructions in Gooey cover how it uses its Discord and Asana tools.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from kue.events.logger import get_logger
from kue.gooey.client import AgentReply, ChatMessage, GooeyClient
from kue.tasks.models import Priority, TaskProposal

log = get_logger("gooey")

SOURCE = "gooey"
MAX_DESCRIPTION = 4000
UNREADABLE_TITLE = "Unreadable task from Gooey"
# Fences must start a line. JSON strings can't hold raw newlines, so backticks inside
# a value (a pasted traceback) or inline in the prose never open or close a block.
_FENCE = re.compile(r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```", re.DOTALL | re.MULTILINE)
_ID = re.compile(r"^\d{1,32}$")  # Asana gids and Discord snowflakes are both numeric


class ReplyFormatError(ValueError):
    """The agent's reply as a whole couldn't be read."""


_PROMPT = """\
Find engineering work for Anish that appeared or changed {since} in the Discord \
channels and Asana projects you can read. Only include work that needs a code change \
in one of these repositories: {repositories}. Skip chatter, answered questions and work \
that's already done.

Reply with exactly one fenced JSON block in this shape:

```json
{{"tasks": [{{"title": "One line saying what needs doing", "description": "What's wrong, \
what done looks like, and links", "repository": "<one of the repositories above>", \
"priority": 4, "asana_task_id": "<the Asana task's gid>", "discord_message_id": null}}]}}
```

- title: one short line saying what needs doing.
- description: the context a developer needs, with links.
- repository: one of the repositories above, or null if you can't tell.
- priority: 0 production outage, 1 production bug, 2 assigned to Anish, 3 due soon, \
4 normal, 5 nice to have.
- asana_task_id: the Asana task's numeric gid, if the work is tracked in Asana.
- discord_message_id: the numeric id of the Discord message where it came up, if any.

Every task needs at least one of the two ids. If there's nothing new, reply with \
{{"tasks": []}} inside the block.
"""

_RETRY_PROMPT = (
    "Kue couldn't read your last reply: {problem} Reply again with only the fenced JSON "
    "block described in the first message, and nothing after it."
)


def build_prompt(since: datetime | None, repositories: list[str]) -> str:
    if since is None:
        window = "in the last 24 hours"
    else:
        window = f"since {since.astimezone(UTC).isoformat(timespec='seconds')}"
    return _PROMPT.format(since=window, repositories=", ".join(repositories) or "(none are configured)")


class GooeySource:
    """A DiscoverySource that asks one published Gooey agent for new work."""

    name = SOURCE

    def __init__(self, client: GooeyClient, example_id: str, repositories: list[str], replies_dir: Path) -> None:
        self._client = client
        self._example_id = example_id
        self._repositories = repositories
        self._replies_dir = replies_dir

    async def fetch(self, since: datetime | None, *, dry_run: bool) -> list[TaskProposal]:
        prompt = build_prompt(since, self._repositories)
        first = await self._ask(prompt, [], dry_run=dry_run)
        try:
            return proposals_from_items(extract_tasks(first.text), run_id=first.run_id)
        except ReplyFormatError as exc:
            problem = str(exc)

        history: list[ChatMessage] = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": first.text},
        ]
        retry = await self._ask(_RETRY_PROMPT.format(problem=problem), history, dry_run=dry_run)
        try:
            return proposals_from_items(extract_tasks(retry.text), run_id=retry.run_id)
        except ReplyFormatError as exc:
            return [_unreadable(first, retry, str(exc))]

    async def _ask(self, prompt: str, messages: list[ChatMessage], *, dry_run: bool) -> AgentReply:
        reply = await self._client.run_agent(self._example_id, prompt, messages)
        saved_to = None
        if not dry_run:
            self._replies_dir.mkdir(parents=True, exist_ok=True)
            saved_to = self._replies_dir / f"{reply.run_id}.txt"
            saved_to.write_text(reply.text, encoding="utf-8")
        log.info("gooey reply", extra={"data": {
            "run_id": reply.run_id, "web_url": reply.web_url, "chars": len(reply.text),
            "saved_to": str(saved_to) if saved_to else None,
        }})
        return reply


def _unreadable(first: AgentReply, retry: AgentReply, problem: str) -> TaskProposal:
    description = f"Gooey runs: {first.web_url} and {retry.web_url}\n\nLast reply:\n{retry.text}"
    return TaskProposal(
        source=SOURCE,
        external_id=f"gooey-run:{retry.run_id}",
        title="Gooey's reply couldn't be read",
        description=description[:MAX_DESCRIPTION],
        review_reason=f"Gooey's reply couldn't be read, twice: {problem}",
    )


def extract_tasks(text: str) -> list[Any]:
    """Return the "tasks" list from the last fenced block. Items are still unvalidated JSON."""
    blocks = _FENCE.findall(text)
    if not blocks:
        raise ReplyFormatError("The reply has no fenced JSON block.")
    try:
        data = json.loads(blocks[-1])
    except json.JSONDecodeError as exc:
        raise ReplyFormatError(f"The JSON block isn't valid JSON: {exc.msg} at line {exc.lineno}.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise ReplyFormatError('The JSON block must be an object with a "tasks" list.')
    return data["tasks"]


def proposals_from_items(items: list[Any], *, run_id: str) -> list[TaskProposal]:
    """One proposal per item. Items Kue can't fully trust become review proposals."""
    return [_proposal(item, fallback_id=f"gooey-run:{run_id}:{i}") for i, item in enumerate(items)]


class _ProposedTask(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    repository: str | None = None
    priority: int = Priority.P4

    @field_validator("title", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v

    @field_validator("description", mode="before")
    @classmethod
    def _none_is_blank(cls, v: Any) -> Any:
        return "" if v is None else v

    @field_validator("description")
    @classmethod
    def _cap(cls, v: str) -> str:
        return v[:MAX_DESCRIPTION]

    @field_validator("repository", mode="before")
    @classmethod
    def _blank_is_none(cls, v: Any) -> Any:
        return v.strip() or None if isinstance(v, str) else v

    @field_validator("priority", mode="before")
    @classmethod
    def _fallback_priority(cls, v: Any) -> int:
        return _priority_or_normal(v)


def _priority_or_normal(v: Any) -> int:
    # A bad priority isn't worth a human's attention; treat it as normal work.
    if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 5:
        return v
    return Priority.P4


def _salvaged(item: dict[str, Any]) -> dict[str, Any]:
    """What an invalid item still says about where the work goes, so approving it can work."""
    repository = item.get("repository")
    if isinstance(repository, str) and repository.strip():
        repository = repository.strip()[:100]
    else:
        repository = None
    return {"repository": repository, "priority": _priority_or_normal(item.get("priority"))}


def _proposal(item: Any, *, fallback_id: str) -> TaskProposal:
    fields = item if isinstance(item, dict) else {}
    asana = _usable_id(fields.get("asana_task_id"))
    discord = _usable_id(fields.get("discord_message_id"))
    # Identity first: a usable id keys the task even when the rest of the item is bad,
    # so the same broken item reported next cycle doesn't create a second task.
    if asana:
        external_id = f"asana:{asana}"
    elif discord:
        external_id = f"discord:{discord}"
    else:
        external_id = fallback_id
    base = {"source": SOURCE, "external_id": external_id, "asana_task_id": asana, "discord_message_id": discord}

    if not isinstance(item, dict):
        return TaskProposal(**base, title=UNREADABLE_TITLE, description=_dump(item),
                            review_reason="Gooey sent a task that isn't a JSON object.")
    try:
        task = _ProposedTask.model_validate(item)
    except ValidationError as exc:
        return TaskProposal(**base, **_salvaged(item), title=_title_hint(item), description=_dump(item),
                            review_reason=f"Gooey's task was invalid: {_summary(exc)}")
    if not (asana or discord):
        return TaskProposal(**base, title=task.title, description=_dump(item),
                            repository=task.repository, priority=task.priority,
                            review_reason="Gooey's task has no usable Asana or Discord id, "
                                          "so Kue can't tell whether it's new.")
    return TaskProposal(**base, title=task.title, description=task.description,
                        repository=task.repository, priority=task.priority)


def _usable_id(value: Any) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if isinstance(value, str) and _ID.match(value.strip()):
        return value.strip()
    return None


def _title_hint(item: dict[str, Any]) -> str:
    title = item.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()[:200]
    return UNREADABLE_TITLE


def _summary(exc: ValidationError) -> str:
    parts = [f"{'.'.join(str(p) for p in e['loc']) or 'item'}: {e['msg']}" for e in exc.errors()]
    return "; ".join(parts)[:300]


def _dump(item: Any) -> str:
    return json.dumps(item, indent=2, ensure_ascii=False, default=str)[:MAX_DESCRIPTION]
