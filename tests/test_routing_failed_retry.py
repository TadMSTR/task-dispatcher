"""Tests for process_routing_failed — the retry pass.

This function existed entirely untested, and it is the one that re-dispatches work that has
already failed once. Its whole reason for being separate from process_submitted is that it
must NOT re-run the approval pipeline: these tasks were already approved, and resetting
them to "submitted" fired a spurious tasks.approved event on every retry (TQMCP-1/MDISP-1).

The property that matters most here is that the retry pass is not a way around the
vocabulary guards. A task with an unknown workflow_mode that was refused on the first tick
comes back five minutes later; if this pass lacked the guard it would fall through the
`== "auto"` test into operator-pickup and quietly reach the state the guard exists to
refuse. A guard only on the first pass is not a guard, so both guards are asserted here
against the retry entry point specifically.

`== "auto"`, not `!= "semi-auto"`, is asserted directly. That rewrite reads like a tidy-up
and is the one edit that would silently auto-launch `manual-then-auto` — destroying the
only property that mode has.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

# Capability-DISTINCT on purpose. An earlier version gave all three agents the same
# capability list, which made every find_agent() call return the first key in the dict —
# so a test asserting that a `docs` task routes to the writer was really asserting dict
# ordering, and passed for a reason that had nothing to do with routing.
MANIFESTS = {
    "developer": {"max_auto_risk": "high", "capabilities": ["build", "fix"]},
    "security": {"max_auto_risk": "high", "capabilities": ["audit"]},
    "writer": {"max_auto_risk": "high", "capabilities": ["docs"]},
}


@pytest.fixture
def creds(dispatcher, monkeypatch):
    monkeypatch.setattr(dispatcher, "load_agent_env", lambda a: {"SCOPED_MCP_BEARER_TOKEN": "t"})
    monkeypatch.setattr(dispatcher, "anthropic_creds_usable", lambda env: True)
    monkeypatch.setattr(dispatcher, "alert_auth_blocked", lambda *a, **k: None)


def _rf(**kw):
    """Defaults for a task sitting in routing-failed and due for a retry."""
    base = {
        "status": "routing-failed",
        "retry_policy": {"retry_count": 1, "max_retries": 3, "next_retry_at": None},
    }
    base.update(kw)
    return base


# --- Eligibility and selection -----------------------------------------------------


def test_a_task_not_yet_due_is_left_alone(dispatcher, write_task, launches):
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    path, _ = write_task(**_rf(retry_policy={"retry_count": 1, "next_retry_at": future}))

    dispatcher.process_routing_failed(MANIFESTS)

    assert dispatcher.load_yaml(path)["status"] == "routing-failed"
    assert launches == []


@pytest.mark.parametrize("status", ["submitted", "approved", "in-progress", "completed"])
def test_only_routing_failed_tasks_are_considered(dispatcher, write_task, launches, status):
    path, _ = write_task(status=status, workflow_mode="auto")

    dispatcher.process_routing_failed(MANIFESTS)

    assert dispatcher.load_yaml(path)["status"] == status
    assert launches == []


def test_dotfiles_and_unparseable_files_are_skipped(dispatcher, queue, write_task):
    (queue / ".hidden.yml").write_text("status: routing-failed")
    (queue / "broken.yml").write_text("{{{ not yaml")
    path, _ = write_task(**_rf())

    dispatcher.process_routing_failed(MANIFESTS)  # must not raise

    assert dispatcher.load_yaml(path)["status"] == "approved"


# --- The vocabulary guards must apply on the retry pass too ------------------------


def test_an_unknown_task_type_is_refused_on_retry(dispatcher, write_task, launches):
    path, _ = write_task(**_rf(task_type="nonsense"))

    dispatcher.process_routing_failed(MANIFESTS)

    on_disk = dispatcher.load_yaml(path)
    assert on_disk["status"] in ("routing-failed", "failed")
    assert "not in" in on_disk["retry_policy"]["last_failure_reason"]
    assert launches == []


def test_an_unknown_workflow_mode_is_refused_on_retry(dispatcher, write_task, launches):
    """The guard-evasion case. Without this, the mode falls through to operator-pickup."""
    path, _ = write_task(**_rf(workflow_mode="turbo"))

    dispatcher.process_routing_failed(MANIFESTS)

    on_disk = dispatcher.load_yaml(path)
    assert on_disk["status"] != "approved", "an unknown mode must not reach approved"
    assert "workflow_mode" in on_disk["retry_policy"]["last_failure_reason"]
    assert launches == []


def test_workflow_is_accepted_even_though_it_is_not_in_the_mcp_vocabulary(
    dispatcher, write_task, monkeypatch
):
    """DISPATCHER_ONLY_TASK_TYPES. Temporal tasks are written to the queue directly by
    temporal-workflow-start.sh, bypassing submit_task, so the unknown-type guard would
    dead-letter every one of them."""
    monkeypatch.setattr(dispatcher, "launch_temporal_workflow", lambda p, t: True)
    path, _ = write_task(
        **_rf(
            task_type="workflow", payload={"plan_name": "p", "workflow_type": "BuildPlanWorkflow"}
        )
    )

    dispatcher.process_routing_failed(MANIFESTS)

    assert dispatcher.load_yaml(path)["status"] == "in-progress"


# --- Self-terminal types -----------------------------------------------------------


def test_a_notify_task_is_closed_not_launched(dispatcher, write_task, launches):
    """vikunja#507. `notify` carries a result, not work — launching a session to read a
    notification is the entire thing that ticket is about."""
    path, _ = write_task(**_rf(task_type="notify", payload={"description": "all done"}))

    dispatcher.process_routing_failed(MANIFESTS)

    on_disk = dispatcher.load_yaml(path)
    assert on_disk["status"] == "completed"
    assert on_disk["result"]["output"] == "all done"
    assert "dispatcher (notify)" in on_disk["result"]["completed_by"]
    assert launches == []


# --- Re-routing --------------------------------------------------------------------


def test_auto_routing_resolves_a_target_and_records_it(dispatcher, write_task):
    path, _ = write_task(**_rf(target_agent="auto", task_type="docs"))

    dispatcher.process_routing_failed(MANIFESTS)

    on_disk = dispatcher.load_yaml(path)
    assert on_disk["target_agent"] == "writer"
    assert on_disk["status"] == "approved"


def test_an_unroutable_task_fails_routing_again(dispatcher, write_task):
    path, _ = write_task(**_rf(target_agent="auto", task_type="deploy"))

    dispatcher.process_routing_failed(MANIFESTS)

    on_disk = dispatcher.load_yaml(path)
    assert on_disk["status"] == "routing-failed"
    assert "routing-failed retry" in on_disk["retry_policy"]["last_failure_reason"]
    assert on_disk["retry_policy"]["retry_count"] == 2


def test_a_retry_that_exhausts_the_budget_dead_letters(dispatcher, queue, write_task):
    path, _ = write_task(
        **_rf(target_agent="auto", task_type="deploy", retry_policy={"retry_count": 3})
    )

    dispatcher.process_routing_failed(MANIFESTS)

    assert not path.exists()
    assert (queue / "dead-letters" / path.name).is_file()


# --- Dispatch: auto vs operator pickup ---------------------------------------------


def test_auto_mode_launches_headlessly(dispatcher, write_task, monkeypatch):
    launched: list[dict] = []
    monkeypatch.setattr(dispatcher, "launch_agent_headless", lambda t: launched.append(t))
    path, _ = write_task(**_rf(workflow_mode="auto"))

    dispatcher.process_routing_failed(MANIFESTS)

    assert len(launched) == 1
    assert dispatcher.load_yaml(path)["status"] == "approved"


@pytest.mark.parametrize("mode", ["semi-auto", "manual-then-auto"])
def test_every_mode_that_is_not_literally_auto_falls_to_operator_pickup(
    dispatcher, write_task, monkeypatch, notifications, mode
):
    """`== "auto"` is load-bearing. Rewriting it as `!= "semi-auto"` reads like a tidy-up
    and would silently auto-launch manual-then-auto, whose only property is that its own
    leg waits for an operator."""
    monkeypatch.setattr(
        dispatcher, "launch_agent_headless", lambda t: pytest.fail(f"{mode} must not launch")
    )
    path, _ = write_task(**_rf(workflow_mode=mode))

    dispatcher.process_routing_failed(MANIFESTS)

    assert dispatcher.load_yaml(path)["status"] == "approved"
    assert [r for r, _, _ in notifications] == ["developer"]


def test_the_operator_notification_carries_the_task_id(dispatcher, write_task, notifications):
    """Without the id the message is not actionable — it is what the operator resumes on."""
    _path, task = write_task(**_rf())

    dispatcher.process_routing_failed(MANIFESTS)

    assert task["id"] in notifications[0][2]


def test_the_retry_records_why_it_transitioned(dispatcher, write_task):
    path, _ = write_task(**_rf())

    dispatcher.process_routing_failed(MANIFESTS)

    assert dispatcher.load_yaml(path)["history"][-1]["note"] == "Routing retry succeeded"


# --- The audit branch on the retry path --------------------------------------------


def test_an_audit_retry_launches_and_is_approved(
    dispatcher, write_task, audit_root, creds, launches
):
    build = audit_root / "retried-build"
    build.mkdir()
    (build / "request.md").write_text("x")
    path, _ = write_task(
        **_rf(
            task_type="audit",
            target_agent="security",
            payload={"request": str(build / "request.md")},
        )
    )

    dispatcher.process_routing_failed(MANIFESTS)

    assert len(launches) == 1
    assert dispatcher.load_yaml(path)["status"] == "approved"


def test_an_audit_retry_with_a_bad_request_path_does_not_reach_approved(
    dispatcher, write_task, audit_root, creds, launches
):
    """The guard must gate the write, not merely precede it. Returning False and falling
    through would mark a task approved for a session that never started."""
    path, _ = write_task(
        **_rf(
            task_type="audit",
            target_agent="security",
            payload={"request": f"{audit_root}/../escape/request.md"},
        )
    )

    dispatcher.process_routing_failed(MANIFESTS)

    assert launches == []
    assert dispatcher.load_yaml(path)["status"] != "approved"


def test_the_audit_retry_prompt_carries_the_task_id(
    dispatcher, write_task, audit_root, creds, launches
):
    """This is the drift the extraction closed — the retry path passed no task id at all,
    leaving the security agent no way to identify which queue entry to claim and close."""
    build = audit_root / "id-check"
    build.mkdir()
    (build / "request.md").write_text("x")
    _path, task = write_task(
        **_rf(
            task_type="audit",
            target_agent="security",
            payload={"request": str(build / "request.md")},
        )
    )

    dispatcher.process_routing_failed(MANIFESTS)

    assert f"Task ID: {task['id']}" in launches[0]["argv"][-1]
