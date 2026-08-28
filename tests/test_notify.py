"""Tests for the Matrix notification path and the NATS publish.

matrix_notify is a two-leg MCP handshake (initialize → capture Mcp-Session-Id → tools/call)
that swallows every exception, by design: a notification failure must never take down a
dispatcher tick. That design is also why it was completely untested — it cannot fail
loudly, so nothing noticed it was unverified. Every assertion here is therefore about what
was SENT or about the absence of a send, never about a raised exception.

_parse_mcp_response is pure and gets the closest scrutiny, because its bug class is silent:
returning the wrong `data:` line yields a well-formed dict that fails no type check and
makes the caller believe a message was delivered. SECURITY[fixed] F-2 from the
dispatcher-auth-and-notify-2026-07 audit is exactly that, and it had no test.
"""

from __future__ import annotations

import json

import httpx
import pytest


def _resp(body: str, status: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers=headers or {},
        content=body.encode(),
        request=httpx.Request("POST", "http://127.0.0.1:8487/mcp"),
    )


# --- _parse_mcp_response -----------------------------------------------------------


def test_parses_a_plain_json_body(dispatcher):
    resp = _resp(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}))

    assert dispatcher._parse_mcp_response(resp, 1)["result"] == {"ok": True}


def test_parses_a_single_sse_data_line(dispatcher):
    resp = _resp('event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"ok":true}}\n\n')

    assert dispatcher._parse_mcp_response(resp, 2)["result"] == {"ok": True}


def test_picks_the_matching_id_from_several_sse_events(dispatcher):
    """SECURITY[fixed] F-2. Taking the FIRST data: line returns the progress notification.

    That is a well-formed dict with no `error` and no `isError`, so the caller concludes
    the message was delivered. This is the assertion that distinguishes the fix from the
    bug — every other test here passes with the first-line implementation.
    """
    body = (
        'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
        'data: {"jsonrpc":"2.0","id":2,"result":{"content":[{"text":"sent"}]}}\n\n'
    )

    got = dispatcher._parse_mcp_response(_resp(body), 2)

    assert got["id"] == 2
    assert got["result"]["content"][0]["text"] == "sent"


def test_falls_back_to_the_last_candidate_when_no_id_matches(dispatcher):
    """Better a late event than the first one — the terminal response is last."""
    body = 'data: {"id":98,"result":"early"}\n\ndata: {"id":99,"result":"late"}\n\n'

    assert dispatcher._parse_mcp_response(_resp(body), 2)["result"] == "late"


def test_ignores_non_data_sse_lines(dispatcher):
    body = 'event: message\nid: 7\nretry: 3000\ndata: {"id":2,"result":"ok"}\n\n'

    assert dispatcher._parse_mcp_response(_resp(body), 2)["result"] == "ok"


def test_tolerates_no_space_after_the_data_prefix(dispatcher):
    assert dispatcher._parse_mcp_response(_resp('data:{"id":2,"result":"ok"}'), 2)["result"] == "ok"


# --- matrix_notify: the fake transport ---------------------------------------------


class _FakeClient:
    """Records posts and replies from a scripted list. Substituted for httpx.Client."""

    def __init__(self, script, calls):
        self._script = list(script)
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        self._calls.append({"url": url, "headers": dict(headers or {}), "json": json})
        return self._script.pop(0)


@pytest.fixture
def matrix(dispatcher, monkeypatch, real_matrix_notify):
    """Drive matrix_notify against a scripted transport. Returns a setup callable.

    matrix_notify is stubbed out by the autouse isolation, so it is restored here — this
    is the one file that tests the real implementation.
    """
    calls: list[dict] = []

    def _setup(*responses):
        monkeypatch.setattr(dispatcher.httpx, "Client", lambda **kw: _FakeClient(responses, calls))
        return calls

    return _setup


_OK_INIT = ('data: {"jsonrpc":"2.0","id":1,"result":{}}', 200, {"mcp-session-id": "sess-1"})
_OK_CALL = ('data: {"jsonrpc":"2.0","id":2,"result":{"content":[]}}', 200, {})


def test_a_successful_notification_sends_both_legs(dispatcher, matrix):
    calls = matrix(_resp(*_OK_INIT), _resp(*_OK_CALL))

    dispatcher.matrix_notify("alerts", "Title", "Body")

    assert len(calls) == 2
    assert calls[0]["json"]["method"] == "initialize"
    assert calls[1]["json"]["method"] == "tools/call"


def test_the_session_id_from_the_handshake_is_replayed_on_the_call(dispatcher, matrix):
    """The whole reason the handshake exists. Without it matrix-mcp answers 400."""
    calls = matrix(_resp(*_OK_INIT), _resp(*_OK_CALL))

    dispatcher.matrix_notify("alerts", "Title", "Body")

    assert calls[1]["headers"]["mcp-session-id"] == "sess-1"


def test_the_message_carries_the_room_title_and_body(dispatcher, matrix):
    calls = matrix(_resp(*_OK_INIT), _resp(*_OK_CALL))

    dispatcher.matrix_notify("sysadmin", "Title", "Body line")

    args = calls[1]["json"]["params"]["arguments"]
    assert args["room_name"] == "sysadmin"
    assert args["message"] == "**Title**\nBody line"


def test_a_failed_handshake_does_not_attempt_the_send(dispatcher, matrix):
    """Posting tools/call without a session is a guaranteed 400 and a misleading log line."""
    calls = matrix(_resp('data: {"id":1,"error":{"code":-32000}}', 200, {"mcp-session-id": "s"}))

    dispatcher.matrix_notify("alerts", "Title", "Body")

    assert len(calls) == 1


def test_a_handshake_with_no_session_header_does_not_send(dispatcher, matrix):
    calls = matrix(_resp('data: {"id":1,"result":{}}', 200, {}))

    dispatcher.matrix_notify("alerts", "Title", "Body")

    assert len(calls) == 1


def test_a_non_2xx_handshake_does_not_send(dispatcher, matrix):
    calls = matrix(_resp('data: {"id":1,"result":{}}', 500, {"mcp-session-id": "s"}))

    dispatcher.matrix_notify("alerts", "Title", "Body")

    assert len(calls) == 1


@pytest.mark.parametrize(
    "call_response",
    [
        ('data: {"id":2,"error":{"code":-32601}}', 200, {}),
        ('data: {"id":2,"result":{"isError":true}}', 200, {}),
        ('data: {"id":2,"result":{}}', 502, {}),
    ],
    ids=["jsonrpc-error", "tool-isError", "http-5xx"],
)
def test_a_failed_send_is_swallowed_rather_than_raised(dispatcher, matrix, call_response):
    """isError is the one that matters: matrix-mcp answers HTTP 200 with a tool-level error.

    A check on status_code alone reports every rejected message as delivered.
    """
    matrix(_resp(*_OK_INIT), _resp(*call_response))

    dispatcher.matrix_notify("alerts", "Title", "Body")  # must not raise


def test_a_transport_exception_never_escapes(dispatcher, monkeypatch, real_matrix_notify):
    """matrix-mcp being down must not take the tick down with it."""

    def _boom(**kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(dispatcher.httpx, "Client", _boom)

    dispatcher.matrix_notify("alerts", "Title", "Body")  # must not raise


# --- publish_nats ------------------------------------------------------------------


def test_publish_nats_shells_out_with_the_subject_and_payload(
    dispatcher, monkeypatch, real_publish_nats
):
    calls: list[list[str]] = []
    monkeypatch.setattr(dispatcher.subprocess, "run", lambda argv, **kw: calls.append(argv) or None)

    dispatcher.publish_nats("tasks.approved", {"task_id": "abc"})

    (argv,) = calls
    assert argv[:2] == ["nats", "pub"]
    assert argv[-2] == "tasks.approved"
    assert json.loads(argv[-1]) == {"task_id": "abc"}


def test_publish_nats_is_bounded_by_a_timeout(dispatcher, monkeypatch, real_publish_nats):
    """Fire-and-forget must never block a 2-minute cron tick on a hung broker."""
    seen: list[dict] = []
    monkeypatch.setattr(dispatcher.subprocess, "run", lambda argv, **kw: seen.append(kw))

    dispatcher.publish_nats("tasks.approved", {})

    assert seen[0]["timeout"] == 5


def test_publish_nats_swallows_a_missing_nats_binary(dispatcher, monkeypatch, real_publish_nats):
    """`nats` is not installed everywhere the dispatcher runs."""

    def _boom(*a, **kw):
        raise FileNotFoundError("nats")

    monkeypatch.setattr(dispatcher.subprocess, "run", _boom)

    dispatcher.publish_nats("tasks.approved", {})  # must not raise
