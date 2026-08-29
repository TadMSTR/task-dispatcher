"""One full main() tick over a seeded queue.

Highest yield per test in the suite, and the only one that would catch a phase-ordering
regression: main() runs process_submitted, then process_routing_failed, then
archive_expired, and the order is load-bearing. Archival runs LAST because it is the only
phase that removes files — running it first would archive a task that this same tick was
about to transition, and every unit test of archive_expired would still pass.

The unit tests above assert each function in isolation. This one asserts they compose:
tasks in six different states go in, and the assertions are on the resulting on-disk state
of the whole queue, not on any single call. That is the shape that catches a task the
dispatcher silently ignores — a status nothing selects for produces no error anywhere, and
a per-function test cannot see it because it never looks at the file that was skipped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


@pytest.fixture
def seeded(dispatcher, monkeypatch, write_task, launchable):
    """A queue with one task in each interesting state, and a matching manifest set."""
    for name, caps in (
        ("developer", ["build", "fix"]),
        ("security", ["audit"]),
        ("writer", ["docs"]),
    ):
        dispatcher.atomic_write(
            dispatcher.MANIFEST_DIR / f"{name}.yml",
            {
                "agent_type": name,
                "capabilities": caps,
                "scope": {"hosts": ["forge"]},
                "max_auto_risk": "medium",
            },
        )
    return write_task


def _aged(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def test_one_tick_moves_every_task_to_its_expected_state(dispatcher, queue, seeded, notifications):
    """Six tasks, six different outcomes, asserted from the files afterwards."""
    auto_low, _ = seeded(status="submitted", risk_level="low", workflow_mode="semi-auto")
    # requires_approval=None, NOT the write_task default of False. An explicit False
    # short-circuits the risk comparison entirely, so seeding a "high risk" task with the
    # default auto-approves it and the assertion below would be testing the override
    # rather than the risk ladder it names.
    needs_approval, _ = seeded(status="submitted", risk_level="high", requires_approval=None)
    notify, _ = seeded(status="submitted", task_type="notify", payload={"description": "done"})
    unroutable, _ = seeded(status="submitted", target_agent="auto", task_type="deploy")
    retry, _ = seeded(status="routing-failed", retry_policy={"retry_count": 1})
    expired, _ = seeded(status="completed", created=_aged(40), ttl_days=30)
    live, _ = seeded(status="in-progress", created=_aged(999), ttl_days=1)

    assert dispatcher.main() == 0

    assert dispatcher.load_yaml(auto_low)["status"] == "approved"
    assert dispatcher.load_yaml(needs_approval)["status"] == "pending-approval"
    assert dispatcher.load_yaml(notify)["status"] == "completed"
    assert dispatcher.load_yaml(unroutable)["status"] == "routing-failed"
    assert dispatcher.load_yaml(retry)["status"] == "approved"
    assert not expired.exists() and (queue / "archive" / expired.name).is_file()
    assert live.exists(), "an in-progress task past its TTL is stuck, not archivable"


def test_archival_runs_after_dispatch_not_before(dispatcher, queue, seeded):
    """Phase ordering, and the only test that can see it.

    A task that this tick transitions to a terminal state must NOT also be archived by the
    same tick — its created date is recent, so it is not eligible. The regression this
    guards is the inverse: archive_expired running FIRST, which would move an expired task
    out from under process_submitted. Seeded with a task that is both terminal-by-this-tick
    and old enough to archive if the phases were swapped.
    """
    notify, _ = seeded(
        status="submitted",
        task_type="notify",
        created=_aged(40),
        ttl_days=30,
        payload={"description": "done"},
    )

    dispatcher.main()

    on_disk = dispatcher.load_yaml(queue / "archive" / notify.name)
    assert on_disk["status"] == "completed", (
        "the task must be dispatched to completed BEFORE being archived — "
        "archived while still 'submitted' means archival ran first"
    )
    assert not notify.exists()


def test_a_tick_over_an_empty_queue_succeeds(dispatcher, seeded):
    """The overwhelmingly common case: 2-minute cron, nothing to do."""
    assert dispatcher.main() == 0


def test_a_tick_survives_an_unparseable_queue_file(dispatcher, queue, seeded):
    """One corrupt file must not strand every other task on the queue."""
    (queue / "broken.yml").write_text("{{{ not yaml")
    good, _ = seeded(status="submitted", risk_level="low")

    assert dispatcher.main() == 0
    assert dispatcher.load_yaml(good)["status"] == "approved"


def test_a_tick_is_idempotent(dispatcher, queue, seeded, notifications):
    """Ticks run every 2 minutes. A second pass over an already-dispatched queue must not
    re-transition anything or re-notify — approved is not a state either phase selects."""
    path, _ = seeded(status="submitted", risk_level="low")
    dispatcher.main()
    after_first = dispatcher.load_yaml(path)
    notifications.clear()

    dispatcher.main()

    after_second = dispatcher.load_yaml(path)
    assert after_second["status"] == "approved"
    assert len(after_second["history"]) == len(after_first["history"])
    assert notifications == []


def test_the_liveness_log_line_is_emitted_verbatim(dispatcher, seeded, caplog):
    """vikunja#479 may key a Loki `absent_over_time` alert on exactly this string.

    Demoting it below INFO or rewording it silences that alert rather than firing it — the
    failure mode is a liveness check that reports healthy forever.
    """
    with caplog.at_level("INFO", logger="task_dispatcher.cli"):
        dispatcher.main()

    assert "=== task-dispatcher run complete ===" in caplog.text


def test_the_per_tick_chatter_stays_below_info(dispatcher, seeded, caplog):
    """These two lines were ~63% of a 20 MB log that nothing rotated."""
    with caplog.at_level("INFO", logger="task_dispatcher.cli"):
        dispatcher.main()

    assert "run start" not in caplog.text
    assert "agent manifests" not in caplog.text


def test_a_submitted_task_is_dispatched_and_an_expired_one_archived_in_one_pass(
    dispatcher, queue, seeded
):
    """Both phases in a single tick — they share the same glob and must not interfere."""
    fresh, _ = seeded(status="submitted", risk_level="low")
    expired, _ = seeded(status="failed", created=_aged(60), ttl_days=30)

    dispatcher.main()

    assert dispatcher.load_yaml(fresh)["status"] == "approved"
    assert (queue / "archive" / expired.name).is_file()
