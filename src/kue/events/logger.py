"""Event names, secret redaction, and structured JSON file logging."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

LOGGER_NAME = "kue"
_SENSITIVE = ("token", "secret", "password", "api_key", "apikey", "authorization", "credential")
REDACTED = "***"


class EventType:
    CYCLE_STARTED = "CYCLE_STARTED"
    CYCLE_FINISHED = "CYCLE_FINISHED"
    SOURCE_SYNC_FAILED = "SOURCE_SYNC_FAILED"
    TASK_CREATED = "TASK_CREATED"
    TASK_LINKED = "TASK_LINKED"  # a known task learned its Asana or Discord id
    TASK_STATUS_CHANGED = "TASK_STATUS_CHANGED"
    WORKSPACE_NOT_READY = "WORKSPACE_NOT_READY"
    EXECUTION_DEFERRED = "EXECUTION_DEFERRED"
    DAEMON_PAUSED = "DAEMON_PAUSED"
    DAEMON_RESUMED = "DAEMON_RESUMED"


def redact(value: Any) -> Any:
    """Recursively replace values whose key looks like a secret."""
    if isinstance(value, dict):
        return {
            k: REDACTED if any(s in str(k).lower() for s in _SENSITIVE) else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "data", None)
        if extra:
            entry["data"] = redact(extra)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def setup_logging(log_dir: Path, level: str = "INFO") -> logging.Logger:
    """Idempotent: safe to call on every CLI invocation."""
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        if getattr(handler, "_kue_handler", False):
            logger.removeHandler(handler)
            handler.close()
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / "kue.log", maxBytes=5_000_000, backupCount=3)
    handler.setFormatter(JsonFormatter())
    handler._kue_handler = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
