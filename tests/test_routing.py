"""Tests for manifest loading, agent resolution, and the routing-failure backoff.

handle_routing_failure is the whole error path of the dispatcher: everything that goes
wrong anywhere else ends up here. Its two outcomes — park with backoff, or dead-letter —
were both untested, and the arithmetic that separates them (retry_count < max_retries)
decides whether a task comes back or is gone.

The backoff assertions check the DELAY, not just that a timestamp was written. 5m/10m/20m
is the documented contract in the docstring and in the operator runbook; a regression to a
fixed delay writes a perfectly valid `next_retry_at` and would satisfy any
"is it set?" assertion.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

# --- load_manifests ----------------------------------------------------------------


def _manifest(dispatcher, name: str, **kw):
    data = {"agent_type": name, "capabilities": ["build"], "scope": {"hosts": ["forge"]}}
    data.update(kw)
    dispatcher.atomic_write(dispatcher.MANIFEST_DIR / f"{name}.yml", data)


def test_load_manifests_keys_by_agent_type(dispatcher):
    _manifest(dispatcher, "developer")
    _manifest(dispatcher, "security")

    assert sorted(dispatcher.load_manifests()) == ["developer", "security"]


def test_load_manifests_falls_back_to_the_name_field(dispatcher):
    """Older manifests spell it `name`; both spellings are in ~/.claude/manifests today."""
    dispatcher.atomic_write(dispatcher.MANIFEST_DIR / "writer.yml", {"name": "writer"})

    assert "writer" in dispatcher.load_manifests()


def test_load_manifests_skips_the_example_manifest(dispatcher):
    """example-manifest.yml is a template. Loading it invents an agent nothing can launch."""
    _manifest(dispatcher, "example-manifest")
    _manifest(dispatcher, "developer")

    assert list(dispatcher.load_manifests()) == ["developer"]


def test_load_manifests_skips_dotfiles(dispatcher):
    """Editor swap files and backups live alongside the real ones."""
    dispatcher.atomic_write(dispatcher.MANIFEST_DIR / ".hidden.yml", {"agent_type": "ghost"})

    assert dispatcher.load_manifests() == {}


def test_load_manifests_skips_a_manifest_with_no_name(dispatcher):
    dispatcher.atomic_write(dispatcher.MANIFEST_DIR / "nameless.yml", {"capabilities": ["build"]})

    assert dispatcher.load_manifests() == {}


def test_load_manifests_survives_an_unparseable_manifest(dispatcher):
    """One malformed manifest must not cost the fleet every other agent."""
    (dispatcher.MANIFEST_DIR / "broken.yml").write_text("{{{ not yaml")
    _manifest(dispatcher, "developer")

    assert list(dispatcher.load_manifests()) == ["developer"]


def test_load_manifests_returns_empty_when_the_directory_is_empty(dispatcher):
    assert dispatcher.load_manifests() == {}


# --- find_agent --------------------------------------------------------------------


def test_find_agent_matches_capability_and_host(dispatcher):
    manifests = {"developer": {"capabilities": ["build"], "scope": {"hosts": ["forge"]}}}

    assert dispatcher.find_agent({"task_type": "build"}, manifests) == "developer"


def test_find_agent_accepts_the_all_hosts_wildcard(dispatcher):
    manifests = {"writer": {"capabilities": ["docs"], "scope": {"hosts": ["all"]}}}

    assert dispatcher.find_agent({"task_type": "docs"}, manifests) == "writer"


def test_find_agent_prefers_a_host_scoped_match_over_a_bare_capability_match(dispatcher):
    """Two passes on purpose: an agent scoped to this host wins over one that is not.

    Dict order alone would give the first agent declared. This asserts the scope pass
    actually runs first, using an ordering where the wrong answer comes first.
    """
    manifests = {
        "elsewhere": {"capabilities": ["build"], "scope": {"hosts": ["claudebox"]}},
        "developer": {"capabilities": ["build"], "scope": {"hosts": ["forge"]}},
    }

    assert dispatcher.find_agent({"task_type": "build"}, manifests) == "developer"


def test_find_agent_falls_back_to_capability_when_no_host_matches(dispatcher):
    """Second pass. Better to route to an off-host agent than to dead-letter the task."""
    manifests = {"elsewhere": {"capabilities": ["build"], "scope": {"hosts": ["claudebox"]}}}

    assert dispatcher.find_agent({"task_type": "build"}, manifests) == "elsewhere"


def test_find_agent_returns_none_when_nothing_has_the_capability(dispatcher):
    manifests = {"developer": {"capabilities": ["build"], "scope": {"hosts": ["forge"]}}}

    assert dispatcher.find_agent({"task_type": "deploy"}, manifests) is None


def test_find_agent_returns_none_for_an_empty_roster(dispatcher):
    assert dispatcher.find_agent({"task_type": "build"}, {}) is None


def test_find_agent_tolerates_a_manifest_with_no_scope(dispatcher):
    manifests = {"developer": {"capabilities": ["build"]}}

    assert dispatcher.find_agent({"task_type": "build"}, manifests) == "developer"


# --- handle_routing_failure: the backoff path --------------------------------------


@pytest.mark.parametrize(
    ("retry_count", "expected_minutes"),
    [(0, 5), (1, 10), (2, 20)],
)
def test_backoff_doubles_on_each_retry(dispatcher, write_task, retry_count, expected_minutes):
    """5m, 10m, 20m — the documented contract, asserted as a DELAY not as "a value exists"."""
    path, task = write_task(retry_policy={"retry_count": retry_count, "max_retries": 3})
    before = datetime.now(UTC)

    dispatcher.handle_routing_failure(path, task, "no agent")

    on_disk = dispatcher.load_yaml(path)
    delay = datetime.fromisoformat(on_disk["retry_policy"]["next_retry_at"]) - before
    assert expected_minutes * 60 - 5 <= delay.total_seconds() <= expected_minutes * 60 + 5


def test_routing_failure_parks_in_routing_failed_not_submitted(dispatcher, write_task):
    """TQMCP-1/MDISP-1. Resetting to "submitted" re-runs the whole approval pipeline every
    retry, firing spurious tasks.approved events — which is how this was found."""
    path, task = write_task()

    dispatcher.handle_routing_failure(path, task, "no agent")

    assert dispatcher.load_yaml(path)["status"] == "routing-failed"


def test_routing_failure_increments_the_counter_and_records_the_reason(dispatcher, write_task):
    path, task = write_task()

    dispatcher.handle_routing_failure(path, task, "no agent for task_type=deploy")

    policy = dispatcher.load_yaml(path)["retry_policy"]
    assert policy["retry_count"] == 1
    assert policy["last_failure_reason"] == "no agent for task_type=deploy"


def test_routing_failure_appends_to_history(dispatcher, write_task):
    path, task = write_task()

    dispatcher.handle_routing_failure(path, task, "the reason")

    last = dispatcher.load_yaml(path)["history"][-1]
    assert last["status"] == "routing-failed"
    assert last["note"] == "the reason"


def test_routing_failure_emits_a_bus_event_while_retries_remain(dispatcher, write_task, bus):
    path, task = write_task()

    dispatcher.handle_routing_failure(path, task, "reason")

    assert bus[0][0] == "task.routing-failed"
    assert "retry 1/3" in bus[0][1]["summary"]


# --- handle_routing_failure: exhaustion --------------------------------------------


def test_exhausted_retries_dead_letter_the_task(dispatcher, queue, write_task, notifications):
    path, task = write_task(retry_policy={"retry_count": 3, "max_retries": 3})

    dispatcher.handle_routing_failure(path, task, "still no agent")

    assert not path.exists(), "an exhausted task must leave the active queue"
    dead = dispatcher.load_yaml(queue / "dead-letters" / path.name)
    assert dead["status"] == "failed"
    assert dead["failed_reason"]["reason"] == "still no agent"
    assert [r for r, _, _ in notifications] == ["alerts"]


def test_exhausted_retries_emit_task_failed_not_routing_failed(dispatcher, write_task, bus, nats):
    """The distinction an alert keys on: routing-failed comes back, failed does not."""
    path, task = write_task(retry_policy={"retry_count": 3, "max_retries": 3})

    dispatcher.handle_routing_failure(path, task, "reason")

    assert [e for e, _ in bus] == ["task.failed"]
    assert [s for s, _ in nats] == ["tasks.failed"]


def test_exhausted_retries_do_not_schedule_another_attempt(dispatcher, queue, write_task):
    """A next_retry_at on a dead-lettered task would be a resurrection instruction."""
    path, task = write_task(retry_policy={"retry_count": 3, "max_retries": 3})

    dispatcher.handle_routing_failure(path, task, "reason")

    dead = dispatcher.load_yaml(queue / "dead-letters" / path.name)
    assert dead["retry_policy"].get("next_retry_at") is None


def test_a_custom_max_retries_is_respected(dispatcher, write_task):
    """max_retries is per-task. Hardcoding 3 would dead-letter a long-budget task early."""
    path, task = write_task(retry_policy={"retry_count": 3, "max_retries": 5})

    dispatcher.handle_routing_failure(path, task, "reason")

    assert dispatcher.load_yaml(path)["status"] == "routing-failed"


def test_max_retries_zero_dead_letters_immediately(dispatcher, queue, write_task):
    path, task = write_task(retry_policy={"retry_count": 0, "max_retries": 0})

    dispatcher.handle_routing_failure(path, task, "reason")

    assert (queue / "dead-letters" / path.name).is_file()
