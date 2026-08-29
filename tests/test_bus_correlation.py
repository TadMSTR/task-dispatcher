"""Correlation ids on bus events (build plan agent-workflow-interop-2026-08, Phase 5.2).

WHY THIS EXISTS

Every `bus_log()` call passed `artifact_path` and no `metadata`, so joining a bus event
back to the task it was about meant string-parsing a filename — while the
`publish_nats()` call on the adjacent line was already sending a structured
`{"task_id": ...}`. Two emitters for one event, and only the one with no consumer
carried the id. publish_nats has since been deleted (Phase 5.4), which makes the bus the
*only* emitter and its metadata the only place a correlation id can live.

THE STRUCTURAL TEST IS THE POINT

`test_every_bus_log_call_passes_metadata` parses cli.py and asserts that every call site
passes `metadata`. Per-site behavioural tests below prove the values are right, but they
can only cover sites that exist when they are written — a bus_log added next month would
be missed by all of them and by review, which is exactly how six of these went three
months with no correlation id in the first place. The parse is what makes the property
hold for sites nobody has written yet.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CLI_SOURCE = Path(__file__).resolve().parent.parent / "src" / "task_dispatcher" / "cli.py"


def _bus_log_calls(source: str) -> list[ast.Call]:
    """Every `bus_log(...)` call in the module, excluding its own no-op definition."""
    tree = ast.parse(source)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "bus_log"
    ]


def test_every_bus_log_call_passes_metadata():
    """A new emitter site must not be able to ship without a correlation id.

    Deliberately a source parse rather than a runtime check: a runtime assertion could
    only fire on paths a test actually drives, and the sites here include a Temporal
    branch and a stuck-run sweep that most runs never reach.
    """
    calls = _bus_log_calls(CLI_SOURCE.read_text())

    assert calls, "no bus_log calls found — the extraction is broken, not the code"

    missing = [
        (call.lineno, call.args[0].value if call.args else "?")
        for call in calls
        if not any(kw.arg == "metadata" for kw in call.keywords)
    ]
    assert not missing, (
        f"bus_log call(s) with no metadata= at line(s) {missing}. Every bus event must "
        f"carry its correlation ids — pass metadata=bus_metadata(task)."
    )


def test_the_expected_number_of_emitter_sites_is_pinned():
    """A count, so a DELETED emitter is as visible as an unannotated new one.

    The build plan said six sites; there are seven. `task.dispatched` in
    request_approval was missed when the plan was written, and a check that only looked
    for "sites without metadata" would have reported clean either way.
    """
    assert len(_bus_log_calls(CLI_SOURCE.read_text())) == 7


# ── bus_metadata itself ───────────────────────────────────────────────────────


def test_metadata_carries_the_four_correlation_fields(dispatcher):
    task = {
        "id": "11111111-2222-3333-4444-555555555555",
        "workflow_mode": "auto",
        "risk_level": "high",
    }

    meta = dispatcher.bus_metadata(task, run_id="run-abc")

    assert meta == {
        "task_id": "11111111-2222-3333-4444-555555555555",
        "run_id": "run-abc",
        "workflow_mode": "auto",
        "risk_level": "high",
    }


def test_absent_fields_are_omitted_not_nulled(dispatcher):
    """A consumer must be able to tell "no run" from "the run id failed to resolve"."""
    meta = dispatcher.bus_metadata({"id": "abc"})

    assert meta == {"task_id": "abc"}
    assert "run_id" not in meta


def test_a_task_without_a_run_gets_no_run_id(dispatcher):
    """Only the sweep holds a run record when it logs; everywhere else it is absent."""
    assert "run_id" not in dispatcher.bus_metadata({"id": "abc", "workflow_mode": "auto"})


def test_metadata_of_an_empty_task_is_empty_rather_than_four_nulls(dispatcher):
    assert dispatcher.bus_metadata({}) == {}


# ── the values that actually reach the bus ────────────────────────────────────


def test_routing_failure_event_carries_the_task_id(dispatcher, write_task, bus):
    path, task = write_task(risk_level="medium", workflow_mode="semi-auto")

    dispatcher.handle_routing_failure(path, task, "no manifest match")

    event_type, kwargs = bus[0]
    assert event_type == "task.routing-failed"
    assert kwargs["metadata"]["task_id"] == task["id"]
    assert kwargs["metadata"]["risk_level"] == "medium"
    assert kwargs["metadata"]["workflow_mode"] == "semi-auto"


def test_a_failed_task_event_carries_the_task_id(dispatcher, write_task, bus):
    path, task = write_task(retry_policy={"retry_count": 3, "max_retries": 3})

    dispatcher.handle_routing_failure(path, task, "reason")

    event_type, kwargs = bus[0]
    assert event_type == "task.failed"
    assert kwargs["metadata"]["task_id"] == task["id"]


@pytest.mark.parametrize("field", ["task_id", "workflow_mode", "risk_level"])
def test_the_approval_event_carries_every_field_the_operator_would_filter_on(
    dispatcher, write_task, notifications, bus, field
):
    path, task = write_task(risk_level="high", workflow_mode="manual-then-auto")

    dispatcher.request_approval(path, task, "high", "low", "risk=high", "developer")

    assert field in bus[0][1]["metadata"]
