"""The fixer launcher (audit-fixer-2026-11 p2): launch_kind, launch_fixer and its checks.

Every refusal is tested alone, against a fixture that launches when nothing is broken
(`test_a_valid_fixer_launches` is the control: a guard that refuses everything passes
every refusal test and breaks every pre-audit). A refusal must route to failure with its
reason, approve nothing and launch nothing.

Popen is captured, never executed. The guard's owner uid is set to the test's own uid,
because the suite does not run as root.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

BUILD = "afx-p2-scratch"
BUILD_TASK_ID = "11111111-2222-4333-8444-555555555555"
MARKER = "INJECTION-MARKER-7f3a"


@pytest.fixture
def home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "comms" / "artifacts" / "fixer-handoffs").mkdir(parents=True)
    (home / ".claude" / "comms" / "artifacts" / "build-plans").mkdir(parents=True)
    (home / ".pm2" / "logs").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def guard(dispatcher, monkeypatch, tmp_path):
    d = tmp_path / "sbin-forge"
    d.mkdir()
    d.chmod(0o755)
    g = d / "fixer-precompact-guard.py"
    g.write_text("#!/usr/bin/env python3\n")
    g.chmod(0o755)
    monkeypatch.setattr(dispatcher, "FIXER_GUARD_PATH", g)
    monkeypatch.setattr(dispatcher, "FIXER_GUARD_OWNER_UID", os.getuid())
    return g


@pytest.fixture
def failures(dispatcher, monkeypatch):
    captured: list[str] = []
    monkeypatch.setattr(
        dispatcher, "handle_routing_failure", lambda path, task, reason: captured.append(reason)
    )
    return captured


@pytest.fixture
def approvals(dispatcher, monkeypatch):
    captured: list[str] = []
    monkeypatch.setattr(dispatcher, "approve_and_write", lambda p, t, note: captured.append(note))
    return captured


def write_handoff(home: Path, *, build=BUILD, rnd=1, seq=0, **overrides) -> Path:
    fm = {
        "artifact-type": "handoff",
        "build_name": build,
        "round": rnd,
        "handoff_seq": seq,
        "agent": "developer",
        "build_task": BUILD_TASK_ID,
        "repos": [
            {"name": "task-queue-mcp", "path": "/home/ted/repos/x", "branch": "b"},
            {"name": "host-forge-scripts", "path": "/home/ted/repos/y", "branch": "b"},
        ],
    }
    fm.update(overrides)
    fm = {k: v for k, v in fm.items() if v is not None}
    d = home / ".claude" / "comms" / "artifacts" / "fixer-handoffs" / build
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"r{rnd}-h{seq}.md"
    p.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n# Fixer handoff\n")
    return p


def write_plan(home: Path, repos_line: str | None, *, programme="afx", build=BUILD) -> Path:
    d = home / ".claude" / "comms" / "artifacts" / "build-plans"
    d = d / programme / build if programme else d / build
    d.mkdir(parents=True, exist_ok=True)
    header = "# Build Plan\n\n**Date:** 2026-11-02\n"
    if repos_line is not None:
        header += repos_line + "\n"
    p = d / "plan.md"
    p.write_text(header + "\n## Goal\n\n**Repos:** body/example-not-the-header\n")
    return p


@pytest.fixture
def build_task(write_task):
    return write_task(
        id=BUILD_TASK_ID,
        task_type="build",
        build_name=BUILD,
        target_agent="developer",
        source_agent="research",
        status="in-progress",
    )


@pytest.fixture
def ready(home, guard, build_task, launchable, monkeypatch, dispatcher):
    """Everything a valid fixer needs. Returns the home dir."""
    monkeypatch.setattr(dispatcher, "alert_auth_blocked", lambda *a, **k: None)
    write_handoff(home)
    write_plan(home, "**Repos:** TadMSTR/task-queue-mcp, host-forge/scripts")
    return home


def fixer_task(**kw) -> dict:
    task = {
        "id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        "source_agent": "developer",
        "target_agent": "developer",
        "task_type": "pre_audit",
        "workflow_mode": "semi-auto",
        "status": "submitted",
        "summary": f"Pre-audit fixer: {BUILD} round 1 h0 {MARKER}",
        "build_name": BUILD,
        "audit_round": 1,
        "handoff_seq": 0,
        "payload": {"description": f"do things {MARKER}", "context_refs": []},
    }
    task.update(kw)
    return task


def launch(dispatcher, queue, task=None, **kw):
    return dispatcher.launch_fixer(
        queue / "t.yml", task or fixer_task(), approval_note="Auto-approved: test", **kw
    )


def refused(result, failures, launches, approvals, needle):
    assert result is False
    assert launches == [], "a refused fixer must launch nothing"
    assert approvals == [], "a refused fixer must not be approved"
    assert len(failures) == 1 and needle in failures[0], failures


# --- launch_kind ---------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["semi-auto", "auto", "manual-then-auto"])
def test_launch_kind_is_fixer_whatever_the_mode(dispatcher, mode):
    task = fixer_task(workflow_mode=mode)
    assert dispatcher.launch_kind(task, "developer") == "fixer"
    assert "fixer" in dispatcher.LAUNCHING_KINDS


def test_fixer_agents_and_vocabulary(dispatcher):
    assert dispatcher.FIXER_AGENTS == {"developer", "sysadmin", "fleet"}
    assert "pre_audit" in dispatcher.VALID_TASK_TYPES


# --- the control ---------------------------------------------------------------------


def test_a_valid_fixer_launches(dispatcher, queue, ready, failures, launches, approvals):
    assert launch(dispatcher, queue) is True, failures
    assert approvals == ["Auto-approved: test"]
    [run] = launches
    prompt = run["argv"][-1]
    assert run["argv"][:3] == ["claude", "-p", "--dangerously-skip-permissions"]
    assert prompt == (
        "Run shared-build-fixer. Task ID: aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee. "
        f"Build: {BUILD}. Round: 1. Handoff: r1-h0.md. Repos: verified."
    )
    assert run["cwd"].endswith("/.claude/projects/developer")
    env = run["env"]
    assert env["FORGE_LAUNCH_KIND"] == "fixer"
    assert env["FORGE_TASK_ID"] == "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    assert env["FORGE_WORKFLOW_MODE"] == "semi-auto"
    assert env["FORGE_RUN_ID"]


def test_prompt_and_argv_carry_no_summary_or_description(
    dispatcher, queue, ready, failures, launches, approvals
):
    assert launch(dispatcher, queue) is True, failures
    [run] = launches
    assert not any(MARKER in a for a in run["argv"])
    assert not any(MARKER in str(v) for v in run["env"].values())


def test_run_record_is_written(dispatcher, queue, ready, failures, launches, approvals):
    assert launch(dispatcher, queue) is True, failures
    [record_path] = list(dispatcher.LAUNCH_DIR.glob("*.json"))
    record = yaml.safe_load(record_path.read_text())
    assert record["launched_by"] == "dispatcher-fixer"
    assert record["agent"] == "developer"
    assert record["log_path"].endswith(f"/.pm2/logs/fixer-developer-{BUILD}-r1-h0.log")


def test_manual_then_auto_children_inherit_auto(
    dispatcher, queue, ready, failures, launches, approvals
):
    assert launch(dispatcher, queue, fixer_task(workflow_mode="manual-then-auto")) is True
    assert launches[0]["env"]["FORGE_WORKFLOW_MODE"] == "auto"


def test_plan_without_repos_line_launches_unverified(
    dispatcher, queue, ready, failures, launches, approvals
):
    write_plan(ready, None)
    assert launch(dispatcher, queue) is True, failures
    assert launches[0]["argv"][-1].endswith("Repos: unverified.")


def test_no_plan_at_all_launches_unverified(
    dispatcher, queue, ready, failures, launches, approvals
):
    (write_plan(ready, None)).unlink()
    assert launch(dispatcher, queue) is True, failures
    assert launches[0]["argv"][-1].endswith("Repos: unverified.")


def test_single_plan_build_layout_is_found(dispatcher, queue, ready, failures, launches, approvals):
    write_plan(ready, None).unlink()
    write_plan(ready, "**Repos:** TadMSTR/task-queue-mcp", programme=None)
    assert launch(dispatcher, queue) is True, failures
    assert launches[0]["argv"][-1].endswith("Repos: verified.")


def test_a_relay_launches_with_its_seq(dispatcher, queue, ready, failures, launches, approvals):
    write_handoff(ready, seq=1)
    assert launch(dispatcher, queue, fixer_task(handoff_seq=1)) is True, failures
    assert "Handoff: r1-h1.md." in launches[0]["argv"][-1]


# --- the task's own fields -----------------------------------------------------------


@pytest.mark.parametrize(
    ("override", "needle"),
    [
        ({"task_type": "build"}, "not a pre_audit task"),
        ({"source_agent": "research"}, "not addressed to its submitter"),
        ({"source_agent": "security", "target_agent": "security"}, "not a build agent"),
        ({"source_agent": "steward", "target_agent": "steward"}, "not a build agent"),
        ({"source_agent": "fleet", "target_agent": "fleet"}, "not in the launch roster"),
        ({"id": "not-a-uuid"}, "not a UUID"),
        ({"build_name": None}, "build_name"),
        ({"build_name": "../etc"}, "build_name"),
        ({"build_name": "a.b"}, "build_name"),
        ({"build_name": "x" * 129}, "build_name"),
        ({"audit_round": None}, "audit_round"),
        ({"audit_round": 0}, "audit_round"),
        ({"audit_round": 21}, "audit_round"),
        ({"audit_round": True}, "audit_round"),
        ({"audit_round": "1"}, "audit_round"),
        ({"handoff_seq": None}, "handoff_seq"),
        ({"handoff_seq": 3}, "handoff_seq"),
        ({"handoff_seq": -1}, "handoff_seq"),
        ({"handoff_seq": False}, "handoff_seq"),
        ({"workflow_mode": "yolo"}, "workflow_mode"),
        ({"payload": {"originating_task_id": BUILD_TASK_ID}}, "originating_task_id"),
    ],
)
def test_task_field_refusals(
    dispatcher, queue, ready, failures, launches, approvals, override, needle
):
    result = launch(dispatcher, queue, fixer_task(**override))
    refused(result, failures, launches, approvals, needle)


def test_run_as_agent_is_refused(
    dispatcher, queue, ready, failures, launches, approvals, monkeypatch
):
    policy = dict(dispatcher.LAUNCH_POLICY)
    policy["developer"] = dict(policy["developer"], run_as_user="agent-dev", launcher="/x")
    monkeypatch.setattr(dispatcher, "LAUNCH_POLICY", policy)
    refused(launch(dispatcher, queue), failures, launches, approvals, "run-as agent")


# --- the handoff file ----------------------------------------------------------------


def _handoff_dir(home):
    return home / ".claude" / "comms" / "artifacts" / "fixer-handoffs" / BUILD


def test_missing_handoff_is_refused(dispatcher, queue, ready, failures, launches, approvals):
    (_handoff_dir(ready) / "r1-h0.md").unlink()
    refused(launch(dispatcher, queue), failures, launches, approvals, "does not exist")


def test_symlinked_handoff_is_refused(
    dispatcher, queue, ready, failures, launches, approvals, tmp_path
):
    p = _handoff_dir(ready) / "r1-h0.md"
    real = tmp_path / "elsewhere.md"
    real.write_text(p.read_text())
    p.unlink()
    p.symlink_to(real)
    refused(launch(dispatcher, queue), failures, launches, approvals, "is a symlink")


def test_symlink_inside_the_root_is_refused_too(
    dispatcher, queue, ready, failures, launches, approvals
):
    """A link whose target is ALSO under the root: containment alone would pass it."""
    d = _handoff_dir(ready)
    (d / "r1-h0.md").rename(d / "real.md")
    (d / "r1-h0.md").symlink_to(d / "real.md")
    refused(launch(dispatcher, queue), failures, launches, approvals, "is a symlink")


def test_symlinked_build_dir_is_refused(
    dispatcher, queue, ready, failures, launches, approvals, tmp_path
):
    d = _handoff_dir(ready)
    moved = tmp_path / "moved-build-dir"
    d.rename(moved)
    d.symlink_to(moved, target_is_directory=True)
    refused(launch(dispatcher, queue), failures, launches, approvals, "is a symlink")


def test_fifo_handoff_is_refused_without_blocking(
    dispatcher, queue, ready, failures, launches, approvals
):
    p = _handoff_dir(ready) / "r1-h0.md"
    p.unlink()
    os.mkfifo(p)
    refused(launch(dispatcher, queue), failures, launches, approvals, "not a regular file")


def test_oversize_handoff_is_refused(dispatcher, queue, ready, failures, launches, approvals):
    p = _handoff_dir(ready) / "r1-h0.md"
    p.write_text(p.read_text() + "x" * (512 * 1024))
    refused(launch(dispatcher, queue), failures, launches, approvals, "larger than")


def test_no_front_matter_is_refused(dispatcher, queue, ready, failures, launches, approvals):
    (_handoff_dir(ready) / "r1-h0.md").write_text("# no front matter\n")
    refused(launch(dispatcher, queue), failures, launches, approvals, "no front matter")


def test_unclosed_front_matter_is_refused(dispatcher, queue, ready, failures, launches, approvals):
    (_handoff_dir(ready) / "r1-h0.md").write_text("---\nbuild_name: x\n" + "a: b\n" * 5000)
    refused(launch(dispatcher, queue), failures, launches, approvals, "not closed")


def test_merge_keys_are_refused(dispatcher, queue, ready, failures, launches, approvals):
    (_handoff_dir(ready) / "r1-h0.md").write_text("---\na: &x {k: 1}\nb:\n  <<: *x\n---\n")
    refused(launch(dispatcher, queue), failures, launches, approvals, "does not parse")


def test_a_fifty_line_front_matter_is_read_whole(
    dispatcher, queue, ready, failures, launches, approvals
):
    """Rendered handoffs close their front matter at line 45-50. A 40-line cut would split
    repos: and refuse every real handoff."""
    write_handoff(ready, **{f"pad{i}": i for i in range(40)})
    assert launch(dispatcher, queue) is True, failures


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"build_name": "other-build"}, "build_name"),
        ({"round": 2}, "round"),
        ({"round": "1"}, "round"),
        ({"handoff_seq": 1}, "handoff_seq"),
        ({"handoff_seq": False}, "handoff_seq"),
        ({"agent": "sysadmin"}, "agent"),
        ({"agent": None}, "agent"),
    ],
)
def test_front_matter_mismatch_on_each_field(
    dispatcher, queue, ready, failures, launches, approvals, override, field
):
    write_handoff(ready, **override)
    refused(
        launch(dispatcher, queue), failures, launches, approvals, f"front matter {field} does not"
    )


def test_mismatch_reason_does_not_echo_the_value(
    dispatcher, queue, ready, failures, launches, approvals
):
    write_handoff(ready, agent=MARKER)
    launch(dispatcher, queue)
    assert failures and MARKER not in failures[0]


# --- build_task ------------------------------------------------------------------------


def test_build_task_missing_is_refused(dispatcher, queue, ready, failures, launches, approvals):
    write_handoff(ready, build_task=None)
    refused(launch(dispatcher, queue), failures, launches, approvals, "not a task id")


def test_build_task_not_in_queue_is_refused(
    dispatcher, queue, ready, failures, launches, approvals
):
    write_handoff(ready, build_task="99999999-2222-4333-8444-555555555555")
    refused(launch(dispatcher, queue), failures, launches, approvals, "not in the queue")


def test_build_task_of_another_agent_is_refused(
    dispatcher, queue, ready, failures, launches, approvals, write_task
):
    other = "22222222-2222-4333-8444-555555555555"
    write_task(
        id=other, task_type="build", build_name=BUILD, target_agent="sysadmin", status="in-progress"
    )
    write_handoff(ready, build_task=other)
    refused(launch(dispatcher, queue), failures, launches, approvals, "is not a build task of")


def test_build_task_of_another_build_is_refused(
    dispatcher, queue, ready, failures, launches, approvals, write_task
):
    other = "33333333-2222-4333-8444-555555555555"
    write_task(
        id=other,
        task_type="build",
        build_name="another-build",
        target_agent="developer",
        status="in-progress",
    )
    write_handoff(ready, build_task=other)
    refused(launch(dispatcher, queue), failures, launches, approvals, "is not a build task of")


def test_build_task_of_another_type_is_refused(
    dispatcher, queue, ready, failures, launches, approvals, write_task
):
    other = "44444444-2222-4333-8444-555555555555"
    write_task(
        id=other, task_type="fix", build_name=BUILD, target_agent="developer", status="in-progress"
    )
    write_handoff(ready, build_task=other)
    refused(launch(dispatcher, queue), failures, launches, approvals, "is not a build task of")


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "bogus"])
def test_terminal_build_task_is_refused(
    dispatcher, queue, ready, failures, launches, approvals, write_task, status
):
    other = "55555555-2222-4333-8444-555555555555"
    write_task(
        id=other, task_type="build", build_name=BUILD, target_agent="developer", status=status
    )
    write_handoff(ready, build_task=other)
    refused(launch(dispatcher, queue), failures, launches, approvals, "is not live")


# --- the plan's repo set (R2-04) -------------------------------------------------------


def test_plan_repo_missing_from_handoff_is_refused(
    dispatcher, queue, ready, failures, launches, approvals
):
    write_plan(
        ready, "**Repos:** TadMSTR/task-queue-mcp, host-forge/scripts, agent-platform/skills"
    )
    refused(launch(dispatcher, queue), failures, launches, approvals, "agent-platform/skills")


def test_plan_repo_match_is_case_insensitive_on_owner_repo_name(
    dispatcher, queue, ready, failures, launches, approvals
):
    write_handoff(ready, repos=[{"name": "Task-Queue-MCP"}, {"name": "HOST-FORGE-SCRIPTS"}])
    assert launch(dispatcher, queue) is True, failures


def test_handoff_without_repos_is_refused_when_the_plan_has_some(
    dispatcher, queue, ready, failures, launches, approvals
):
    write_handoff(ready, repos=None)
    refused(launch(dispatcher, queue), failures, launches, approvals, "repos: is not a list")


@pytest.mark.parametrize(
    "line", ["**Repos:**", "**Repos:** not-a-repo", "**Repos:** a/b, ../x/y", "**Repos:** a/b c"]
)
def test_malformed_plan_repos_line_is_refused(
    dispatcher, queue, ready, failures, launches, approvals, line
):
    write_plan(ready, line)
    refused(launch(dispatcher, queue), failures, launches, approvals, "**Repos:** line")


def test_two_plans_for_one_build_is_refused(
    dispatcher, queue, ready, failures, launches, approvals
):
    write_plan(ready, "**Repos:** TadMSTR/task-queue-mcp", programme="other-programme")
    refused(launch(dispatcher, queue), failures, launches, approvals, "more than one plan")


def test_archived_plan_is_not_a_second_match(
    dispatcher, queue, ready, failures, launches, approvals
):
    write_plan(ready, "**Repos:** x/y", programme="archive")
    write_plan(ready, "**Repos:** x/y", programme="_archive")
    assert launch(dispatcher, queue) is True, failures


def test_symlinked_plan_is_refused(
    dispatcher, queue, ready, failures, launches, approvals, tmp_path
):
    p = write_plan(ready, "**Repos:** TadMSTR/task-queue-mcp")
    real = tmp_path / "plan-elsewhere.md"
    real.write_text("**Repos:** TadMSTR/task-queue-mcp\n")
    p.unlink()
    p.symlink_to(real)
    refused(launch(dispatcher, queue), failures, launches, approvals, "is a symlink")


# --- the PreCompact guard --------------------------------------------------------------


def test_missing_guard_is_refused(dispatcher, queue, ready, failures, launches, approvals, guard):
    guard.unlink()
    refused(launch(dispatcher, queue), failures, launches, approvals, "PreCompact guard")


def test_guard_with_wrong_owner_is_refused(
    dispatcher, queue, ready, failures, launches, approvals, monkeypatch
):
    monkeypatch.setattr(dispatcher, "FIXER_GUARD_OWNER_UID", os.getuid() + 1)
    refused(launch(dispatcher, queue), failures, launches, approvals, "not owned by uid")


def test_world_writable_guard_is_refused(
    dispatcher, queue, ready, failures, launches, approvals, guard
):
    guard.chmod(0o757)
    refused(launch(dispatcher, queue), failures, launches, approvals, "world-writable")


def test_group_writable_guard_dir_is_refused(
    dispatcher, queue, ready, failures, launches, approvals, guard
):
    guard.parent.chmod(0o775)
    refused(launch(dispatcher, queue), failures, launches, approvals, "world-writable")


def test_symlinked_guard_is_refused(
    dispatcher, queue, ready, failures, launches, approvals, guard, tmp_path
):
    real = tmp_path / "real-guard.py"
    real.write_text("x")
    guard.unlink()
    guard.symlink_to(real)
    refused(launch(dispatcher, queue), failures, launches, approvals, "not a regular file")


# --- credentials -----------------------------------------------------------------------


def test_missing_bearer_token_is_refused(
    dispatcher, queue, ready, failures, launches, approvals, monkeypatch
):
    monkeypatch.setattr(dispatcher, "load_agent_env", lambda a: {})
    refused(launch(dispatcher, queue), failures, launches, approvals, "SCOPED_MCP_BEARER_TOKEN")


def test_unusable_anthropic_creds_are_refused(
    dispatcher, queue, ready, failures, launches, approvals, monkeypatch
):
    monkeypatch.setattr(dispatcher, "anthropic_creds_usable", lambda env: False)
    refused(launch(dispatcher, queue), failures, launches, approvals, "Anthropic credential")


# --- through the tick ------------------------------------------------------------------


def _queue_fixer(write_task, **kw):
    t = fixer_task(**kw)
    t.pop("id")
    return write_task(id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", **t)


def test_tick_launches_a_submitted_fixer_in_semi_auto(
    dispatcher, ready, launches, write_task, monkeypatch
):
    path, _ = _queue_fixer(write_task)
    dispatcher.process_submitted({})
    assert len(launches) == 1
    assert yaml.safe_load(path.read_text())["status"] == "approved"


def test_tick_routes_a_refused_fixer_to_routing_failed_not_approved(
    dispatcher, ready, launches, write_task
):
    path, _ = _queue_fixer(write_task, source_agent="research")
    dispatcher.process_submitted({})
    assert launches == []
    data = yaml.safe_load(path.read_text())
    assert data["status"] == "routing-failed"
    assert "approved" not in [h["status"] for h in data.get("history", [])]
    assert "Fixer launch refused" in data["retry_policy"]["last_failure_reason"]


def test_per_agent_cap_holds_a_relay_at_submitted(
    dispatcher, ready, launches, write_task, live_pids
):
    """A relay waits until the session that queued it has exited."""
    live_pids.add(777)
    dispatcher.write_run_record(
        run_id="r",
        task_id="bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        agent="developer",
        launched_by="dispatcher-fixer",
        pid=777,
        log_path=Path("/dev/null"),
        workflow_mode="semi-auto",
    )
    write_handoff(ready, seq=1)
    path, _ = _queue_fixer(write_task, handoff_seq=1)
    dispatcher.process_submitted({})
    assert launches == []
    assert yaml.safe_load(path.read_text())["status"] == "submitted"

    live_pids.clear()
    dispatcher.process_submitted({})
    assert len(launches) == 1
    assert "Handoff: r1-h1.md." in launches[0]["argv"][-1]


def test_routing_failed_retry_reaches_launch_fixer(
    dispatcher, ready, launches, write_task, monkeypatch
):
    calls = []
    real = dispatcher.launch_fixer

    def spy(path, task, **kw):
        calls.append(kw)
        return real(path, task, **kw)

    monkeypatch.setattr(dispatcher, "launch_fixer", spy)
    path, _ = _queue_fixer(write_task, status="routing-failed")
    dispatcher.process_routing_failed({})
    assert calls == [{"approval_note": "Routing retry succeeded", "retry": True}]
    assert len(launches) == 1
    assert yaml.safe_load(path.read_text())["status"] == "approved"


def test_routing_failed_retry_respects_the_per_agent_cap(
    dispatcher, ready, launches, write_task, live_pids
):
    live_pids.add(777)
    dispatcher.write_run_record(
        run_id="r",
        task_id="bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        agent="developer",
        launched_by="dispatcher",
        pid=777,
        log_path=Path("/dev/null"),
        workflow_mode="auto",
    )
    path, _ = _queue_fixer(write_task, status="routing-failed")
    dispatcher.process_routing_failed({})
    assert launches == []
    data = yaml.safe_load(path.read_text())
    assert data["status"] == "routing-failed"
    assert data.get("retry_policy", {}).get("retry_count", 0) == 0, "a held retry spends nothing"


def test_routing_failed_refusal_never_falls_through_to_operator_pickup(
    dispatcher, ready, launches, write_task, notifications
):
    path, _ = _queue_fixer(write_task, status="routing-failed", source_agent="research")
    dispatcher.process_routing_failed({})
    assert launches == []
    assert not any("[task ready]" in title for _, title, _ in notifications)
    assert yaml.safe_load(path.read_text())["status"] == "routing-failed"


# --- read_contained / parse_front_matter / find_plan, directly ---------------------------
#
# The branches below are reachable only through a race or a broken host (a component
# swapped between the lstat walk and the open, a file grown after the size check), so
# they are driven by stubbing the one syscall the race would change.


def test_read_contained_missing_root(dispatcher, tmp_path):
    with pytest.raises(dispatcher.FixerRefused, match="is missing"):
        dispatcher.read_contained(tmp_path / "nope", ["a"], 10)


@pytest.mark.parametrize("part", ["", ".", "..", "a/b"])
def test_read_contained_bad_component(dispatcher, tmp_path, part):
    with pytest.raises(dispatcher.FixerRefused, match="bad path component"):
        dispatcher.read_contained(tmp_path, [part, "f"], 10)


def test_read_contained_intermediate_not_a_directory(dispatcher, tmp_path):
    (tmp_path / "f").write_text("x")
    with pytest.raises(dispatcher.FixerRefused, match="not a directory"):
        dispatcher.read_contained(tmp_path, ["f", "g"], 10)


def test_read_contained_link_hidden_from_lstat_is_caught_by_nofollow(
    dispatcher, tmp_path, monkeypatch
):
    """A link the walk didn't see (lstat raced, or lied) is refused by the O_NOFOLLOW open.

    resolve() can't be the backstop here: realpath() walks with lstat too, so a stubbed
    lstat hides the link from both. The open is the check that reads the inode itself.
    """
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("x")
    (root / "f.md").symlink_to(outside)
    monkeypatch.setattr(dispatcher.os, "lstat", os.stat)
    with pytest.raises(dispatcher.FixerRefused, match="could not be opened"):
        dispatcher.read_contained(root, ["f.md"], 10)


def test_read_contained_resolving_outside_the_root_is_refused(dispatcher, tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "f.md").write_text("x")
    real_resolve = Path.resolve

    def resolve(self, strict=False):
        if self.name == "f.md":
            return tmp_path / "elsewhere" / "f.md"
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(dispatcher.FixerRefused, match="is outside"):
        dispatcher.read_contained(root, ["f.md"], 10)


def test_read_contained_swap_between_walk_and_open(dispatcher, tmp_path, monkeypatch):
    (tmp_path / "f").write_text("x")
    real_fstat = os.fstat

    class Other:
        def __init__(self, st):
            self.st_dev, self.st_ino, self.st_mode = st.st_dev, st.st_ino + 1, st.st_mode

    monkeypatch.setattr(dispatcher.os, "fstat", lambda fd: Other(real_fstat(fd)))
    with pytest.raises(dispatcher.FixerRefused, match="changed while being opened"):
        dispatcher.read_contained(tmp_path, ["f"], 10)


def test_read_contained_open_failure(dispatcher, tmp_path, monkeypatch):
    (tmp_path / "f").write_text("x")

    def boom(*a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(dispatcher.os, "open", boom)
    with pytest.raises(dispatcher.FixerRefused, match="could not be opened"):
        dispatcher.read_contained(tmp_path, ["f"], 10)


def test_read_contained_file_grown_after_the_size_check(dispatcher, tmp_path, monkeypatch):
    (tmp_path / "f").write_text("x")
    monkeypatch.setattr(dispatcher.os, "read", lambda fd, n: b"y" * n)
    with pytest.raises(dispatcher.FixerRefused, match="grew past"):
        dispatcher.read_contained(tmp_path, ["f"], 10)


def test_read_contained_reads_the_file(dispatcher, tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").write_bytes(b"hello")
    assert dispatcher.read_contained(tmp_path, ["d", "f"], 5) == b"hello"


def test_front_matter_that_is_not_a_mapping(dispatcher):
    with pytest.raises(dispatcher.FixerRefused, match="not a mapping"):
        dispatcher.parse_front_matter(b"---\n- a\n- b\n---\n")


def test_find_plan_with_no_build_plans_root(dispatcher, home):
    (home / ".claude" / "comms" / "artifacts" / "build-plans").rmdir()
    assert dispatcher.find_plan(BUILD) is None


def test_find_plan_skips_files_and_links_at_the_top(dispatcher, home, tmp_path):
    root = home / ".claude" / "comms" / "artifacts" / "build-plans"
    (root / "loose-file.md").write_text("x")
    elsewhere = tmp_path / "elsewhere-programme"
    (elsewhere / BUILD).mkdir(parents=True)
    (elsewhere / BUILD / "plan.md").write_text("**Repos:** a/b\n")
    (root / "linked-programme").symlink_to(elsewhere, target_is_directory=True)
    assert dispatcher.find_plan(BUILD) is None


def test_project_dir_missing_is_refused(
    dispatcher, queue, ready, failures, launches, approvals, monkeypatch, tmp_path
):
    policy = dict(dispatcher.LAUNCH_POLICY)
    policy["developer"] = dict(policy["developer"], project_dir=tmp_path / "gone")
    monkeypatch.setattr(dispatcher, "LAUNCH_POLICY", policy)
    refused(launch(dispatcher, queue), failures, launches, approvals, "project dir missing")


def test_routing_failed_retry_held_during_auth_outage(
    dispatcher, ready, launches, write_task, monkeypatch
):
    monkeypatch.setattr(dispatcher, "auth_outage_active", lambda: True)
    path, _ = _queue_fixer(write_task, status="routing-failed")
    dispatcher.process_routing_failed({})
    assert launches == []
    assert yaml.safe_load(path.read_text())["status"] == "routing-failed"
