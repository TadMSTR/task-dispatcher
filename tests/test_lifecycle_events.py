"""Lifecycle events the dispatcher owns (operator-panel-2026-09 part 3, v1.5.0).

The rule this release adopts is that the component that writes a status emits the event.
The dispatcher writes some statuses directly to the queue YAML (approved,
pending-approval, routing-failed, failed, dead-lettered, a notify's completed, a Temporal
in-progress), so it emits those. Everything that goes through task-queue-mcp's API, the
stuck-run sweep included, is emitted by task-queue-mcp and must NOT be emitted here as
well, or the coverage check counts it twice.

Every status write carries `from_status` / `to_status`, which is what the coverage check
joins queue history to events on.
"""

from __future__ import annotations

import json

import pytest

from task_dispatcher import cli as _cli

# Captured at import, before the autouse isolate fixture stubs it (conftest's pattern).
_REAL_BUS_LOG = _cli.bus_log


def _events(bus):
    return [e for e, _ in bus]


def _md(bus, event):
    return next(kw["metadata"] for e, kw in bus if e == event)


# ── renames ──────────────────────────────────────────────────────────────────


def test_pending_approval_is_its_own_event_not_dispatched(
    dispatcher, write_task, notifications, bus
):
    path, task = write_task(risk_level="high")
    dispatcher.request_approval(path, task, "high", "low", "risk", "developer")
    assert _events(bus) == ["task.pending-approval"]
    md = _md(bus, "task.pending-approval")
    assert (md["from_status"], md["to_status"]) == ("submitted", "pending-approval")


def test_no_site_emits_the_deprecated_spellings():
    from pathlib import Path

    src = (Path(__file__).parent.parent / "src" / "task_dispatcher" / "cli.py").read_text()
    assert '"task.dispatched"' not in src
    assert '"task.workflow_started"' not in src


# ── approved: one site, every path ───────────────────────────────────────────


def test_approve_and_write_emits_approved_once_with_its_transition(dispatcher, write_task, bus):
    path, task = write_task(status="routing-failed")
    dispatcher.approve_and_write(path, task, "Routing retry succeeded")
    assert _events(bus) == ["task.approved"]
    md = _md(bus, "task.approved")
    assert (md["from_status"], md["to_status"]) == ("routing-failed", "approved")
    assert dispatcher.load_yaml(path)["status"] == "approved"


def test_an_auto_approved_semi_auto_task_emits_approved_exactly_once(dispatcher, write_task, bus):
    write_task(workflow_mode="semi-auto", risk_level="low", requires_approval=False)
    dispatcher.process_submitted({"developer": {"approval": {"max_auto_risk": "low"}}})
    assert _events(bus).count("task.approved") == 1


# ── routing failure and dead letters ─────────────────────────────────────────


def test_routing_failed_carries_its_transition(dispatcher, write_task, bus):
    path, task = write_task(status="approved")
    dispatcher.handle_routing_failure(path, task, "no agent")
    md = _md(bus, "task.routing-failed")
    assert (md["from_status"], md["to_status"]) == ("approved", "routing-failed")


def test_exhaustion_emits_failed_then_dead_lettered_with_the_reason(
    dispatcher, write_task, bus, queue
):
    path, task = write_task(status="routing-failed", retry_policy={"retry_count": 3})
    dispatcher.handle_routing_failure(path, task, "Invalid or missing build_name: 'unknown'")
    assert _events(bus) == ["task.failed", "task.dead-lettered"]
    assert _md(bus, "task.failed")["to_status"] == "failed"
    dl = dict(next(kw for e, kw in bus if e == "task.dead-lettered"))
    assert dl["metadata"]["task_id"] == task["id"]
    assert "build_name" in dl["metadata"]["reason"]
    assert dl["artifact_path"].endswith(f"dead-letters/{path.name}")
    assert (queue / "dead-letters" / path.name).exists()


def test_dead_letter_reason_is_capped(dispatcher, write_task, bus):
    path, task = write_task()
    dispatcher.move_to_dead_letter(path, task, "r" * 1000)
    assert len(_md(bus, "task.dead-lettered")["reason"]) == 300


# ── notify ───────────────────────────────────────────────────────────────────


def test_a_notify_closed_on_the_retry_path_now_emits_completed(dispatcher, write_task, bus):
    """It wrote `completed` and said nothing, before v1.5.0."""
    write_task(task_type="notify", status="routing-failed")
    dispatcher.process_routing_failed({})
    assert _events(bus) == ["task.completed"]
    md = _md(bus, "task.completed")
    assert (md["from_status"], md["to_status"]) == ("routing-failed", "completed")


# ── runs ─────────────────────────────────────────────────────────────────────


def test_writing_a_run_record_emits_run_started(dispatcher, bus, tmp_path):
    rec = dispatcher.write_run_record(
        run_id="run-1",
        task_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        agent="developer",
        launched_by="dispatcher",
        pid=4242,
        log_path=tmp_path / "x.log",
        workflow_mode="auto",
    )
    assert rec is not None
    assert _events(bus) == ["run.started"]
    md = _md(bus, "run.started")
    assert md == {
        "run_id": "run-1",
        "task_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        "agent": "developer",
        "launched_by": "dispatcher",
        "workflow_mode": "auto",
    }


def test_a_record_that_cannot_be_written_emits_nothing(dispatcher, bus, monkeypatch, tmp_path):
    def boom(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(dispatcher, "atomic_write_json", boom)
    rec = dispatcher.write_run_record(
        run_id="r",
        task_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        agent="developer",
        launched_by="dispatcher",
        pid=1,
        log_path=tmp_path / "x.log",
        workflow_mode="auto",
    )
    assert rec is None
    assert bus == []


@pytest.mark.parametrize("reason", ["pid-gone", "max-runtime"])
def test_closing_a_record_emits_run_ended_with_reaped_and_no_exit_code(
    dispatcher, bus, tmp_path, reason
):
    path = tmp_path / "developer-aaaaaaaa.json"
    rec = {"run_id": "run-2", "task_id": "aaaaaaaa-x", "agent": "developer", "exit_code": None}
    dispatcher._close_record(path, rec, reason)
    assert _events(bus) == ["run.ended"]
    md = _md(bus, "run.ended")
    assert md["reaped"] == reason
    assert "exit_code" not in md
    assert json.loads(path.read_text())["reaped"] == reason


# ── the sweep ────────────────────────────────────────────────────────────────


def test_the_sweep_emits_no_task_event_of_its_own(dispatcher, monkeypatch, write_task, bus):
    """task-queue-mcp emits task.failed for the API write; a second one would double-count."""
    monkeypatch.setattr(dispatcher, "control_api_update", lambda *a, **k: True)
    _, t = write_task(target_agent="developer", status="in-progress")
    dispatcher.sweep_dead_runs(
        [{"task_id": t["id"], "run_id": "r", "agent": "developer", "pid": 1}]
    )
    assert not [e for e in _events(bus) if e.startswith("task.")]


# ── metadata ─────────────────────────────────────────────────────────────────


def test_build_name_and_audit_round_ride_along_when_the_task_has_them(dispatcher):
    md = dispatcher.bus_metadata({"id": "x", "build_name": "b", "audit_round": 2})
    assert (md["build_name"], md["audit_round"]) == ("b", 2)


def test_every_status_writing_site_passes_to_status():
    """A source parse, like test_bus_correlation's: covers sites no test drives."""
    import ast
    from pathlib import Path

    src = (Path(__file__).parent.parent / "src" / "task_dispatcher" / "cli.py").read_text()
    task_events = []
    for node in ast.walk(ast.parse(src)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "bus_log"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and str(node.args[0].value).startswith("task.")
            and node.args[0].value != "task.dead-lettered"  # a move, not a status write
        ):
            md = next(kw.value for kw in node.keywords if kw.arg == "metadata")
            names = {k.arg for k in getattr(md, "keywords", [])}
            task_events.append((node.args[0].value, node.lineno, "to_status" in names))
    assert task_events
    missing = [(e, ln) for e, ln, ok in task_events if not ok]
    assert not missing, f"task.* emits without to_status: {missing}"


# ── F-05: an emit failure never changes what the dispatcher does next ────────


@pytest.fixture
def raising_client(dispatcher, monkeypatch):
    calls = []

    def boom(event_type, **kw):
        calls.append(event_type)
        raise OSError("bus log unwritable")

    monkeypatch.setattr(dispatcher, "_bus_client", boom)
    # The isolate fixture stubs bus_log; put the real guard back so this tests it.
    monkeypatch.setattr(dispatcher, "bus_log", _REAL_BUS_LOG)
    return calls


def test_the_guard_swallows_and_logs(dispatcher, raising_client, caplog):
    assert dispatcher.bus_log("task.approved", source="d", summary="s", metadata={}) is None
    assert raising_client == ["task.approved"]
    assert "bus event task.approved not written" in caplog.text


def test_approve_and_write_completes_when_the_bus_raises(dispatcher, write_task, raising_client):
    path, task = write_task(status="submitted")
    dispatcher.approve_and_write(path, task, "auto")
    assert dispatcher.load_yaml(path)["status"] == "approved"
    assert raising_client == ["task.approved"]


def test_dead_letter_alert_still_fires_when_the_bus_raises(
    dispatcher, write_task, raising_client, monkeypatch, queue
):
    """CodeRabbit CR-01 on #7: the dead-letter emit raising used to drop the alert."""
    sent = []
    monkeypatch.setattr(dispatcher, "matrix_notify", lambda room, t, b: sent.append(room))
    path, task = write_task()
    dispatcher.move_to_dead_letter(path, task, "reason")
    assert sent == ["alerts"]
    assert (queue / "dead-letters" / path.name).exists()


def test_closing_a_record_still_returns_it_when_the_bus_raises(
    dispatcher, raising_client, tmp_path
):
    rec = {"run_id": "r", "task_id": "t", "agent": "developer"}
    out = dispatcher._close_record(tmp_path / "developer-t.json", rec, "pid-gone")
    assert out["reaped"] == "pid-gone"
    assert raising_client == ["run.ended"]
