"""Typed configuration. Unknown keys are rejected so typos fail loudly."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_REPO_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def _expand(value: str | Path) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepositoryConfig(_Strict):
    path: Path
    github_repo: str | None = None
    default_branch: str = "main"
    test_command: str | None = None
    enabled: bool = True

    @field_validator("path", mode="before")
    @classmethod
    def _expand_path(cls, v: str | Path) -> Path:
        return _expand(v)

    @field_validator("test_command", "github_repo", mode="before")
    @classmethod
    def _blank_is_none(cls, v: str | None) -> str | None:
        return v.strip() or None if isinstance(v, str) else v


class SchedulerConfig(_Strict):
    interval_minutes: int = Field(default=60, ge=1)


class WorkspaceConfig(_Strict):
    mode: Literal["in_place"] = "in_place"
    dirty_tree_action: Literal["skip", "require_review"] = "skip"


class AutonomyConfig(_Strict):
    auto_commit: bool = True
    auto_push: bool = True
    auto_create_pr: bool = True
    merge_mode: Literal["never", "when_approved"] = "when_approved"
    max_changed_lines: int = Field(default=400, ge=1)
    require_review_for: list[str] = Field(
        default_factory=lambda: [
            "database_migration",
            "security",
            "infrastructure",
            "production_config",
            "destructive_operation",
        ]
    )


class LimitsConfig(_Strict):
    max_attempts: int = Field(default=3, ge=1)
    max_runs_per_day: int = Field(default=10, ge=0)
    task_timeout_minutes: int = Field(default=45, ge=1)


class GooeyConfig(_Strict):
    agent_example_id: str | None = None
    timeout_minutes: int = Field(default=10, ge=1)

    @field_validator("agent_example_id", mode="before")
    @classmethod
    def _blank_is_none(cls, v: str | None) -> str | None:
        return v.strip() or None if isinstance(v, str) else v


class CommandConfig(_Strict):
    command: str


class DatabaseConfig(_Strict):
    path: Path = Field(default=Path("~/.kue/state.db"), validate_default=True)

    @field_validator("path", mode="before")
    @classmethod
    def _expand_path(cls, v: str | Path) -> Path:
        return _expand(v)


class LoggingConfig(_Strict):
    dir: Path = Field(default=Path("~/.kue/logs"), validate_default=True)
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("dir", mode="before")
    @classmethod
    def _expand_path(cls, v: str | Path) -> Path:
        return _expand(v)


class AppConfig(_Strict):
    repositories: dict[str, RepositoryConfig] = Field(default_factory=dict)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    claude: CommandConfig = Field(default_factory=lambda: CommandConfig(command="claude"))
    github: CommandConfig = Field(default_factory=lambda: CommandConfig(command="gh"))
    gooey: GooeyConfig = Field(default_factory=GooeyConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @field_validator("repositories")
    @classmethod
    def _valid_names(cls, repos: dict[str, RepositoryConfig]) -> dict[str, RepositoryConfig]:
        for name in repos:
            if not _REPO_NAME.match(name):
                raise ValueError(f"Invalid repository name {name!r} (use letters, digits, . _ -)")
        return repos

    @property
    def lock_path(self) -> Path:
        return self.database.path.parent / "cycle.lock"

    def resolve_repository(self, name: str | None) -> RepositoryConfig | None:
        """Return the repo config only if it exists and is enabled."""
        if not name:
            return None
        repo = self.repositories.get(name)
        return repo if repo and repo.enabled else None
