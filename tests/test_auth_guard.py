"""Tests for the per-agent env loader, the Anthropic credential preflight, and its alert.

SMCP-28/29. A headless `claude -p` with no usable credential does not fail — it prints
"Not logged in - Please run /login" and exits 0 having never read the task prompt. So the
dispatcher launches a session that looks successful in every log and does nothing. The
preflight exists to turn that into a loud routing failure, and a dead OAuth token hits
every queued task at once, which is what the alert debounce is for.

anthropic_creds_usable is the function whose false-positive is expensive: returning True on
an expired token restores the exact silent failure the guard was added to prevent. Both the
expired and the just-valid sides of `expiresAt > now` are asserted, because a guard that
returns True unconditionally passes any test that only checks the happy path.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

# --- load_agent_env ----------------------------------------------------------------


def test_load_agent_env_returns_empty_when_the_file_is_absent(dispatcher):
    """/opt/appdata/agents/<agent>/.env does not exist for every agent, and must not raise."""
    assert dispatcher.load_agent_env("no-such-agent-xyz") == {}


def _parse(dispatcher, monkeypatch, tmp_path, content: str) -> dict:
    """Drive load_agent_env against a tmp .env by redirecting the path it builds."""
    env = tmp_path / "agent.env"
    env.write_text(content)
    real_path = dispatcher.Path

    def _fake_path(arg):
        if isinstance(arg, str) and arg.startswith("/opt/appdata/agents/"):
            return env
        return real_path(arg)

    monkeypatch.setattr(dispatcher, "Path", _fake_path)
    return dispatcher.load_agent_env("security")


def test_load_agent_env_parses_key_value_lines(dispatcher, monkeypatch, tmp_path):
    got = _parse(dispatcher, monkeypatch, tmp_path, "SCOPED_MCP_BEARER_TOKEN=abc\nOTHER=1\n")

    assert got == {"SCOPED_MCP_BEARER_TOKEN": "abc", "OTHER": "1"}


def test_load_agent_env_strips_quotes_and_whitespace(dispatcher, monkeypatch, tmp_path):
    """run-scoped-mcp-http.sh writes both quoted and bare forms; both must parse the same."""
    got = _parse(dispatcher, monkeypatch, tmp_path, "  A=\"dq\"  \nB='sq'\nC= bare \n")

    assert got == {"A": "dq", "B": "sq", "C": "bare"}


def test_load_agent_env_skips_comments_and_blanks(dispatcher, monkeypatch, tmp_path):
    got = _parse(dispatcher, monkeypatch, tmp_path, "# a comment\n\n   \nA=1\n")

    assert got == {"A": "1"}


def test_load_agent_env_skips_lines_with_no_equals(dispatcher, monkeypatch, tmp_path):
    got = _parse(dispatcher, monkeypatch, tmp_path, "export A\nB=2\n")

    assert got == {"B": "2"}


def test_load_agent_env_keeps_equals_signs_inside_the_value(dispatcher, monkeypatch, tmp_path):
    """partition, not split. Base64 and JWT values contain `=` and would be truncated."""
    got = _parse(dispatcher, monkeypatch, tmp_path, "TOKEN=aGVsbG8=\n")

    assert got == {"TOKEN": "aGVsbG8="}


def test_load_agent_env_ignores_an_empty_key(dispatcher, monkeypatch, tmp_path):
    got = _parse(dispatcher, monkeypatch, tmp_path, "=orphan\nA=1\n")

    assert got == {"A": "1"}


# --- anthropic_creds_usable --------------------------------------------------------


@pytest.mark.parametrize(
    "var", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"]
)
def test_any_of_the_three_env_credentials_is_sufficient(dispatcher, var):
    """SMCP-32. All three short-circuit the same way inside claude; all three count."""
    assert dispatcher.anthropic_creds_usable({var: "sk-whatever"}) is True


def test_an_empty_env_credential_does_not_count(dispatcher):
    """`FOO=` in a .env parses to "" — falsy, and a session started with it cannot auth."""
    assert dispatcher.anthropic_creds_usable({"ANTHROPIC_API_KEY": ""}) is False


def _write_oauth(dispatcher, **oauth):
    dispatcher.OAUTH_CRED_PATH.write_text(json.dumps({"claudeAiOauth": oauth}))


def test_a_valid_oauth_token_is_the_fallback(dispatcher):
    future_ms = (datetime.now(UTC) + timedelta(hours=1)).timestamp() * 1000
    _write_oauth(dispatcher, accessToken="tok", expiresAt=future_ms)

    assert dispatcher.anthropic_creds_usable({}) is True


def test_an_expired_oauth_token_is_unusable(dispatcher):
    """Headless mode does NOT interactively refresh — it prints the login prompt instead.

    That is what makes `expiresAt > now` a correct usability test rather than an
    over-strict one: there are no refreshable-but-expired false positives here.
    """
    past_ms = (datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1000
    _write_oauth(dispatcher, accessToken="tok", expiresAt=past_ms)

    assert dispatcher.anthropic_creds_usable({}) is False


def test_an_oauth_entry_with_no_access_token_is_unusable(dispatcher):
    future_ms = (datetime.now(UTC) + timedelta(hours=1)).timestamp() * 1000
    _write_oauth(dispatcher, expiresAt=future_ms)

    assert dispatcher.anthropic_creds_usable({}) is False


def test_an_oauth_entry_with_no_expiry_is_unusable(dispatcher):
    """Default 0, not "assume valid". An absent expiry is not evidence of a live token."""
    _write_oauth(dispatcher, accessToken="tok")

    assert dispatcher.anthropic_creds_usable({}) is False


def test_a_missing_credentials_file_is_unusable(dispatcher):
    assert not dispatcher.OAUTH_CRED_PATH.exists()

    assert dispatcher.anthropic_creds_usable({}) is False


def test_an_unparseable_credentials_file_is_unusable(dispatcher):
    """Must return False, not raise — this runs inside the per-task dispatch loop."""
    dispatcher.OAUTH_CRED_PATH.write_text("{ not json")

    assert dispatcher.anthropic_creds_usable({}) is False


def test_an_env_credential_wins_without_reading_the_file(dispatcher):
    """The env check short-circuits: a broken credentials file must not veto a live key."""
    dispatcher.OAUTH_CRED_PATH.write_text("{ not json")

    assert dispatcher.anthropic_creds_usable({"ANTHROPIC_API_KEY": "sk-x"}) is True


# --- alert_auth_blocked ------------------------------------------------------------


def test_the_alert_goes_to_sysadmin_and_names_the_blocked_task(dispatcher, notifications):
    dispatcher.alert_auth_blocked("security", "task-123")

    (room, title, body) = notifications[0]
    assert room == "sysadmin"
    assert "[auth]" in title
    assert "task-123" in body
    assert "security" in body


def test_the_alert_writes_a_debounce_stamp(dispatcher, notifications):
    dispatcher.alert_auth_blocked("security", "task-123")

    assert dispatcher.AUTH_ALERT_STAMP.is_file()
    datetime.fromisoformat(dispatcher.AUTH_ALERT_STAMP.read_text().strip())


def test_a_second_alert_inside_the_window_is_suppressed(dispatcher, notifications):
    """A dead OAuth blocks every queued task on the same tick — without this, #sysadmin
    gets one message per task."""
    dispatcher.alert_auth_blocked("security", "task-1")
    dispatcher.alert_auth_blocked("developer", "task-2")
    dispatcher.alert_auth_blocked("writer", "task-3")

    assert len(notifications) == 1


def test_an_alert_after_the_window_fires_again(dispatcher, notifications):
    """The condition is still true 15 minutes later; the operator must be told again."""
    stale = datetime.now(UTC) - timedelta(seconds=dispatcher.AUTH_ALERT_DEBOUNCE_SEC + 60)
    dispatcher.AUTH_ALERT_STAMP.write_text(stale.isoformat())

    dispatcher.alert_auth_blocked("security", "task-1")

    assert len(notifications) == 1


def test_a_corrupt_debounce_stamp_does_not_suppress_the_alert(dispatcher, notifications):
    """Fail open. An unreadable stamp must not silence the alert forever."""
    dispatcher.AUTH_ALERT_STAMP.write_text("not a timestamp")

    dispatcher.alert_auth_blocked("security", "task-1")

    assert len(notifications) == 1


def test_the_debounce_window_is_fifteen_minutes(dispatcher):
    assert dispatcher.AUTH_ALERT_DEBOUNCE_SEC == 900
