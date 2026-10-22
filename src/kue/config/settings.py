"""Loading configuration and secrets. Secrets come only from the environment."""
from __future__ import annotations

import os
from importlib import resources
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, SecretStr, ValidationError

from kue.config.models import AppConfig

HOME_DIR = Path("~/.kue").expanduser()


class ConfigError(Exception):
    """Raised when the config file is missing or invalid."""


def default_config_path() -> Path:
    return Path(os.environ.get("KUE_CONFIG", HOME_DIR / "config.yaml")).expanduser()


def template_text() -> str:
    return (
        resources.files("kue") / "templates" / "config.example.yaml"
    ).read_text(encoding="utf-8")


def load_config(path: Path | None = None) -> AppConfig:
    path = (path or default_config_path()).expanduser()
    if not path.exists():
        raise ConfigError(f"No config file at {path}. Run `kue init` to create one.")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level.")
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Invalid config in {path}:\n{exc}") from exc


class Secrets(BaseModel):
    gooey_api_url: str | None = None
    gooey_api_key: SecretStr | None = None


def load_secrets() -> Secrets:
    """Read secrets from ~/.kue/.env, a local .env, then the environment."""
    load_dotenv(HOME_DIR / ".env", override=False)
    load_dotenv(override=False)
    key = os.environ.get("GOOEY_API_KEY") or None
    return Secrets(
        gooey_api_url=os.environ.get("GOOEY_API_URL") or None,
        gooey_api_key=SecretStr(key) if key else None,
    )
