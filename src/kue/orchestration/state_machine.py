"""Task lifecycle rules. Every status change goes through here."""
from __future__ import annotations

from kue.tasks.models import TaskStatus as S


class InvalidTransition(ValueError):
    def __init__(self, current: S, target: S) -> None:
        super().__init__(f"Cannot move a task from {current} to {target}.")
        self.current = current
        self.target = target


_ESCALATE = frozenset({S.FAILED, S.BLOCKED, S.REQUIRES_HUMAN_REVIEW, S.CANCELLED})

TRANSITIONS: dict[S, frozenset[S]] = {
    S.DISCOVERED: frozenset({S.TRIAGED, S.REQUIRES_HUMAN_REVIEW, S.FAILED, S.CANCELLED}),
    S.TRIAGED: frozenset({S.QUEUED, S.BLOCKED, S.REQUIRES_HUMAN_REVIEW, S.CANCELLED}),
    S.QUEUED: frozenset({S.RUNNING, S.BLOCKED, S.REQUIRES_HUMAN_REVIEW, S.CANCELLED}),
    # RUNNING -> QUEUED covers "workspace turned out not to be ready, try later".
    S.RUNNING: frozenset({S.IMPLEMENTING, S.QUEUED}) | _ESCALATE,
    S.IMPLEMENTING: frozenset({S.TESTING}) | _ESCALATE,
    S.TESTING: frozenset({S.COMMITTING, S.IMPLEMENTING}) | _ESCALATE,
    S.COMMITTING: frozenset({S.PR_CREATED}) | _ESCALATE,
    S.PR_CREATED: frozenset({S.VALIDATING}) | _ESCALATE,
    S.VALIDATING: frozenset({S.AWAITING_REVIEW, S.READY_TO_MERGE}) | _ESCALATE,
    # With branch protection, GitHub auto-merge can merge while we wait: AWAITING_REVIEW -> COMPLETED.
    S.AWAITING_REVIEW: frozenset({S.VALIDATING, S.READY_TO_MERGE, S.COMPLETED}) | _ESCALATE,
    S.READY_TO_MERGE: frozenset({S.MERGING, S.AWAITING_REVIEW}) | _ESCALATE,
    S.MERGING: frozenset({S.COMPLETED}) | _ESCALATE,
    S.BLOCKED: frozenset({S.QUEUED, S.CANCELLED}),
    S.FAILED: frozenset({S.QUEUED, S.CANCELLED}),
    S.REQUIRES_HUMAN_REVIEW: frozenset({S.QUEUED, S.CANCELLED}),
    S.COMPLETED: frozenset(),
    S.CANCELLED: frozenset(),
}

TERMINAL = frozenset({S.COMPLETED, S.CANCELLED})

# States in which the local repository is actively being used by the worker.
WORKER_BUSY = frozenset(
    {S.RUNNING, S.IMPLEMENTING, S.TESTING, S.COMMITTING, S.PR_CREATED, S.MERGING}
)

NEEDS_ATTENTION = frozenset({S.BLOCKED, S.REQUIRES_HUMAN_REVIEW})


def can_transition(current: S, target: S) -> bool:
    return target in TRANSITIONS[current]


def assert_transition(current: S, target: S) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(current, target)
