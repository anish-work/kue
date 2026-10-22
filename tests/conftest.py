import subprocess
from pathlib import Path

import pytest
import yaml

from kue.config.models import AppConfig
from kue.database.database import connect
from kue.database.repositories import Store


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "gooey-server"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


@pytest.fixture
def config_dict(tmp_path: Path, git_repo: Path) -> dict:
    return {
        "repositories": {
            "gooey-server": {"path": str(git_repo), "github_repo": "org/gooey-server"},
            "old-repo": {"path": str(tmp_path / "old"), "enabled": False},
        },
        "database": {"path": str(tmp_path / "home" / "state.db")},
        "logging": {"dir": str(tmp_path / "home" / "logs")},
    }


@pytest.fixture
def config(config_dict: dict) -> AppConfig:
    return AppConfig.model_validate(config_dict)


@pytest.fixture
def config_file(tmp_path: Path, config_dict: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config_dict))
    return path


@pytest.fixture
def store(config: AppConfig) -> Store:
    return Store(connect(config.database.path))
