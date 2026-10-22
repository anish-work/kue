import pytest

from kue.orchestration.state_machine import (
    TERMINAL,
    TRANSITIONS,
    WORKER_BUSY,
    InvalidTransition,
    assert_transition,
    can_transition,
)
from kue.tasks.models import TaskStatus as S


def test_every_status_has_rules():
    assert set(TRANSITIONS) == set(S)


def test_all_targets_are_valid_statuses():
    for targets in TRANSITIONS.values():
        assert targets <= set(S)


def test_terminal_states_have_no_exits():
    for s in TERMINAL:
        assert TRANSITIONS[s] == frozenset()


@pytest.mark.parametrize(
    "path",
    [
        # Branch-protected route: GitHub auto-merge completes the task.
        [S.DISCOVERED, S.TRIAGED, S.QUEUED, S.RUNNING, S.IMPLEMENTING, S.TESTING,
         S.COMMITTING, S.PR_CREATED, S.VALIDATING, S.AWAITING_REVIEW, S.COMPLETED],
        # Direct merge route.
        [S.QUEUED, S.RUNNING, S.IMPLEMENTING, S.TESTING, S.COMMITTING, S.PR_CREATED,
         S.VALIDATING, S.READY_TO_MERGE, S.MERGING, S.COMPLETED],
        # Test failure loops back to implementation.
        [S.IMPLEMENTING, S.TESTING, S.IMPLEMENTING],
        # Human loops.
        [S.RUNNING, S.FAILED, S.QUEUED],
        [S.TESTING, S.REQUIRES_HUMAN_REVIEW, S.QUEUED],
    ],
)
def test_valid_paths(path):
    for a, b in zip(path, path[1:]):
        assert_transition(a, b)


@pytest.mark.parametrize(
    "a,b",
    [(S.COMPLETED, S.QUEUED), (S.QUEUED, S.COMPLETED), (S.DISCOVERED, S.RUNNING),
     (S.CANCELLED, S.QUEUED), (S.REQUIRES_HUMAN_REVIEW, S.RUNNING)],
)
def test_invalid_transitions(a, b):
    assert not can_transition(a, b)
    with pytest.raises(InvalidTransition):
        assert_transition(a, b)


def test_every_busy_state_can_escalate():
    for s in WORKER_BUSY:
        assert can_transition(s, S.REQUIRES_HUMAN_REVIEW)
        assert can_transition(s, S.FAILED)
