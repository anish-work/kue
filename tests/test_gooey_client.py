import asyncio

import httpx
import pytest

from kue.gooey.client import AgentReply, GooeyClient, GooeyConnectAccount, GooeyError


def run(coro):
    return asyncio.run(coro)


def test_run_agent_starts_then_polls_until_completed(fake_gooey):
    fake_gooey.started()
    fake_gooey.status("running")
    fake_gooey.completed("hello")
    reply = run(fake_gooey.client().run_agent("ex1", "hi", []))
    assert reply == AgentReply("r1", "https://gooey.example/r1", "hello")

    start = fake_gooey.requests[0]
    assert (start.method, start.url.path, start.url.params["example_id"]) == ("POST", "/v3/agent/async/", "ex1")
    assert fake_gooey.bodies("start") == [{"input_prompt": "hi", "messages": []}]
    assert all(r.url.host == "api.example.test" for r in fake_gooey.requests)
    assert all(r.headers["authorization"] == "Bearer k" for r in fake_gooey.requests)
    assert [r.url.params["run_id"] for r in fake_gooey.requests[1:]] == ["r1", "r1"]


def test_failed_status_raises_with_detail_and_link(fake_gooey):
    fake_gooey.started()
    fake_gooey.status("failed", detail="boom")
    with pytest.raises(GooeyError, match="boom") as exc:
        run(fake_gooey.client().run_agent("ex1", "hi", []))
    assert exc.value.web_url == "https://gooey.example/r1"
    assert "https://gooey.example/r1" in str(exc.value)


@pytest.mark.parametrize("code, body, expected", [
    (403, {"detail": {"error": "Invalid API Key."}}, "GOOEY_API_KEY"),
    (401, {"detail": {"error": "Invalid Authorization header."}}, "Invalid Authorization header."),
    (402, {"detail": {"error": "Doh! You need to purchase additional credits."}}, "purchase additional credits"),
    (402, {"detail": {"error": "This model is only available to paid users."}}, "only available to paid users"),
    (429, {"detail": {"error": "Try again in 5 minutes."}}, "Try again in 5 minutes."),
    (500, {"detail": {"error": "kaboom"}}, "500"),
    (422, {"detail": [{"loc": ["body", "input_prompt"], "msg": "bad"}]}, "422"),
])
def test_error_codes_become_clear_messages(fake_gooey, code, body, expected):
    fake_gooey.queue("start", code, body)
    with pytest.raises(GooeyError, match=expected):
        run(fake_gooey.client().run_agent("ex1", "hi", []))


def test_connect_account_error_carries_link(fake_gooey):
    fake_gooey.started()
    fake_gooey.queue("status", 401, {"detail": {"run_id": "r1", "error": (
        "Gooey.AI could not access this resource. It may be private, require a connected "
        "account, or no longer exist. Connect your account to continue: https://connect.example/abc")}})
    with pytest.raises(GooeyConnectAccount) as exc:
        run(fake_gooey.client().run_agent("ex1", "hi", []))
    assert exc.value.connect_url == "https://connect.example/abc"
    assert "https://connect.example/abc" in str(exc.value)


def test_auth_error_during_a_run_is_not_blamed_on_the_key(fake_gooey):
    fake_gooey.started()
    fake_gooey.queue("status", 401, {"detail": {"error": "Please sign in to OneDrive to continue."}})
    with pytest.raises(GooeyError) as exc:
        run(fake_gooey.client().run_agent("ex1", "hi", []))
    assert "OneDrive" in str(exc.value) and "GOOEY_API_KEY" not in str(exc.value)


def test_timeout_raises_with_link(fake_gooey):
    fake_gooey.started()
    for _ in range(20):
        fake_gooey.status("running")
    with pytest.raises(GooeyError, match="1 minute") as exc:
        run(fake_gooey.client(timeout_minutes=1).run_agent("ex1", "hi", []))
    assert "https://gooey.example/r1" in str(exc.value)
    assert fake_gooey.now >= 60


def test_empty_output_text_is_an_error(fake_gooey):
    fake_gooey.started()
    fake_gooey.status("completed", output={"output_text": []})
    with pytest.raises(GooeyError, match="without a reply"):
        run(fake_gooey.client().run_agent("ex1", "hi", []))


@pytest.mark.parametrize("run_id", ["../x", "r1\n", None, 123])
def test_unsafe_run_id_is_rejected(fake_gooey, run_id):
    fake_gooey.queue("start", 202, {"run_id": run_id, "web_url": "https://gooey.example/x"})
    with pytest.raises(GooeyError):
        run(fake_gooey.client().run_agent("ex1", "hi", []))
    assert len(fake_gooey.requests) == 1


def test_network_error_becomes_gooey_error(fake_gooey):
    fake_gooey.raise_error = httpx.ConnectError("connection refused")
    with pytest.raises(GooeyError, match="connection refused"):
        run(fake_gooey.client().run_agent("ex1", "hi", []))


def test_network_error_without_text_names_the_error(fake_gooey):
    fake_gooey.raise_error = httpx.ReadTimeout("")
    with pytest.raises(GooeyError, match="ReadTimeout"):
        run(fake_gooey.client().run_agent("ex1", "hi", []))


def test_malformed_base_url_is_a_gooey_error():
    with pytest.raises(GooeyError, match="GOOEY_API_URL"):
        run(GooeyClient("http://[::1", "k").balance())


def test_balance(fake_gooey):
    fake_gooey.queue("balance", 200, {"balance": 1200})
    assert run(fake_gooey.client().balance()) == 1200
    assert fake_gooey.requests[0].headers["authorization"] == "Bearer k"
