"""Tests for the queue's read/write/move primitives.

Every one of these touches the filesystem, and the filesystem is the dispatcher's only
durable state — a task's status IS the file. So the assertions here read the file back
rather than inspecting the dict that was passed in. A function that mutates the dict and
never writes it passes a dict-based assertion and loses the transition on the next tick.

atomic_write's mode and its tmp-file cleanup are pinned because both are invisible in
normal operation: a queue file readable by other users, or a directory slowly filling with
.tmp turds after a failed write, are conditions nothing else in the suite would report.
"""

from __future__ import annotations

import stat
from datetime import UTC, datetime, timedelta

import pytest
import yaml

# --- atomic_write / load_yaml ------------------------------------------------------


def test_atomic_write_round_trips_through_load_yaml(dispatcher, queue):
    path = queue / "t.yml"
    data = {"id": "abc", "status": "submitted", "payload": {"nested": [1, 2]}}

    dispatcher.atomic_write(path, data)

    assert dispatcher.load_yaml(path) == data


def test_atomic_write_leaves_no_tmp_file_behind(dispatcher, queue):
    path = queue / "t.yml"

    dispatcher.atomic_write(path, {"id": "abc"})

    assert list(queue.glob("*.tmp")) == [], "the tmp file must be renamed, not copied"


def test_atomic_write_creates_the_file_private(dispatcher, queue):
    """0o600. Queue files carry task payloads and this host has other service accounts."""
    path = queue / "t.yml"

    dispatcher.atomic_write(path, {"id": "abc"})

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_atomic_write_cleans_up_its_tmp_file_when_the_write_fails(dispatcher, queue, monkeypatch):
    """A failed write must not leave a partial .tmp — and must not create the destination.

    The failure is injected at yaml.dump rather than built from a value that cannot be
    serialised, because there is no such value here: atomic_write uses `yaml.dump`, whose
    default Dumper represents arbitrary Python objects via `!!python/object:` tags instead
    of raising. An earlier version of this test asserted a YAMLError that never came.

    The error must PROPAGATE, not be swallowed. atomic_write is how a status transition
    becomes durable; a silent failure would leave the task at its old status while every
    caller carried on as though it had been written.
    """
    path = queue / "t.yml"

    def _boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(dispatcher.yaml, "dump", _boom)

    with pytest.raises(OSError, match="disk full"):
        dispatcher.atomic_write(path, {"id": "abc"})

    assert list(queue.glob("*.tmp")) == []
    assert not path.exists(), "a failed write must not create the destination"


def test_atomic_write_replaces_an_existing_file_wholesale(dispatcher, queue):
    """rename() over the top, not a merge. A stale key surviving a write is a stale status."""
    path = queue / "t.yml"
    dispatcher.atomic_write(path, {"id": "abc", "old_key": "gone"})

    dispatcher.atomic_write(path, {"id": "abc"})

    assert dispatcher.load_yaml(path) == {"id": "abc"}


def test_load_yaml_returns_a_dict_for_an_empty_file(dispatcher, queue):
    """`or {}` — an empty queue file must not return None and blow up every .get() call."""
    path = queue / "empty.yml"
    path.write_text("")

    assert dispatcher.load_yaml(path) == {}


def test_atomic_write_preserves_key_order(dispatcher, queue):
    """sort_keys=False. Queue files are read by humans during incidents; id first matters."""
    path = queue / "t.yml"
    data = {"id": "abc", "created": "x", "status": "submitted", "summary": "s"}

    dispatcher.atomic_write(path, data)

    assert list(yaml.safe_load(path.read_text())) == list(data)


def test_atomic_write_keeps_unicode_readable(dispatcher, queue):
    """allow_unicode=True. Summaries carry em-dashes and arrows; \\u2014 escapes are unreadable."""
    path = queue / "t.yml"

    dispatcher.atomic_write(path, {"summary": "build → done — ok"})

    assert "build → done — ok" in path.read_text()


# --- append_history ----------------------------------------------------------------


def test_append_history_creates_the_list_and_stamps_the_entry(dispatcher):
    task = {}

    dispatcher.append_history(task, "approved", "dispatcher", "because")

    (entry,) = task["history"]
    assert entry["status"] == "approved"
    assert entry["actor"] == "dispatcher"
    assert entry["note"] == "because"
    datetime.fromisoformat(entry["timestamp"])  # parses, and is tz-aware
    assert datetime.fromisoformat(entry["timestamp"]).tzinfo is not None


def test_append_history_appends_rather_than_replacing(dispatcher):
    task = {"history": [{"status": "submitted"}]}

    dispatcher.append_history(task, "approved", "dispatcher")

    assert [h["status"] for h in task["history"]] == ["submitted", "approved"]


def test_append_history_omits_an_empty_note(dispatcher):
    """A `note: ''` key in every entry is noise in a file operators read by hand."""
    task = {}

    dispatcher.append_history(task, "approved", "dispatcher")

    assert "note" not in task["history"][0]


# --- is_eligible -------------------------------------------------------------------


def test_is_eligible_true_when_no_retry_policy(dispatcher):
    """The common case: a task that has never failed routing is always eligible."""
    assert dispatcher.is_eligible({}) is True
    assert dispatcher.is_eligible({"retry_policy": {}}) is True


def test_is_eligible_false_before_the_retry_window(dispatcher):
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()

    assert dispatcher.is_eligible({"retry_policy": {"next_retry_at": future}}) is False


def test_is_eligible_true_after_the_retry_window(dispatcher):
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()

    assert dispatcher.is_eligible({"retry_policy": {"next_retry_at": past}}) is True


# --- find_task_by_id ---------------------------------------------------------------


def test_find_task_by_id_searches_the_live_queue(dispatcher, write_task):
    _, task = write_task()

    assert dispatcher.find_task_by_id(task["id"])["id"] == task["id"]


def test_find_task_by_id_also_searches_the_archive(dispatcher, queue):
    """Parent lookup for workflow_mode inheritance: the parent is usually already archived."""
    archive = queue / "archive"
    archive.mkdir()
    dispatcher.atomic_write(archive / "old.yml", {"id": "parent-1", "workflow_mode": "auto"})

    assert dispatcher.find_task_by_id("parent-1")["workflow_mode"] == "auto"


def test_find_task_by_id_returns_none_when_absent(dispatcher, write_task):
    write_task()

    assert dispatcher.find_task_by_id("no-such-id") is None


def test_find_task_by_id_skips_dotfiles_and_unparseable_files(dispatcher, queue, write_task):
    """One corrupt file must not stop the search — the answer may be the next file along."""
    (queue / "broken.yml").write_text("{{{ not yaml")
    (queue / ".hidden.yml").write_text("id: hidden")
    _, task = write_task()

    assert dispatcher.find_task_by_id(task["id"])["id"] == task["id"]
    assert dispatcher.find_task_by_id("hidden") is None


# --- move_to_dead_letter -----------------------------------------------------------


def test_move_to_dead_letter_moves_the_file_and_records_why(
    dispatcher, queue, write_task, notifications
):
    path, task = write_task(status="failed")

    dispatcher.move_to_dead_letter(path, task, "exhausted retries")

    assert not path.exists(), "the task must leave the active queue"
    dead = dispatcher.load_yaml(queue / "dead-letters" / path.name)
    assert dead["failed_reason"]["reason"] == "exhausted retries"
    assert dead["failed_reason"]["retry_count"] == 0
    assert [r for r, _, _ in notifications] == ["alerts"]


def test_move_to_dead_letter_creates_the_directory(dispatcher, queue, write_task):
    path, task = write_task(status="failed")
    assert not (queue / "dead-letters").exists()

    dispatcher.move_to_dead_letter(path, task, "reason")

    assert (queue / "dead-letters" / path.name).is_file()


def test_move_to_dead_letter_carries_the_retry_count(dispatcher, queue, write_task):
    """The count is the evidence for how the task got here; losing it loses the diagnosis."""
    path, task = write_task(status="failed", retry_policy={"retry_count": 3})

    dispatcher.move_to_dead_letter(path, task, "reason")

    assert (
        dispatcher.load_yaml(queue / "dead-letters" / path.name)["failed_reason"]["retry_count"]
        == 3
    )


# --- archive_expired ---------------------------------------------------------------


def _aged(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def test_archive_expired_moves_a_terminal_task_past_its_ttl(dispatcher, queue, write_task):
    path, _ = write_task(status="completed", created=_aged(31), ttl_days=30)

    dispatcher.archive_expired()

    assert not path.exists()
    assert (queue / "archive" / path.name).is_file()


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_archive_expired_covers_every_terminal_status(dispatcher, queue, write_task, status):
    path, _ = write_task(status=status, created=_aged(40), ttl_days=30)

    dispatcher.archive_expired()

    assert (queue / "archive" / path.name).is_file()


@pytest.mark.parametrize("status", ["submitted", "approved", "in-progress", "routing-failed"])
def test_archive_expired_never_touches_a_live_task(dispatcher, queue, write_task, status):
    """An in-progress task older than its TTL is a stuck task, not an archivable one.

    Archiving it would remove the queue entry an agent is still working against.
    """
    path, _ = write_task(status=status, created=_aged(999), ttl_days=1)

    dispatcher.archive_expired()

    assert path.exists()


def test_archive_expired_leaves_a_terminal_task_inside_its_ttl(dispatcher, queue, write_task):
    path, _ = write_task(status="completed", created=_aged(5), ttl_days=30)

    dispatcher.archive_expired()

    assert path.exists()


def test_archive_expired_archives_exactly_at_the_ttl_boundary(dispatcher, queue, write_task):
    """`>=`, not `>`. A ttl_days=30 task archives on day 30, not day 31."""
    path, _ = write_task(status="completed", created=_aged(30), ttl_days=30)

    dispatcher.archive_expired()

    assert (queue / "archive" / path.name).is_file()


def test_archive_expired_defaults_to_thirty_days(dispatcher, queue, write_task):
    path, _ = write_task(status="completed", created=_aged(31))
    task = dispatcher.load_yaml(path)
    del task["ttl_days"]
    dispatcher.atomic_write(path, task)

    dispatcher.archive_expired()

    assert (queue / "archive" / path.name).is_file()


def test_archive_expired_treats_a_naive_timestamp_as_utc(dispatcher, queue, write_task):
    """Older queue files carry naive timestamps; comparing them to an aware `now` raises."""
    naive = (datetime.now(UTC) - timedelta(days=40)).replace(tzinfo=None).isoformat()
    path, _ = write_task(status="completed", created=naive, ttl_days=30)

    dispatcher.archive_expired()

    assert (queue / "archive" / path.name).is_file()


@pytest.mark.parametrize("bad", ["", "not-a-date", None, 12345])
def test_archive_expired_skips_an_unparseable_created_timestamp(dispatcher, queue, write_task, bad):
    """Skip the file, do not crash the tick — archival runs last and would strand the rest."""
    path, _ = write_task(status="completed", created=bad, ttl_days=1)

    dispatcher.archive_expired()

    assert path.exists()


def test_archive_expired_survives_an_unparseable_file(dispatcher, queue, write_task):
    (queue / "broken.yml").write_text("{{{ not yaml")
    path, _ = write_task(status="completed", created=_aged(40), ttl_days=30)

    dispatcher.archive_expired()

    assert (queue / "archive" / path.name).is_file(), "one bad file must not stop archival"


def test_archive_expired_creates_the_archive_directory(dispatcher, queue):
    assert not (queue / "archive").exists()

    dispatcher.archive_expired()

    assert (queue / "archive").is_dir()
