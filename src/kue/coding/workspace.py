"""Read-only inspection of local repositories. Nothing here modifies a repo."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RepoState:
    path: Path
    exists: bool = False
    is_git: bool = False
    branch: str | None = None
    dirty_files: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def dirty(self) -> bool:
        return bool(self.dirty_files)

    @property
    def problem(self) -> str | None:
        """Why the agent must not start here, or None if the repo is ready."""
        if not self.exists:
            return f"Repository path does not exist: {self.path}"
        if not self.is_git:
            return f"Not a git repository: {self.path}"
        if self.error:
            return f"Could not read git state: {self.error}"
        if self.branch is None:
            return "Repository is in detached HEAD state."
        if self.dirty:
            return f"Repository has {len(self.dirty_files)} uncommitted/untracked file(s)."
        return None


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, timeout=30
    )


def inspect_repository(path: Path) -> RepoState:
    state = RepoState(path=path)
    if not path.is_dir():
        return state
    state.exists = True
    inside = _git(path, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return state
    state.is_git = True
    branch = _git(path, "branch", "--show-current")
    status = _git(path, "status", "--porcelain", "--untracked-files=normal")
    if branch.returncode != 0 or status.returncode != 0:
        state.error = (branch.stderr or status.stderr).strip() or "git command failed"
        return state
    state.branch = branch.stdout.strip() or None
    # Untracked files count as dirty: the agent might otherwise commit them.
    state.dirty_files = [line[3:] for line in status.stdout.splitlines() if line.strip()]
    return state


def branch_name_for(task_id: int, title: str, max_slug: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = slug[:max_slug].rstrip("-") or "task"
    return f"kue/KUE-{task_id}-{slug}"
