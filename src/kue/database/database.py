"""SQLite connection, schema and migrations."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA_V1 = """
CREATE TABLE tasks (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id        TEXT NOT NULL,
    source             TEXT NOT NULL,
    title              TEXT NOT NULL,
    description        TEXT NOT NULL DEFAULT '',
    repository         TEXT,
    priority           INTEGER NOT NULL DEFAULT 4,
    status             TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    last_seen_at       TEXT NOT NULL,
    attempt_count      INTEGER NOT NULL DEFAULT 0,
    current_run_id     INTEGER,
    branch_name        TEXT,
    original_branch    TEXT,
    pr_number          INTEGER,
    asana_task_id      TEXT,
    discord_message_id TEXT,
    autonomy_policy    TEXT,
    failure_reason     TEXT,
    UNIQUE (source, external_id)
);
CREATE INDEX idx_tasks_status ON tasks (status);

CREATE TABLE runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks (id),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    agent       TEXT NOT NULL,
    command     TEXT,
    exit_code   INTEGER,
    stdout_path TEXT,
    stderr_path TEXT,
    summary     TEXT
);
CREATE INDEX idx_runs_started ON runs (started_at);

CREATE TABLE events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER REFERENCES tasks (id),
    timestamp  TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_events_ts ON events (timestamp);
CREATE INDEX idx_events_task ON events (task_id);

CREATE TABLE discovery_cursor (
    source         TEXT PRIMARY KEY,
    cursor         TEXT,
    last_synced_at TEXT
);

-- Small key/value store for daemon state (paused flag, last cycle time).
-- Never store secrets here.
CREATE TABLE daemon_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL
);
"""

_MIGRATIONS = {1: _SCHEMA_V1}


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # we manage transactions explicitly
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    migrate(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> None:
    current = schema_version(conn)
    for version in range(current + 1, SCHEMA_VERSION + 1):
        conn.executescript(
            f"BEGIN;\n{_MIGRATIONS[version]}\nPRAGMA user_version = {version};\nCOMMIT;"
        )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
