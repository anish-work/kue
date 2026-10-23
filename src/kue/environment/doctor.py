"""Environment checks for `kue doctor`. Never prints secret values."""
from __future__ import annotations

import asyncio
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass

from kue.coding.workspace import inspect_repository
from kue.config.models import AppConfig
from kue.config.settings import Secrets
from kue.database.database import connect, schema_version
from kue.gooey.client import GooeyClient, GooeyError


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def _run(cmd: list[str]) -> tuple[bool, str]:
    if not cmd or shutil.which(cmd[0]) is None:
        return False, "not found on PATH"
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    text = (out.stdout or out.stderr).strip().splitlines()
    return out.returncode == 0, text[0] if text else f"exit code {out.returncode}"


def _gooey_key_check(base_url: str, api_key: str) -> Check:
    """Checks the key with Gooey's balance endpoint, which doesn't run anything."""
    try:
        balance = asyncio.run(GooeyClient(base_url, api_key).balance())
    except GooeyError as exc:
        return Check("Gooey API key", False, str(exc), required=False)
    return Check("Gooey API key", True, f"valid, {balance} credits", required=False)


def run_checks(config: AppConfig | None, config_error: str | None, secrets: Secrets) -> list[Check]:
    checks: list[Check] = []
    v = sys.version_info
    checks.append(Check("Python >= 3.12", v >= (3, 12), f"{v.major}.{v.minor}.{v.micro}"))
    ok, detail = _run(["git", "--version"])
    checks.append(Check("git", ok, detail))

    checks.append(Check("config", config is not None, config_error or "loaded"))
    if config is None:
        return checks

    gh = shlex.split(config.github.command)
    ok, detail = _run([*gh, "--version"])
    checks.append(Check("GitHub CLI (Phase 5)", ok, detail, required=False))
    if ok:
        ok, detail = _run([*gh, "auth", "status"])
        checks.append(Check("GitHub CLI auth", ok, "logged in" if ok else detail, required=False))

    ok, detail = _run([*shlex.split(config.claude.command), "--version"])
    checks.append(Check("Claude Code CLI (Phase 4)", ok, detail, required=False))

    checks.append(
        Check(
            "Gooey credentials",
            bool(secrets.gooey_api_url and secrets.gooey_api_key),
            "GOOEY_API_URL and GOOEY_API_KEY set"
            if secrets.gooey_api_url and secrets.gooey_api_key
            else "set GOOEY_API_URL and GOOEY_API_KEY in ~/.kue/.env",
            required=False,
        )
    )
    checks.append(
        Check(
            "Gooey agent",
            config.gooey.agent_example_id is not None,
            "set" if config.gooey.agent_example_id else "set gooey.agent_example_id in config.yaml",
            required=False,
        )
    )
    if secrets.gooey_api_url and secrets.gooey_api_key:
        checks.append(_gooey_key_check(secrets.gooey_api_url, secrets.gooey_api_key.get_secret_value()))

    try:
        conn = connect(config.database.path)
        checks.append(Check("database", True, f"{config.database.path} (schema v{schema_version(conn)})"))
        conn.close()
    except Exception as exc:
        checks.append(Check("database", False, str(exc)))

    if not config.repositories:
        checks.append(Check("repositories", False, "none configured"))
    for name, repo in config.repositories.items():
        if not repo.enabled:
            checks.append(Check(f"repo {name}", True, "disabled", required=False))
            continue
        state = inspect_repository(repo.path)
        if not state.exists or not state.is_git or state.error:
            checks.append(Check(f"repo {name}", False, state.problem or "unknown problem"))
        else:
            note = f"on {state.branch or 'detached HEAD'}"
            if state.dirty:
                note += f", {len(state.dirty_files)} uncommitted file(s) (tasks will wait)"
            checks.append(Check(f"repo {name}", True, note))
    return checks
