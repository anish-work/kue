import json
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml

from kue.config.models import AppConfig
from kue.database.database import connect
from kue.database.repositories import Store
from kue.gooey.client import GooeyClient

GOOEY_BASE = "https://api.example.test"
_GOOEY_PATHS = {"/v3/agent/async/": "start", "/v3/agent/status/": "status", "/v1/balance/": "balance"}


class FakeGooey:
    """A scripted Gooey API over httpx.MockTransport. Records every request; time is fake."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: dict[str, list[httpx.Response]] = {name: [] for name in _GOOEY_PATHS.values()}
        self.raise_error: Exception | None = None
        self.now = 0.0
        self.transport = httpx.MockTransport(self._handle)

    def queue(self, endpoint: str, status_code: int = 200, json: object = None) -> None:
        self.responses[endpoint].append(httpx.Response(status_code, json=json))

    def started(self, run_id: str = "r1") -> None:
        self.queue("start", 202, {"run_id": run_id, "web_url": f"https://gooey.example/{run_id}",
                                  "created_at": "2026-10-23T10:00:00", "status_url": "https://evil.example/status"})

    def status(self, state: str, run_id: str = "r1", **extra: object) -> None:
        self.queue("status", 200, {"run_id": run_id, "web_url": f"https://gooey.example/{run_id}",
                                   "status": state, "detail": "", **extra})

    def completed(self, text: str, run_id: str = "r1") -> None:
        self.status("completed", run_id=run_id, output={"output_text": [text]})

    def bodies(self, endpoint: str) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if _GOOEY_PATHS[r.url.path] == endpoint]

    def client(self, **kwargs) -> GooeyClient:
        return GooeyClient(GOOEY_BASE, "k", transport=self.transport, sleep=self.sleep, clock=self.clock, **kwargs)

    async def sleep(self, seconds: float) -> None:
        self.now += seconds

    def clock(self) -> float:
        return self.now

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_error:
            raise self.raise_error
        return self.responses[_GOOEY_PATHS[request.url.path]].pop(0)


@pytest.fixture
def fake_gooey() -> FakeGooey:
    return FakeGooey()


@pytest.fixture(autouse=True)
def no_real_secrets(monkeypatch):
    """Tests never see a developer's real Gooey key, from the environment or a .env file."""
    monkeypatch.delenv("GOOEY_API_URL", raising=False)
    monkeypatch.delenv("GOOEY_API_KEY", raising=False)
    monkeypatch.setattr("kue.config.settings.load_dotenv", lambda *a, **k: False)


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
