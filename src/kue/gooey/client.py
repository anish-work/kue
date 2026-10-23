"""Async client for Gooey's agent API: start a run, poll it, turn failures into clear errors."""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

import httpx

_AGENT = "agent"  # canonical slug of Gooey's agent workflow
_RUN_ID = re.compile(r"[A-Za-z0-9_-]+")  # used with fullmatch: it becomes a filename
_LINK = re.compile(r"https?://\S+")


class ChatMessage(TypedDict):
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class AgentReply:
    run_id: str
    web_url: str
    text: str


class GooeyError(Exception):
    def __init__(self, message: str, web_url: str | None = None) -> None:
        if web_url and web_url not in message:
            message = f"{message} ({web_url})"
        super().__init__(message)
        self.web_url = web_url


class GooeyConnectAccount(GooeyError):
    """The agent needs a Composio account that isn't connected. Not a bad API key."""

    def __init__(self, connect_url: str, web_url: str | None = None) -> None:
        super().__init__(f"The agent needs a connected account. Connect it here: {connect_url}", web_url)
        self.connect_url = connect_url


class GooeyClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_minutes: int = 10,
        poll_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_minutes = timeout_minutes
        self._poll_seconds = poll_seconds
        self._transport = transport
        self._sleep = sleep
        self._clock = clock

    async def run_agent(self, example_id: str, input_prompt: str, messages: list[ChatMessage]) -> AgentReply:
        async with self._http() as http:
            started = await self._request(
                http, "POST", f"/v3/{_AGENT}/async/",
                params={"example_id": example_id},
                json={"input_prompt": input_prompt, "messages": messages},
            )
            run_id, web_url = started.get("run_id"), str(started.get("web_url") or "")
            if not (isinstance(run_id, str) and _RUN_ID.fullmatch(run_id)):
                raise GooeyError(f"Gooey returned an unexpected run id: {str(run_id)[:60]!r}.", web_url)
            # Poll by run_id against our own base URL; never follow the response's status_url.
            deadline = self._clock() + self._timeout_minutes * 60
            while True:
                await self._sleep(self._poll_seconds)
                status = await self._request(
                    http, "GET", f"/v3/{_AGENT}/status/", params={"run_id": run_id},
                    web_url=web_url, run_started=True,
                )
                state = status.get("status")
                if state == "completed":
                    return AgentReply(run_id, web_url, _reply_text(status, web_url))
                if state == "failed":
                    raise GooeyError(f"The agent run failed: {status.get('detail') or 'no details'}", web_url)
                if self._clock() >= deadline:
                    raise GooeyError(
                        f"The agent run didn't finish within {_minutes(self._timeout_minutes)}. "
                        "It may still finish on Gooey",
                        web_url,
                    )

    async def balance(self) -> int:
        async with self._http() as http:
            data = await self._request(http, "GET", "/v1/balance/")
        balance = data.get("balance")
        if not isinstance(balance, int):
            raise GooeyError("Gooey's balance reply didn't include a balance.")
        return balance

    def _http(self) -> httpx.AsyncClient:
        try:
            return httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                transport=self._transport,
                timeout=30.0,
            )
        except httpx.InvalidURL as exc:
            raise GooeyError(f"GOOEY_API_URL isn't a valid URL: {exc}") from exc

    async def _request(
        self,
        http: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        web_url: str | None = None,
        run_started: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            response = await http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise GooeyError(f"Couldn't reach Gooey: {str(exc) or type(exc).__name__}", web_url) from exc
        if response.status_code >= 400:
            raise _error_for(response, web_url, run_started=run_started)
        try:
            data = response.json()
        except ValueError as exc:
            raise GooeyError(f"Gooey sent a reply that isn't JSON (HTTP {response.status_code}).", web_url) from exc
        if not isinstance(data, dict):
            raise GooeyError("Gooey sent a reply Kue doesn't recognise.", web_url)
        return data


def _error_for(response: httpx.Response, web_url: str | None, *, run_started: bool) -> GooeyError:
    code, text = response.status_code, _error_text(response)
    if code == 401 and "connect your account" in text.lower() and (links := _LINK.findall(text)):
        return GooeyConnectAccount(links[-1].rstrip(".,)"), web_url)
    if code in (401, 403) and run_started:
        # The key worked to start the run, so this is the run's own auth failure.
        return GooeyError(f"The agent run failed: {text}", web_url)
    if code in (401, 403):
        return GooeyError(f"Gooey rejected the API key: {text} Check GOOEY_API_KEY in ~/.kue/.env.", web_url)
    if code == 402:
        return GooeyError(f"Gooey needs payment for this run: {text}", web_url)
    if code == 429:
        return GooeyError(f"Gooey rate limit hit: {text}", web_url)
    return GooeyError(f"Gooey returned HTTP {code}: {text}", web_url)


def _error_text(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return response.text[:300] or "no details"
    if isinstance(detail, dict) and detail.get("error"):
        return str(detail["error"])[:1000]
    return str(detail)[:300] if detail else "no details"


def _reply_text(status: dict[str, Any], web_url: str) -> str:
    output = status.get("output")
    texts = output.get("output_text") if isinstance(output, dict) else None
    if not (isinstance(texts, list) and texts and isinstance(texts[0], str) and texts[0].strip()):
        raise GooeyError("The agent run finished without a reply", web_url)
    return texts[0]


def _minutes(n: int) -> str:
    return f"{n} minute" if n == 1 else f"{n} minutes"
