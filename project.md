# Kue

Local-first autonomous engineer. It discovers engineering work (via a Gooey
agent that watches Discord + Asana), keeps every task in SQLite, hands coding
to Claude Code inside real local repositories, opens PRs, and reports back.

Owner: Anish (Gooey.AI). Runs on one Mac, no cloud anything.

**Current state: Phase 1 complete.** Config, SQLite, state machine, CLI,
logging, dry-run, 70 passing tests. No discovery, no coding worker, no GitHub
yet.

---

## Read this before `SPEC.md`

`SPEC.md` (the original "Autonomous Engineering Orchestrator" brief) is the
product vision and is still the best statement of *intent*. But it was written
before implementation, and several decisions have changed since. **Where the
two disagree, this file wins.** Known divergences:

| SPEC says | Reality |
|---|---|
| `ae` command, `autonomous_engineer` package | `kue`, package `kue` |
| `~/.autonomous-engineer/` | `~/.kue/` |
| `agent/AE-123-slug` branches | `kue/KUE-123-slug` |
| `autonomy.auto_merge: true` | `autonomy.merge_mode: never \| when_approved` |
| Dirty tree → `REQUIRES_HUMAN_REVIEW` | Skip and retry next cycle (configurable) |
| Claude Code commits and opens the PR | The orchestrator does; Claude Code only edits code |
| States end at `READY_TO_MERGE` | Added `AWAITING_REVIEW` for approval-gated repos |

Do not "fix" the code to match SPEC.md.

---

## Hard rules

1. **Kue never merges its own PRs.** Anish's repos require human approval.
   Kue opens the PR and enables GitHub auto-merge; a human approves. A task
   only reaches `COMPLETED` once the PR has actually merged.
2. **The orchestrator owns git. Claude Code only edits code.** Branch, commit,
   push, PR and merge are plain Python so they're predictable, testable and
   logged. Never grant the coding agent `git commit` or `git push`.
3. **Never touch uncommitted user work.** No auto-stash, no checkout over
   changes, no `--force`. A dirty tree means the task waits.
4. **Secrets live in the environment only** (`~/.kue/.env`). Never in SQLite,
   never in logs, never in config YAML. Event payloads pass through `redact()`.
5. **Never silently fail.** Every failure becomes an event and a task state.
6. **`--dry-run` writes nothing** — no DB rows, no lock file, no git calls.
7. **Tests never need real credentials or network.** Use the fake adapters.
8. **No new infrastructure.** No Docker requirement, Redis, Postgres, cloud
   queues or microservices. SQLite and the local filesystem are the whole
   substrate.

---

## Layout

```
src/kue/
  main.py              entry point
  config/              models.py (pydantic, extra="forbid"), settings.py (yaml + .env)
  database/            database.py (schema + migrations), repositories.py (Store)
  tasks/models.py      Task, TaskProposal, Run, Event, TaskStatus, Priority
  orchestration/
    state_machine.py   TRANSITIONS, WORKER_BUSY, TERMINAL, assert_transition
    orchestrator.py    the cycle; CycleReport; cycle_lock; select_next
    sources.py         DiscoverySource protocol  ← Gooey plugs in here
  coding/workspace.py  read-only git inspection, branch naming
  events/logger.py     EventType, redact(), JSON file logging
  environment/doctor.py
  cli/commands.py      all `kue` subcommands
tests/                 conftest.py builds real temp git repos
```

Phase 2+ adds `gooey/`, `github/`, `coding/claude_code.py`, `coding/policies.py`,
`orchestration/scheduler.py`.

---

## Invariants in the code

- **All status changes go through `Store.transition()`**, which validates
  against `TRANSITIONS` and writes the event in the same SQLite transaction.
  Never `UPDATE tasks SET status`.
- `Store.update_task()` only accepts fields in the `_UPDATABLE` allowlist, and
  `status` is deliberately not one of them.
- **Dedup key is `(source, external_id)`**, enforced by a UNIQUE constraint.
  Re-seeing a task bumps `last_seen_at` and nothing else.
- **Crash recovery:** anything found in `WORKER_BUSY` at the start of a cycle
  is moved to `REQUIRES_HUMAN_REVIEW`. Kue never resumes interrupted work blindly.
- **One task at a time.** If any task is in `WORKER_BUSY`, the cycle selects nothing.
- **Prioritisation is deterministic**: lowest priority number, then oldest,
  then lowest id. An LLM may *suggest* a priority; it never controls scheduling.
- Config is strict — an unknown YAML key is an error, not a shrug.
- `cycle.lock` (flock, next to the DB) prevents two concurrent cycles.

## The cycle

`recover → discover → triage → select → preflight → execute`

Preflight requires: repo exists, is a git repo, is on a branch (not detached),
and has zero uncommitted *or untracked* files. Phase 1 stops after preflight and
records `EXECUTION_DEFERRED` with the branch it would have created.

Nothing starts if: another task is busy, `kue pause` is set, or
`limits.max_runs_per_day` is reached.

---

## Decisions already made (don't relitigate)

- **Gooey returns free text, not JSON.** Phase 2 must ask for a fenced JSON
  block, extract it, validate with pydantic, keep the raw reply, and send
  anything unparseable to `REQUIRES_HUMAN_REVIEW`. Retry the parse once.
- **Asana updates go through Gooey**, which already has access. Don't add a
  direct Asana client.
- **Claude Code is billed to the company plan / API key.** Record
  `total_cost_usd` from each run into the `runs` table.
- **Workspace mode is `in_place`** — the agent works in Anish's own checkout,
  not a worktree or copy. He chose this knowing it means dirty-tree waits.
- **First real repo is `gooey-server`.** Its tests are one offline command.
- **Python 3.12+ required.** The code uses `StrEnum` and `datetime.UTC`; 3.10
  cannot run it, and pip will loop backwards through pydantic releases forever
  if you try.

## Planned Claude Code invocation (Phase 4)

Headless, launched from Python inside the repo directory:

```
claude -p "<task packet>" \
  --output-format json \
  --json-schema '<report schema>' \
  --permission-mode acceptEdits \
  --allowedTools "Bash(<test cmd> *),Bash(git diff *),Bash(git status *)" \
  --permission-prompts none
```

- `acceptEdits` lets it edit files; shell commands still need an allowlist
  entry, so it cannot commit or push.
- `--permission-prompts none` is for unattended runs (needs Claude Code
  ≥ 2.1.259 — check with `kue doctor`).
- `--json-schema` returns a structured report in `structured_output`:
  `{status: done|blocked|failed, summary, tests_run, concerns, blocked_reason}`.
- **Do not pass `--bare`** — we want the target repo's own `CLAUDE.md` and
  conventions loaded.
- Timeout via SIGTERM; Claude Code exits 143. Retry with `--resume <session_id>`
  plus the failure output, up to `limits.max_attempts`.

After the run, **the orchestrator verifies independently** — runs the tests
itself, checks diff size against `max_changed_lines`, flags migration/auth/infra
paths for review — rather than trusting the agent's self-report. Then it commits.

---

## Commands

`init` · `doctor` · `status` · `tasks` · `task <id>` · `add` ·
`run-once [--dry-run] [--task ID]` · `pause` · `resume` · `retry` · `approve` ·
`cancel` · `logs` · `runs`

`kue start` / `stop` arrive with the Phase 3 scheduler.

## Working on this codebase

```bash
source .venv/bin/activate      # Python 3.12
pytest                         # must stay green
kue doctor
```

- Config override: `--config PATH` or `KUE_CONFIG`.
- Add tests alongside behaviour; the test suite is the safety net for a system
  that will eventually push code unattended.
- Keep it boring. Clean, observable and reliable beats clever — this thing gets
  write access to real repositories.

## Roadmap

2 Gooey client · 3 scheduler daemon · 4 Claude Code worker · 5 commit/push/PR/CI ·
6 merge policy engine · 7 Asana feedback · 8 macOS app · 9 iPhone app
