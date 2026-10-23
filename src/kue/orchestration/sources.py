"""Discovery source interface, and the sources the config turns on."""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from kue.config.models import AppConfig
from kue.config.settings import ConfigError, Secrets
from kue.gooey.client import GooeyClient
from kue.gooey.discovery import GooeySource
from kue.tasks.models import TaskProposal


class DiscoverySource(Protocol):
    name: str

    async def fetch(self, since: datetime | None, *, dry_run: bool) -> list[TaskProposal]:
        """Return proposals for work since `since` (None on the first fetch).

        Raise on failure; the orchestrator records it. In a dry run, write nothing.
        """
        ...


def configured_sources(config: AppConfig, secrets: Secrets) -> list[DiscoverySource]:
    """Sources the config turns on. Raises ConfigError for one that's only half set up."""
    gooey = config.gooey
    if not gooey.agent_example_id:
        return []
    if not (secrets.gooey_api_url and secrets.gooey_api_key):
        raise ConfigError(
            "gooey.agent_example_id is set, but GOOEY_API_URL and GOOEY_API_KEY aren't. "
            "Add them to ~/.kue/.env."
        )
    client = GooeyClient(
        secrets.gooey_api_url,
        secrets.gooey_api_key.get_secret_value(),
        timeout_minutes=gooey.timeout_minutes,
    )
    repositories = sorted(name for name, repo in config.repositories.items() if repo.enabled)
    replies_dir = config.database.path.parent / "replies"
    return [GooeySource(client, gooey.agent_example_id, repositories, replies_dir)]
