# Kue

Kue is a local-first autonomous engineer for your Mac. It discovers engineering work
(through Gooey), keeps every task in SQLite, and hands coding to Claude Code
inside your own repositories. It opens PRs and lets GitHub auto-merge them
once a teammate approves.

**Current status: Phase 1 (foundation).** Config, database, task state
machine, CLI, logging and dry runs all work. Discovery, coding and GitHub
are not built yet.

## Setup

```bash
cd kue
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

kue init          # creates ~/.kue/config.yaml
# edit the config: set repositories.gooey-server.path to your checkout
cp .env.example ~/.kue/.env   # fill in later (Phase 2)
kue doctor        # checks python, git, gh, claude, credentials, repos
```

Use `--config PATH` (or the `KUE_CONFIG` environment variable) to point at a
different config file.

## Commands

| Command | What it does |
|---|---|
| `kue init` | Create a config from the template |
| `kue doctor` | Check the environment (exits 1 on required failures) |
| `kue status` | Paused/active, last cycle, task counts |
| `kue tasks [--status S] [--all]` | Tasks in priority order |
| `kue task ID` | One task with its history |
| `kue add "title" --repo NAME --priority 0-5` | Add a manual task |
| `kue run-once [--dry-run] [--task ID]` | Run one orchestration cycle |
| `kue pause` / `kue resume` | Emergency stop for new work |
| `kue retry ID` | FAILED/BLOCKED → QUEUED |
| `kue approve ID` | REQUIRES_HUMAN_REVIEW → QUEUED |
| `kue cancel ID` | Cancel a task that isn't mid-run |
| `kue logs [--task ID] [--today]` | Event history |
| `kue runs` | Coding-agent runs (empty until Phase 4) |

`kue start` / `kue stop` arrive with the scheduler in Phase 3.

## What one cycle does

1. **Recover.** Any task left mid-work (after a crash or restart) moves to
   `REQUIRES_HUMAN_REVIEW`. The system never resumes blindly.
2. **Discover.** It asks each discovery source for proposals and deduplicates
   them on `(source, external_id)`. There are no sources in Phase 1.
3. **Triage.** Tasks whose repository resolves to an enabled repo become
   `QUEUED`; the rest go to review.
4. **Select.** It picks the lowest priority number, then the oldest task.
   Nothing starts if another task is in progress, the system is paused, or the
   daily run limit is reached.
5. **Preflight.** The repo must exist, be a git repo, be on a branch, and have
   no uncommitted or untracked files. A dirty repo is skipped until next cycle
   (or sent to review, depending on `workspace.dirty_tree_action`).
6. **Execute.** Phase 1 only records `EXECUTION_DEFERRED` with the planned
   branch (`kue/KUE-<id>-<slug>`). The task stays `QUEUED`.

`--dry-run` performs no database writes and takes no lock. A lock file
(`cycle.lock` next to the database) stops two cycles from running at once.

## Task lifecycle

```
DISCOVERED → TRIAGED → QUEUED → RUNNING → IMPLEMENTING → TESTING → COMMITTING
→ PR_CREATED → VALIDATING → AWAITING_REVIEW → (GitHub auto-merge) → COMPLETED
                                     └→ READY_TO_MERGE → MERGING → COMPLETED
Any working state → FAILED | BLOCKED | REQUIRES_HUMAN_REVIEW | CANCELLED
FAILED/BLOCKED --retry--> QUEUED      REQUIRES_HUMAN_REVIEW --approve--> QUEUED
```

The allowed moves live in `orchestration/state_machine.py`. Every change is
recorded as an event in the same transaction.

## Design decisions

- **Reviews are required on company repos.** The system never merges on its
  own. It enables GitHub auto-merge (`merge_mode: when_approved`), and a task
  becomes `COMPLETED` only once the PR has actually merged.
- **The agent works in your own checkout** (`workspace.mode: in_place`). It
  runs only when the tree is clean, and in later phases it will switch back to
  your original branch afterwards.
- **Gooey returns free text.** Phase 2 will ask for a JSON block, validate it
  with Pydantic, keep the raw reply, and send anything unparseable to review.
- **Asana updates go through Gooey.** They are recorded as requested, then
  confirmed on the next sync.
- **Secrets live only in the environment** (`~/.kue/.env`),
  never in SQLite or logs. Event payloads are redacted.

## Files

- Database: `~/.kue/state.db` (tables `tasks`, `runs`,
  `events`, `discovery_cursor`, `daemon_state`)
- Logs: `~/.kue/logs/kue.log` (JSON lines, rotated)

## Tests

```bash
pytest
```

The tests use temporary git repositories and databases. No credentials are needed.

## Roadmap

2. Gooey client and structured task proposals
3. Scheduler daemon (`kue start/stop`)
4. Claude Code worker (headless), branches, run capture, timeouts
5. Commit, push, PR, CI monitoring
6. Merge policy engine (auto-merge after approval)
7. Asana feedback through Gooey
8. macOS app
9. iPhone app
