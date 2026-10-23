"""Gooey discovery: the reply contract, and parsing the agent's reply into task proposals."""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from kue.tasks.models import Priority, TaskProposal

SOURCE = "gooey"
MAX_DESCRIPTION = 4000
UNREADABLE_TITLE = "Unreadable task from Gooey"
_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_ID = re.compile(r"^\d{1,32}$")  # Asana gids and Discord snowflakes are both numeric


class ReplyFormatError(ValueError):
    """The agent's reply as a whole couldn't be read."""


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
        # A bad priority isn't worth a human's attention; treat it as normal work.
        if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 5:
            return v
        return Priority.P4


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
        return TaskProposal(**base, title=_title_hint(item), description=_dump(item),
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
