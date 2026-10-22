"""Discovery source interface. Gooey (Phase 2) will be one implementation."""
from __future__ import annotations

from typing import Protocol

from kue.tasks.models import TaskProposal


class DiscoverySource(Protocol):
    name: str

    async def fetch(self) -> list[TaskProposal]:
        """Return task proposals. Raise on failure; the orchestrator records it."""
        ...
