# Gooey Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Kue calls one published Gooey agent each cycle and turns its reply into validated task proposals.

**Architecture:** A small async HTTP client (`kue/gooey/client.py`) starts and polls Gooey agent runs. `kue/gooey/discovery.py` owns the prompt, the reply contract, parsing, and `GooeySource`, which implements the existing `DiscoverySource` protocol. The orchestrator gains a cursor and a review path for proposals Kue couldn't read.

**Tech Stack:** Python 3.12, pydantic 2, httpx (async client, `MockTransport` in tests), typer, pytest.

**Spec:** `docs/superpowers/specs/2026-10-23-gooey-discovery-design.md`

## Global Constraints

- `project.md` hard rules apply: secrets only in the environment, `--dry-run` writes nothing, tests never need credentials or network, every failure becomes an event or task state.
- New dependency: `httpx>=0.27`. No others.
- Config stays strict (`extra="forbid"`); `config.example.yaml` and `src/kue/templates/config.example.yaml` must stay byte-identical.
- Agent slug is `agent`. Start: `POST {base}/v3/agent/async/?example_id=…`. Poll: `GET {base}/v3/agent/status/?run_id=…`. Balance: `GET {base}/v1/balance/`. Header: `Authorization: Bearer <key>`.
- Kue never follows `status_url` from a response; it builds every URL from `GOOEY_API_URL`.
- `source` for Gooey proposals is `"gooey"`. `external_id` is `asana:<gid>`, `discord:<message_id>`, `gooey-run:<run_id>:<index>` or `gooey-run:<run_id>`.
- Poll interval 5 s; default timeout 10 min (`gooey.timeout_minutes`).
- Description text Kue builds from raw replies or items is capped at 4000 characters.
- User-facing copy: sentence case, says what happened and what to do, no apologies.

## Review Focus

- `run_id` comes from Gooey and becomes a filename: anything but `[A-Za-z0-9_-]+` must be rejected, never written to disk (Task 2).
- The published agent's saved `messages` could leak into Kue's first call: Kue always sends `messages: []` on the first call (Task 2, Task 4).
- A completed run with an empty or missing `output_text` must be a clear error, not an `IndexError` (Task 2).
- A corrupted cursor value in `discovery_cursor` must be treated as "no cursor" and logged, not crash the cycle (Task 5).
- A very long reply must not become a multi-megabyte task description (Task 3, Task 4).

---

### Task 1: Gooey config section, dependency, and test isolation

**Files:**
- Modify: `src/kue/config/models.py` (add `GooeyConfig`, `AppConfig.gooey`)
- Modify: `src/kue/templates/config.example.yaml`, `config.example.yaml` (identical `gooey:` section, wording from the spec)
- Modify: `pyproject.toml` (add `"httpx>=0.27"` to `dependencies`)
- Modify: `tests/conftest.py` (autouse fixture)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `GooeyConfig(agent_example_id: str | None = None, timeout_minutes: int = 10)`, blank or whitespace `agent_example_id` becomes `None`, `timeout_minutes` `ge=1`. `AppConfig.gooey: GooeyConfig` with a default.

- [ ] **Step 1: Write the failing tests** in `tests/test_config.py`:

```python
def test_gooey_section_defaults_to_off():
    cfg = AppConfig.model_validate({})
    assert cfg.gooey.agent_example_id is None and cfg.gooey.timeout_minutes == 10

def test_gooey_blank_example_id_is_none_and_timeout_is_checked():
    assert AppConfig.model_validate({"gooey": {"agent_example_id": "  "}}).gooey.agent_example_id is None
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"gooey": {"timeout_minutes": 0}})
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"gooey": {"agent": "x"}})
```

Also extend `test_template_is_valid_config` with `assert cfg.gooey.agent_example_id is None`.

- [ ] **Step 2: Run** `pytest tests/test_config.py -v`. Expected: the new tests FAIL (no `gooey` attribute / extra key accepted).
- [ ] **Step 3: Implement `GooeyConfig`** in `models.py` following `RepositoryConfig`'s `_blank_is_none` pattern; add the YAML section to both example files; add `httpx>=0.27` to `pyproject.toml`.
- [ ] **Step 4: Add the autouse fixture** `no_real_secrets(monkeypatch)` to `tests/conftest.py`: `delenv` `GOOEY_API_URL` and `GOOEY_API_KEY` (`raising=False`) and set `kue.config.settings.load_dotenv` to a no-op returning `False`.
- [ ] **Step 5: Run** `pytest -q`. Expected: all pass, including `test_root_example_matches_packaged_template`.
- [ ] **Step 6: Commit** `feat(config): add gooey section and httpx dependency`.

---

### Task 2: Gooey HTTP client

**Files:**
- Create: `src/kue/gooey/__init__.py` (empty), `src/kue/gooey/client.py`
- Modify: `tests/conftest.py` (add `FakeGooey`)
- Test: `tests/test_gooey_client.py`

**Interfaces:**
- Produces:
  - `class ChatMessage(TypedDict): role: Literal["user", "assistant"]; content: str`
  - `@dataclass(frozen=True) class AgentReply: run_id: str; web_url: str; text: str`
  - `class GooeyError(Exception)` with `__init__(self, message: str, web_url: str | None = None)` and attribute `web_url`.
  - `class GooeyConnectAccount(GooeyError)` with attribute `connect_url: str`.
  - `class GooeyClient.__init__(self, base_url: str, api_key: str, *, timeout_minutes: int = 10, poll_seconds: float = 5.0, transport: httpx.AsyncBaseTransport | None = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, clock: Callable[[], float] = time.monotonic)`
  - `async def run_agent(self, example_id: str, input_prompt: str, messages: list[ChatMessage]) -> AgentReply`
  - `async def balance(self) -> int`
- Test helper (conftest): `FakeGooey` with `transport: httpx.MockTransport`, `requests: list[httpx.Request]`, a queue of scripted responses per path (`start`, `status`, `balance`), and `clock`/`sleep` fakes that advance time by the slept amount.

- [ ] **Step 1: Write the failing tests** in `tests/test_gooey_client.py` (all use `FakeGooey`, base URL `https://api.example.test`):
  - `test_run_agent_starts_then_polls_until_completed`: start returns 202 `{"run_id": "r1", "web_url": "https://gooey.example/r1", "status_url": "https://evil.example/status"}`; status returns `running` then `completed` with `output.output_text == ["hello"]`. Assert the reply is `AgentReply("r1", "https://gooey.example/r1", "hello")`; the start request is `POST /v3/agent/async/` with `example_id=ex1` and JSON body `{"input_prompt": "hi", "messages": []}`; every request's host is `api.example.test` and carries `Authorization: Bearer k`.
  - `test_failed_status_raises_with_detail_and_link`: status `{"status": "failed", "detail": "boom", "web_url": …}` → `GooeyError` whose message contains `boom` and the link, and `.web_url` is set.
  - A parametrized `test_error_codes_become_clear_messages` over: start 403 `{"detail": {"error": "Invalid API Key."}}` → message contains `GOOEY_API_KEY`; 402 → contains `out of credits`; 429 `{"detail": {"error": "Try again in 5 minutes."}}` → contains `Try again in 5 minutes.`; 500 → contains `500`.
  - `test_connect_account_error_carries_link`: status returns 401 `{"detail": {"error": "Gooey.AI could not access this resource. … Connect your account to continue: https://connect.example/abc"}}` → `GooeyConnectAccount` with `.connect_url == "https://connect.example/abc"`.
  - `test_timeout_raises_with_link`: `timeout_minutes=1`, status stays `running` → `GooeyError` mentioning `1 minute` and the run link; the fake clock advanced ≥ 60 s.
  - `test_empty_output_text_is_an_error`: completed with `output_text: []` → `GooeyError` mentioning `without a reply`.
  - `test_unsafe_run_id_is_rejected`: start returns `run_id "../x"` → `GooeyError`; no status request was made.
  - `test_network_error_becomes_gooey_error`: transport raises `httpx.ConnectError` → `GooeyError`.
  - `test_balance`: `GET /v1/balance/` returns `{"balance": 1200}` → `1200`.
- [ ] **Step 2: Run** `pytest tests/test_gooey_client.py -v`. Expected: FAIL on import.
- [ ] **Step 3: Implement `client.py`.** One `httpx.AsyncClient(base_url=…, headers=…, transport=…, timeout=30)` per call. A single private `_raise_for(response, web_url)` maps status codes to the messages in the spec; it reads `detail.error` when `detail` is a dict and falls back to a 300-character `str(detail)` otherwise. Connect-account detection: 401 and the text contains `Connect your account`; the link is the last `https?://\S+` in the text. Validate `run_id` against `^[A-Za-z0-9_-]+$`. Loop: `sleep(poll_seconds)`, `GET` status, stop on `completed`/`failed`, raise once `clock()` passes the deadline.
- [ ] **Step 4: Run** `pytest tests/test_gooey_client.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(gooey): add async agent client`.

---

### Task 3: Reply parsing

**Files:**
- Create: `src/kue/gooey/discovery.py` (parsing half)
- Modify: `src/kue/tasks/models.py` (add `TaskProposal.review_reason: str | None = None`)
- Test: `tests/test_gooey_parsing.py`

**Interfaces:**
- Produces:
  - `class ReplyFormatError(ValueError)`
  - `def extract_tasks(text: str) -> list[Any]` (JSON values; typed `Any` because they're unvalidated input)
  - `def proposals_from_items(items: list[Any], *, run_id: str) -> list[TaskProposal]`
  - `TaskProposal.review_reason`

- [ ] **Step 1: Write the failing tests.** A parametrized `test_extract_tasks` table:

| reply | expected |
|---|---|
| prose, then ```` ```json\n{"tasks": [{"title": "a"}]}\n``` ````, then prose | `[{"title": "a"}]` |
| ```` ```\n{"tasks": []}\n``` ```` (no tag) | `[]` |
| two blocks, first `{"tasks": [{"title": "old"}]}`, last `{"tasks": [{"title": "new"}]}` | `[{"title": "new"}]` |
| `{"tasks": []}` with no fence | `ReplyFormatError` "no fenced JSON block" |
| ```` ```json\n{"tasks": [\n``` ```` | `ReplyFormatError` "isn't valid JSON" |
| ```` ```json\n[1, 2]\n``` ```` and ```` ```json\n{"tasks": "x"}\n``` ```` | `ReplyFormatError` "object with a \"tasks\" list" |

And `test_proposals_from_items`:
  - `{"title": "Fix it", "asana_task_id": 1203, "discord_message_id": "99", "repository": "gooey-server", "priority": 1, "extra": "ignored"}` → one proposal, `source == "gooey"`, `external_id == "asana:1203"`, `asana_task_id == "1203"`, no `review_reason`.
  - Discord only → `external_id == "discord:99"`.
  - No ids → review proposal, `external_id == "gooey-run:r1:0"`, `review_reason` mentions `Asana or Discord`.
  - `{"title": "x", "priority": 9, "asana_task_id": "1"}` and `{"title": "x", "priority": "high", "asana_task_id": "1"}` → normal proposals with `priority == 4`.
  - `{"title": "", "asana_task_id": "1"}` → review proposal keyed `asana:1` (identity first).
  - `{"asana_task_id": "abc", "title": "x"}` and the non-dict `"just text"` → review proposals keyed `gooey-run:r1:<index>`; the non-dict one is titled `Unreadable task from Gooey`.
  - An item whose JSON is longer than 4000 characters → review proposal `description` length ≤ 4000.
- [ ] **Step 2: Run** `pytest tests/test_gooey_parsing.py -v`. Expected: FAIL on import.
- [ ] **Step 3: Implement.** Read the ids first: an id is usable if, after converting an int to str and stripping, it matches `^\d{1,32}$`; the external id comes from the usable ids (Asana first). Then a private pydantic `ProposedTask` (`extra="ignore"`): `title` 1–200 chars after stripping, `description` (None → `""`), `repository` (blank → None), `priority` 0–5 where anything missing or invalid becomes 4. Fence regex: ```` r"```[^\n]*\n(.*?)```" ```` with `re.DOTALL`, take the last match. Review reasons for invalid items summarise `ValidationError.errors()` as `field: message` pairs, capped at 300 characters.
- [ ] **Step 4: Run** `pytest tests/test_gooey_parsing.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(gooey): parse agent replies into task proposals`.

---

### Task 4: GooeySource (prompt, one retry, raw replies)

**Files:**
- Modify: `src/kue/gooey/discovery.py` (source half)
- Test: `tests/test_gooey_discovery.py`

**Interfaces:**
- Consumes: `GooeyClient.run_agent`, `AgentReply`, `ChatMessage` (Task 2); `extract_tasks`, `proposals_from_items`, `ReplyFormatError` (Task 3).
- Produces:
  - `def build_prompt(since: datetime | None, repositories: list[str]) -> str`
  - `class GooeySource` with `name = "gooey"`, `__init__(self, client: GooeyClient, example_id: str, repositories: list[str], replies_dir: Path)`, `async def fetch(self, since: datetime | None, *, dry_run: bool) -> list[TaskProposal]`.

- [ ] **Step 1: Write the failing tests** (real `GooeyClient` over `FakeGooey`):
  - `test_build_prompt`: with `since=None` the prompt contains `last 24 hours`; with a datetime it contains its UTC ISO form; it names every repository passed in and contains ```` ```json ````.
  - `test_fetch_returns_proposals_and_saves_reply`: one completed run whose text holds a valid block → proposals returned; `replies_dir / "r1.txt"` contains the raw text; the start body had `"messages": []`.
  - `test_unreadable_reply_is_retried_once_with_history`: first reply `no json here`, second valid → proposals from the second; second start body `messages == [{"role": "user", "content": <first prompt>}, {"role": "assistant", "content": "no json here"}]` and its `input_prompt` mentions the parse problem.
  - `test_unreadable_twice_becomes_one_review_proposal`: both replies unreadable → exactly one proposal, `external_id == "gooey-run:r2"`, `title == "Gooey's reply couldn't be read"`, description contains both run links and is ≤ 4000 characters even when the reply is 10,000 characters, `review_reason` set.
  - `test_dry_run_saves_nothing`: `dry_run=True` → `replies_dir` doesn't exist afterwards.
- [ ] **Step 2: Run** `pytest tests/test_gooey_discovery.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `build_prompt` (priority meanings from `Priority`'s comments in `tasks/models.py`; the contract from the spec) and `GooeySource.fetch` as the spec's "Retry once" and "Raw replies" sections describe. Each reply is logged with `run_id`, `web_url` and its length via `get_logger("gooey")`.
- [ ] **Step 4: Run** `pytest tests/test_gooey_discovery.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(gooey): add GooeySource with one retry and saved replies`.

---

### Task 5: Orchestrator cursor, review path, and wiring

**Files:**
- Modify: `src/kue/orchestration/sources.py` (protocol signature, `configured_sources`)
- Modify: `src/kue/orchestration/orchestrator.py` (`_discover`)
- Modify: `src/kue/database/repositories.py` (`upsert_proposal` also matches on `asana_task_id` / `discord_message_id` within the same source)
- Test: `tests/test_database.py` (`test_upsert_matches_on_either_id_within_source`: a `gooey` task `discord:99` with `discord_message_id="99"`, then a proposal `asana:7` with `asana_task_id="7"`, `discord_message_id="99"` → `created is False`, one task, its `external_id` still `discord:99`; the same ids under source `manual` create a new task)
- Modify: `src/kue/cli/commands.py` (`run_once` uses `configured_sources`)
- Modify: `tests/test_orchestrator.py` (`FakeSource.fetch(since, *, dry_run)` records its arguments)
- Test: `tests/test_orchestrator.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `GooeySource`, `GooeyClient` (Tasks 2 and 4), `GooeyConfig` (Task 1), `Secrets`, `ConfigError` (`config/settings.py`).
- Produces: `DiscoverySource.fetch(self, since: datetime | None, *, dry_run: bool) -> list[TaskProposal]`; `def configured_sources(config: AppConfig, secrets: Secrets) -> list[DiscoverySource]`.

- [ ] **Step 1: Write the failing tests:**
  - `test_cursor_is_passed_and_advanced_only_after_success`: run with a fixed `clock`; first `fetch` gets `since is None`; the stored cursor equals the cycle's clock time; the second cycle's `fetch` gets that time. A failing source leaves the cursor unchanged.
  - `test_dry_run_passes_flag_and_keeps_cursor`: `fetch` saw `dry_run=True`; `get_cursor` is still `(None, None)`.
  - `test_review_proposal_lands_in_review`: a proposal with `review_reason="Gooey's reply couldn't be read"` → task is `REQUIRES_HUMAN_REVIEW` with that `failure_reason`; a second cycle with the same proposal creates nothing and doesn't transition again.
  - `test_corrupt_cursor_is_ignored`: `store.set_cursor("fake", "not-a-date")` → `fetch` gets `since is None`, cycle finishes.
  - `test_configured_sources`: no `agent_example_id` → `[]`; set but secrets missing → `ConfigError` mentioning `GOOEY_API_KEY`; set with both secrets → one source named `gooey`.
  - In `tests/test_cli.py`, `test_run_once_fails_clearly_without_gooey_secrets`: config with `gooey.agent_example_id: "ex1"` → `run-once` exits 2 and prints `GOOEY_API_KEY`.
- [ ] **Step 2: Run** `pytest tests/test_orchestrator.py tests/test_cli.py -v`. Expected: the new tests FAIL; existing tests fail only where `FakeSource.fetch` changed.
- [ ] **Step 3: Implement** as the spec's "Changes to existing code" section says. The cursor time is `self.clock()` taken before `fetch`; it's written with `iso()` only when `write` and the fetch succeeded. An unparseable cursor logs a warning and counts as `None`. Dry-run previews append `, needs review: <reason>` when a proposal has one.
- [ ] **Step 4: Run** `pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `feat(orchestrator): pass discovery cursor and route unreadable proposals to review`.

---

### Task 6: Doctor checks and docs

**Files:**
- Modify: `src/kue/environment/doctor.py`
- Modify: `README.md`, `project.md`, `.env.example`
- Test: `tests/test_cli.py` (doctor still passes with no Gooey secrets)

**Interfaces:**
- Consumes: `GooeyClient.balance`, `GooeyError` (Task 2), `AppConfig.gooey` (Task 1).

- [ ] **Step 1: Write the failing test** `test_doctor_reports_gooey_agent_not_set`: `kue doctor` output contains `Gooey agent` and `agent_example_id`, exit code unchanged from today.
- [ ] **Step 2: Run** `pytest tests/test_cli.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** two non-required checks after "Gooey credentials": `Gooey agent` (set, or "set gooey.agent_example_id in config.yaml"); and, only when both secrets are set, `Gooey API key` via `asyncio.run(GooeyClient(...).balance())` → `valid, <n> credits` or the `GooeyError` message.
- [ ] **Step 4: Update docs.** README: status line to Phase 2, a "Gooey discovery" setup note (set `agent_example_id`; give the agent read-only Discord and Asana tools; `run-once --dry-run` calls Gooey for real and costs credits). `project.md`: current state line and the `gooey/` entry in Layout. `.env.example`: comment says "Used by Gooey discovery".
- [ ] **Step 5: Run** `pytest -q`. Expected: all pass.
- [ ] **Step 6: Commit** `feat(doctor): check Gooey key and agent; document Phase 2`.

---

### Final verification

- [ ] `pytest -q` passes.
- [ ] `kue doctor` with the real `~/.kue` config shows the Gooey checks.
- [ ] `kue run-once --dry-run` with no `agent_example_id` runs and notes there are no discovery sources.
- [ ] One fresh reviewer reads the whole branch diff against the spec.
