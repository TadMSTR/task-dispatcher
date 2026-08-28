"""Tests for the two launch paths: Temporal workflow submission and headless agent launch.

launch_temporal_workflow was 26 of 27 statements uncovered — effectively the whole
function. It shells out to temporal-workflow-start.sh with a workflow_id and a JSON blob
built from task payload content, so its input validation is the only thing between a queue
file and a subprocess argument list. Both validators (the workflow_type allowlist and the
plan_name charset) are asserted here, along with the fact that a rejection returns False
rather than falling through — the caller only checks the return value.

launch_agent_headless is already well covered for the paths that succeed, by
test_dispatcher_headless_chain.py. This file covers only the branches that refuse: unknown
agent, missing project dir, missing bearer token, unusable credentials, and the run-as
launcher being absent. Those five are the difference between a loud routing failure and a
session that starts and silently does nothing.

WHAT IS NOT ASSERTED HERE: that steward actually launches as agent-steward. That is a
property of sudoers and the launcher, not of this function, and asserting it against a
stubbed Popen would be asserting the stub.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def failures(dispatcher, monkeypatch):
    captured: list[str] = []
    monkeypatch.setattr(
        dispatcher, "handle_routing_failure", lambda path, task, reason: captured.append(reason)
    )
    return captured


@pytest.fixture
def runs(dispatcher, monkeypatch):
    """Capture subprocess.run calls and let the test choose the return code."""
    captured: list[dict] = []

    class _Result:
        returncode = 0

    def _fake_run(argv, stdout=None, stderr=None, timeout=None, **kw):
        captured.append({"argv": argv, "timeout": timeout})
        return _Result()

    monkeypatch.setattr(dispatcher.subprocess, "run", _fake_run)
    return captured


_TASK_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _wf_task(**payload):
    p = {"workflow_type": "BuildPipelineWorkflow", "plan_name": "some-plan"}
    p.update(payload)
    return {"id": _TASK_ID, "task_type": "workflow", "payload": p}


# --- launch_temporal_workflow: validation ------------------------------------------


@pytest.mark.parametrize(
    "bad_type", ["EvilWorkflow", "", "buildpipelineworkflow", "BuildPipelineWorkflow "]
)
def test_an_unallowlisted_workflow_type_is_refused(dispatcher, queue, failures, runs, bad_type):
    """SECURITY[control]: workflow_type is allowlisted before it reaches a subprocess."""
    ok = dispatcher.launch_temporal_workflow(queue / "t.yml", _wf_task(workflow_type=bad_type))

    assert ok is False
    assert runs == [], "nothing may be submitted once the type is rejected"
    assert any("Unknown workflow_type" in r for r in failures), failures


@pytest.mark.parametrize("good_type", ["BuildPipelineWorkflow", "BuildPlanWorkflow"])
def test_both_allowlisted_workflow_types_are_accepted(dispatcher, queue, failures, runs, good_type):
    """The allowlist must not have shrunk to one entry."""
    ok = dispatcher.launch_temporal_workflow(queue / "t.yml", _wf_task(workflow_type=good_type))

    assert ok is True, failures
    assert runs[0]["argv"][1] == good_type


@pytest.mark.parametrize(
    "bad_plan",
    ["", "Has-Capitals", "under_score", "-leading-dash", "has/slash", "has space", "dots.here"],
)
def test_an_invalid_plan_name_is_refused(dispatcher, queue, failures, runs, bad_plan):
    """plan_name becomes half the Temporal workflow id and is payload-supplied."""
    ok = dispatcher.launch_temporal_workflow(queue / "t.yml", _wf_task(plan_name=bad_plan))

    assert ok is False
    assert runs == []
    assert any("Invalid plan_name" in r for r in failures), failures


def test_a_missing_plan_name_is_refused(dispatcher, queue, failures, runs):
    task = {"id": _TASK_ID, "payload": {"workflow_type": "BuildPipelineWorkflow"}}

    assert dispatcher.launch_temporal_workflow(queue / "t.yml", task) is False
    assert runs == []


# --- launch_temporal_workflow: what gets submitted ---------------------------------


def test_the_workflow_id_is_the_plan_name_and_the_task_prefix(dispatcher, queue, failures, runs):
    """Deterministic and collision-resistant: Temporal rejects a duplicate workflow id."""
    dispatcher.launch_temporal_workflow(queue / "t.yml", _wf_task(plan_name="my-plan"))

    assert runs[0]["argv"][2] == f"my-plan-{_TASK_ID[:8]}"


def test_a_malformed_task_id_degrades_to_a_literal(dispatcher, queue, failures, runs):
    """TASK_ID_RE. The id reaches a Temporal workflow id and a log filename."""
    task = _wf_task()
    task["id"] = "../../etc/passwd"

    dispatcher.launch_temporal_workflow(queue / "t.yml", task)

    assert runs[0]["argv"][2] == "some-plan-invalid-"
    assert "passwd" not in " ".join(runs[0]["argv"])


def test_the_input_json_carries_the_plan_name_and_the_rest_of_the_payload(
    dispatcher, queue, failures, runs
):
    dispatcher.launch_temporal_workflow(
        queue / "t.yml", _wf_task(plan_name="my-plan", extra="value", count=3)
    )

    payload = json.loads(runs[0]["argv"][3])
    assert payload == {"plan_name": "my-plan", "extra": "value", "count": 3}


def test_the_task_token_is_never_forwarded_into_the_workflow_input(
    dispatcher, queue, failures, runs
):
    """task_token is the completion credential for the activity that dispatched this.

    Forwarding it into the workflow's own input would hand a task the token used to
    complete it — and it lands in a Temporal payload that is retained and inspectable.
    """
    dispatcher.launch_temporal_workflow(queue / "t.yml", _wf_task(task_token="secret-token-value"))

    assert "secret-token-value" not in runs[0]["argv"][3]
    assert "task_token" not in json.loads(runs[0]["argv"][3])


def test_workflow_type_is_not_duplicated_into_the_input(dispatcher, queue, failures, runs):
    """It is already argv[1]. Repeating it in the input is a second place to disagree."""
    dispatcher.launch_temporal_workflow(queue / "t.yml", _wf_task())

    assert "workflow_type" not in json.loads(runs[0]["argv"][3])


def test_the_submission_is_bounded_by_a_timeout(dispatcher, queue, failures, runs):
    """A hung start script must not stall a 2-minute cron tick indefinitely."""
    dispatcher.launch_temporal_workflow(queue / "t.yml", _wf_task())

    assert runs[0]["timeout"] == 30


# --- launch_temporal_workflow: failure modes ---------------------------------------


def test_a_nonzero_exit_from_the_start_script_is_a_routing_failure(
    dispatcher, queue, failures, monkeypatch
):
    class _Result:
        returncode = 3

    monkeypatch.setattr(dispatcher.subprocess, "run", lambda *a, **kw: _Result())

    assert dispatcher.launch_temporal_workflow(queue / "t.yml", _wf_task()) is False
    assert any("exited 3" in r for r in failures), failures


def test_a_timeout_is_a_routing_failure_not_an_exception(dispatcher, queue, failures, monkeypatch):
    """TimeoutExpired must be caught here; uncaught it kills the tick for every other task."""

    def _boom(*a, **kw):
        raise dispatcher.subprocess.TimeoutExpired(cmd="temporal-workflow-start.sh", timeout=30)

    monkeypatch.setattr(dispatcher.subprocess, "run", _boom)

    assert dispatcher.launch_temporal_workflow(queue / "t.yml", _wf_task()) is False
    assert any("timed out" in r for r in failures), failures


# --- launch_agent_headless: the refusal branches -----------------------------------


def test_an_unknown_agent_is_refused(dispatcher, failures, launches):
    """AGENT_PROJECT_DIRS is a closed set; nothing derived from task content enters it."""
    dispatcher.launch_agent_headless({"id": _TASK_ID, "target_agent": "not-an-agent"})

    assert launches == []
    assert any("Unknown agent" in r for r in failures), failures


def test_a_missing_project_dir_is_refused(dispatcher, failures, launches, monkeypatch, tmp_path):
    """An agent in the roster whose project dir was never created on this host."""
    monkeypatch.setitem(
        dispatcher.AGENT_PROJECT_DIRS, "developer", tmp_path / "absent" / "developer"
    )

    dispatcher.launch_agent_headless({"id": _TASK_ID, "target_agent": "developer"})

    assert launches == []
    assert any("Project dir missing" in r for r in failures), failures


def test_a_missing_bearer_token_refuses_the_launch(dispatcher, failures, launches, monkeypatch):
    """SMCP-28. Without it the session starts with no scoped-mcp tools and 401s inside."""
    monkeypatch.setattr(dispatcher, "load_agent_env", lambda a: {})
    monkeypatch.delenv("SCOPED_MCP_BEARER_TOKEN", raising=False)

    dispatcher.launch_agent_headless({"id": _TASK_ID, "target_agent": "developer"})

    assert launches == []
    assert any("SCOPED_MCP_BEARER_TOKEN" in r for r in failures), failures


def test_unusable_credentials_refuse_the_launch_and_alert(
    dispatcher, failures, launches, monkeypatch
):
    """SMCP-29. The alert is the point: otherwise this is silent for every queued task."""
    alerts: list[tuple] = []
    monkeypatch.setattr(dispatcher, "load_agent_env", lambda a: {"SCOPED_MCP_BEARER_TOKEN": "t"})
    monkeypatch.setattr(dispatcher, "anthropic_creds_usable", lambda env: False)
    monkeypatch.setattr(
        dispatcher, "alert_auth_blocked", lambda agent, tid: alerts.append((agent, tid))
    )

    dispatcher.launch_agent_headless({"id": _TASK_ID, "target_agent": "developer"})

    assert launches == []
    assert alerts == [("developer", _TASK_ID)]
    assert any("Anthropic credential" in r for r in failures), failures


def test_a_run_as_agent_with_no_launcher_is_refused(
    dispatcher, failures, launches, monkeypatch, tmp_path
):
    """vikunja#404. An undeployed launcher would otherwise surface as an uncaught
    FileNotFoundError from Popen and take the tick down for every other agent too."""
    monkeypatch.setitem(
        dispatcher.AGENT_RUN_AS, "steward", ("agent-steward", str(tmp_path / "absent-launcher"))
    )

    dispatcher.launch_agent_headless({"id": _TASK_ID, "target_agent": "steward"})

    assert launches == []
    assert any("Launcher missing" in r for r in failures), failures


def test_a_run_as_agent_skips_the_credential_guards(
    dispatcher, failures, launches, monkeypatch, tmp_path
):
    """Deliberate, not an oversight: for a run-as agent the credentials are NOT in
    child_env by design — the launcher sources them as the target user from a file this
    process cannot read. Running the guards here would fail every launch for the one
    agent whose isolation is working correctly."""
    launcher = tmp_path / "run-steward.sh"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    monkeypatch.setitem(dispatcher.AGENT_RUN_AS, "steward", ("agent-steward", str(launcher)))
    monkeypatch.setattr(dispatcher, "load_agent_env", lambda a: pytest.fail("must not be called"))
    monkeypatch.setattr(dispatcher, "anthropic_creds_usable", lambda env: False)

    dispatcher.launch_agent_headless({"id": _TASK_ID, "target_agent": "steward"})

    assert len(launches) == 1, failures
    assert launches[0]["argv"][:4] == ["sudo", "-n", "-u", "agent-steward"]
