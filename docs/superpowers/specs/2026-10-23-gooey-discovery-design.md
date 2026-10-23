# Phase 2: Gooey discovery

Status: approved by Anish on 2026-10-23. Builder: read `project.md` first; its hard
rules apply here and win over anything below.

## Intent

Kue needs real work to do. A Gooey agent already has (or will have) read access to
Discord and Asana through Composio tools. Phase 2 makes Kue call that agent once per
cycle, turn its free-text reply into validated `TaskProposal`s, and feed them into the
existing cycle. Nothing after discovery changes: triage, selection and preflight
behave exactly as in Phase 1.

## Goals

- `GooeySource`, a `DiscoverySource` that calls one published Gooey agent and returns
  proposals.
- A small async Gooey HTTP client that starts a run, polls it to completion and turns
  every failure into a clear, specific error.
- Parsing that follows the decision in `project.md`: fenced JSON block, pydantic
  validation, raw reply kept, one retry, anything unreadable goes to
  `REQUIRES_HUMAN_REVIEW`.
- A cursor so each cycle only asks about work since the last successful fetch.
- `kue doctor` verifies the Gooey key without running the agent.

## Non-goals

- Asana or Discord write-back (Phase 7, through a separate Gooey agent).
- Any direct Discord, Asana or Composio client in Kue.
- A local or alternative LLM backend (decided against on 2026-10-23).
- The scheduler daemon (Phase 3). Discovery runs inside `kue run-once`.

## Anti-goals (fail even if it "works")

- A discovery call that can write to Discord or Asana. The discovery agent must only
  have read tools. Kue cannot enforce this; the setup docs must say it plainly.
- Silent loss: a reply Kue couldn't read that leaves no task and no event.
- Duplicate tasks for the same Asana task or Discord message.
- Sending the API key anywhere except `GOOEY_API_URL`.
- Tests that need the network or a real key.

## What Gooey's API does (read from gooey-server, 2026-10-23)

- Auth: `Authorization: Bearer $GOOEY_API_KEY`.
- Agent workflow slugs: `video-bots`, `bots`, `copilot`, `agent`. The canonical one is
  `agent`. Routes are registered with and without a trailing slash, so no redirects.
- A published agent is selected with the query parameter `example_id`. The JSON body
  overrides that agent's saved settings.
- Start a run: `POST /v3/agent/async/?example_id=…` returns 202 with
  `{run_id, web_url, created_at, status_url}`.
- Poll: `GET /v3/agent/status/?run_id=…` returns
  `{run_id, web_url, created_at, run_time_sec, status, detail, output?}`. `status` is
  one of `standby`, `starting`, `running`, `completed`, `failed`. `output` is present
  only when completed.
- Request fields used: `input_prompt: str`, `messages: list[{"role", "content"}]`.
- Reply text: `output.output_text[0]` (a list because of `num_outputs`).
- Errors arrive as `{"detail": {"error": "…"}}` (sometimes with `id`, `url`):
  - 401 / 403: bad or missing key ("Invalid API Key.", "Invalid Authorization header…").
  - 401 with "Connect your account to continue: <url>": a Composio account the agent
    needs isn't connected (`ComposioAuthRequired`). This is not a bad key.
  - 402: out of credits.
  - 429: rate limited; the error text says when to retry.
  - The status endpoint returns the run's own error code (for example 402 or 401) when
    the run failed with one; otherwise a failed run is `status: "failed"` with `detail`.
- `GET /v1/balance/` returns `{balance: int}` for the key's workspace. It does not run
  anything.

## Design

### Configuration

New strict section in `config.yaml` (template and root example, which must match):

```yaml
gooey:
  # Published Gooey agent that reads Discord and Asana. Give it read-only tools.
  # Leave blank to turn Gooey discovery off.
  agent_example_id: ""
  timeout_minutes: 10
```

`GOOEY_API_URL` and `GOOEY_API_KEY` stay in `~/.kue/.env`, as today. If
`agent_example_id` is set but either secret is missing, `run-once` fails with a clear
message instead of silently skipping discovery.

### Client (`src/kue/gooey/client.py`)

- `GooeyClient(base_url, api_key, *, timeout_minutes, transport=None, poll_seconds=5,
  sleep=asyncio.sleep, clock=time.monotonic)`. `transport` is an
  `httpx.AsyncBaseTransport` so tests can use `httpx.MockTransport`.
- `async run_agent(example_id, input_prompt, messages) -> AgentReply` where
  `AgentReply` holds `run_id`, `web_url`, `text`. `messages` is always sent, `[]` on a
  first call, so the published agent's own saved conversation never leaks in.
- `async balance() -> int`.
- Kue builds the status URL itself from `base_url` and `run_id`. It never follows the
  `status_url` in the response, so the key can't be sent to another host.
- Polls every `poll_seconds` until `completed` or `failed`, or until
  `timeout_minutes` passes.
- Errors are one exception type, `GooeyError(message, web_url=None)`, plus the subclass
  `GooeyConnectAccount(connect_url)` for the Composio case. Messages say what happened
  and what to do: "Gooey rejected the API key. Check GOOEY_API_KEY in ~/.kue/.env.",
  "Gooey is out of credits.", "Gooey rate limit hit: <its text>", "The agent needs a
  connected account. Connect it here: <url>", "The agent run failed: <detail>
  (<web_url>)", "The agent run didn't finish within 10 minutes. It may still finish
  on Gooey: <web_url>". Network errors and timeouts from httpx become `GooeyError` too.
- No retries inside the client. The next hourly cycle is the retry.

### Discovery (`src/kue/gooey/discovery.py`)

**Prompt.** Kue owns the prompt and the reply contract, because they change together
with the parser. The agent's own instructions in Gooey cover how to use its Discord
and Asana tools. The prompt says:

- find engineering work for Anish that appeared or changed since `<since>` (an ISO
  time; on the very first fetch, "the last 24 hours");
- the repositories Kue can work in, by name (enabled repos from config);
- reply with exactly one fenced ```json block shaped like the contract below, with
  `{"tasks": []}` when there's nothing new.

Contract, per task: `title` (required), `description`, `repository` (one of the given
names, or null), `priority` (0–5, the meanings from `tasks/models.py`),
`asana_task_id`, `discord_message_id`.

**Parsing.**

- `extract_json_block(text) -> dict`: takes the **last** fenced block (with or without
  the `json` tag) and parses it. No block, invalid JSON, or a top level that isn't an
  object with a `tasks` list raises `ReplyFormatError` with a short reason.
- Each item is validated on its own, so one bad item doesn't sink the rest. Unknown
  keys are ignored. Ids may arrive as numbers and are turned into strings; they must
  be digits only (Asana gids and Discord snowflakes are both numeric).
- Kue builds `external_id` itself: `asana:<gid>` if there is an Asana id, otherwise
  `discord:<message_id>`. `source` is `"gooey"`. Dedup on `(source, external_id)`
  already exists.
- Identity comes first. When an item carries a valid id, its `external_id` comes from
  that id even if other fields are bad, so a bad item reported again next cycle can't
  create a second task. A missing or out-of-range `priority` falls back to P4 instead
  of sending the item to review.
- An item that still fails validation (no usable title, or not an object), or has
  neither id, becomes a review proposal: the title if there is one (otherwise
  "Unreadable task from Gooey"), the item's JSON as the description, and a
  `review_reason`. Its `external_id` is the id-based one when a valid id exists, and
  `gooey-run:<run_id>:<index>` otherwise.
- Dedup also matches on the same Asana task, or on the same Discord message, within
  the same source. A Discord match never crosses to a task tied to a *different*
  Asana task, because one message often mentions several. When a known task is
  matched, it also learns any Asana or Discord id it didn't have yet (recorded as a
  `TASK_LINKED` event). So: Discord message X alone, then X with Asana A1, is one
  task that now knows A1; X with Asana A2 after that is a second task. Ids already
  set are never overwritten. This lives in `Store.find_existing` / `upsert_proposal`.
  (Revised after the final review: the first version matched on either id and could
  silently merge two Asana tasks that shared a Discord message.)
- A repository name Gooey invents is not special-cased: triage already sends unknown
  repos to review with "Repository 'x' is not in the config."

**Retry once.** If the whole reply raises `ReplyFormatError`, Kue calls the agent a
second time with `messages = [the prompt as user, the bad reply as assistant]` and an
`input_prompt` that names the problem and asks for only the JSON block. If that reply
fails too, the fetch returns a single review proposal: "Gooey's reply couldn't be
read", the raw text and both run links in the description,
`external_id = gooey-run:<second run_id>`. The fetch counts as successful, so the
cursor moves on. That's deliberate: the work in that window reaches Anish as raw text
in the review task, and Kue can never get stuck re-asking about a window the agent
keeps failing on.

**Raw replies.** Every reply is saved to `<database dir>/replies/<run_id>.txt`
(`~/.kue/replies/` by default) and logged with its `web_url`. In a dry run nothing is
saved; only the log line is written, as the orchestrator already does in dry runs.

### Changes to existing code

- `DiscoverySource.fetch(since: datetime | None, *, dry_run: bool) -> list[TaskProposal]`.
- `TaskProposal.review_reason: str | None = None`. When the orchestrator creates a
  task from a proposal with a `review_reason`, it immediately transitions it
  `DISCOVERED → REQUIRES_HUMAN_REVIEW` with that reason. No schema change. Dry-run
  previews show the reason.
- Orchestrator `_discover`: reads the stored cursor, records the fetch start time from
  `self.clock()`, calls `fetch(since, dry_run=…)`, and on success (and only when
  writing) stores the start time as the new cursor. Overlap between runs is safe
  because known tasks only get `last_seen_at` bumped.
- `orchestration/sources.py` gains `configured_sources(config, secrets)` that returns
  `[GooeySource(...)]` when `gooey.agent_example_id` is set, `[]` when it isn't, and
  raises `ConfigError` when it's set but secrets are missing. `run-once` uses it.
- `kue doctor`: when both secrets are set, calls `balance()` and reports "valid, N
  credits" or the error. Reports whether `gooey.agent_example_id` is set. Both checks
  stay non-required.
- Dependency: `httpx>=0.27`.
- Docs: README status and config notes, `project.md` current state and layout,
  `.env.example` comment.

### Dry run

`run-once --dry-run` calls Gooey for real (it costs credits and creates a run on
Gooey's side) but writes nothing in Kue: no rows, no cursor, no reply files, no lock.
Anish didn't object to this default on 2026-10-23; it's one line to change if he
does.

## Testing

All tests run offline with `httpx.MockTransport`; an autouse fixture clears
`GOOEY_*` from the environment and stops `.env` files from loading.

- `extract_json_block`: a table of replies (prose around the block, no `json` tag, two
  blocks, no block, invalid JSON, wrong top-level shape, empty task list).
- Item mapping: Asana id wins over Discord id, numeric ids, non-digit ids, missing
  title, missing both ids, unknown keys ignored.
- Client: submit then poll through `running` to `completed`; `failed` status; each
  error code (401 key, 401 connect-account with link, 402, 429, 500); timeout with a
  fake clock; the key is only ever sent to the configured host.
- Source: one retry, then the review proposal; raw replies saved only when not a dry
  run.
- Orchestrator: cursor stored only after success and only when writing; a review
  proposal lands in `REQUIRES_HUMAN_REVIEW` with its reason; a second identical fetch
  creates nothing new.
- Config: the new section validates; template and root example still match.

## Left to the builder

File layout inside `src/kue/gooey/`, exact prompt wording, log field names, and test
structure, within the rules above.

## Open items for Anish

- The discovery agent's `example_id`. Until it's set, Gooey discovery is off.
- Confirm that agent has only read tools for Discord and Asana.
- Cost and run time per agent call are unknown; check them after the first real runs.
- Gooey's suggested priority is stored as given and sorts the queue, which is the
  literal reading of "an LLM may suggest a priority; it never controls scheduling"
  (the selection rule stays deterministic). Before Phase 4 starts executing tasks,
  decide whether Gooey's P0/P1 should be capped without a human.
