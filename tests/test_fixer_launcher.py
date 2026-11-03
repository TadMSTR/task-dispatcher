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
import stat
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
    # forge's shape: $HOME not group-writable, ~/.pm2 0700, PM2's own ~/.pm2/logs 0775.
    home.chmod(0o701)
    (home / ".pm2").chmod(0o700)
    (home / ".pm2" / "logs").chmod(0o775)
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
    # The chain from here down to the guard is checked too; /tmp above it is world-writable.
    tmp_path.chmod(0o755)
    monkeypatch.setattr(dispatcher, "FIXER_GUARD_TRUST_ROOT", tmp_path)
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
    refused(launch(dispatcher, queue), failures, launches, approvals, "2 plans for build")
    assert "other-programme" not in failures[0], "programme dir names are agent-written"


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
    with pytest.raises(dispatcher.FixerRefused, match="not a single path component"):
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
    monkeypatch.setattr(dispatcher, "_lstat_at", lambda dir_fd, name: os.stat(name, dir_fd=dir_fd))
    with pytest.raises(dispatcher.FixerRefused, match="could not be opened"):
        dispatcher.read_contained(root, ["f.md"], 10)


def test_read_contained_root_reached_through_a_link_is_refused(dispatcher, tmp_path):
    """Containment is by construction now (an fd chain from /), not by resolve(): a root
    that is itself a link, or sits under one, is refused before anything is read."""
    real = tmp_path / "real"
    real.mkdir()
    (real / "f.md").write_text("x")
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(dispatcher.FixerRefused, match="a component is a link"):
        dispatcher.read_contained(tmp_path / "link", ["f.md"], 10)
    with pytest.raises(dispatcher.FixerRefused, match="a component is a link"):
        dispatcher.read_contained(tmp_path / "link" / ".", ["f.md"], 10)


def test_read_contained_a_root_swapped_after_pinning_is_not_followed(
    dispatcher, tmp_path, monkeypatch
):
    """FW-25: the swap the path-based walk let through. The root is replaced by a link to
    another directory after it was checked; the read must stay on the checked one."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "d").mkdir()
    (root / "d" / "f.md").write_bytes(b"good")
    evil = tmp_path / "evil"
    (evil / "d").mkdir(parents=True)
    (evil / "d" / "f.md").write_bytes(b"evil")
    real_pin = dispatcher._pin_root

    def pin_then_swap(r, what):
        fd = real_pin(r, what)
        root.rename(tmp_path / "moved")
        root.symlink_to(evil)
        return fd

    monkeypatch.setattr(dispatcher, "_pin_root", pin_then_swap)
    assert dispatcher.read_contained(root, ["d", "f.md"], 10) == b"good"


def test_close_errors_do_not_replace_the_result(dispatcher, tmp_path, monkeypatch):
    """OV-59: an os.close failure in cleanup must not replace the read or the refusal."""
    (tmp_path / "f").write_bytes(b"ok")

    _real_close = os.close

    def bad_close(fd):
        _real_close(fd)  # the fd really is closed; only the report is an error
        raise OSError(5, "EIO")

    monkeypatch.setattr(dispatcher.os, "close", bad_close)
    assert dispatcher.read_contained(tmp_path, ["f"], 10) == b"ok"


def test_read_contained_swap_between_walk_and_open(dispatcher, tmp_path, monkeypatch):
    (tmp_path / "f").write_text("x")
    real_fstat = os.fstat

    def swapped(fd):
        # A real stat_result with only st_ino changed, so anything else that calls fstat
        # while the stub is in place (pytest's own cleanup, on 3.12) still gets every field.
        st = real_fstat(fd)
        fields = list(st)
        fields[stat.ST_INO] = st.st_ino + 1
        return os.stat_result(fields)

    with monkeypatch.context() as m:
        m.setattr(dispatcher.os, "fstat", swapped)
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


def test_find_plan_skips_files_at_the_top(dispatcher, home):
    root = home / ".claude" / "comms" / "artifacts" / "build-plans"
    (root / "loose-file.md").write_text("x")
    assert dispatcher.find_plan(BUILD) is None


def test_plan_only_under_a_linked_programme_is_refused_not_unverified(dispatcher, home, tmp_path):
    # Before the fix this read as "no plan", and the fixer launched `Repos: unverified`.
    root = home / ".claude" / "comms" / "artifacts" / "build-plans"
    elsewhere = tmp_path / "elsewhere-programme"
    (elsewhere / BUILD).mkdir(parents=True)
    (elsewhere / BUILD / "plan.md").write_text("**Repos:** a/b\n")
    (root / "linked-programme").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(dispatcher.FixerRefused, match="not searched"):
        dispatcher.find_plan(BUILD)


@pytest.mark.parametrize("skipped", ["_drafts", ".hidden", "archive"])
def test_plan_only_under_a_skipped_programme_is_refused(dispatcher, home, skipped):
    write_plan(home, "**Repos:** a/b", programme=skipped)
    with pytest.raises(dispatcher.FixerRefused, match="not searched"):
        dispatcher.find_plan(BUILD)


def test_unreadable_build_plans_is_refused_not_unverified(dispatcher, home, monkeypatch):
    def boom(path):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(dispatcher.os, "scandir", boom)
    with pytest.raises(dispatcher.FixerRefused, match="unreadable"):
        dispatcher.find_plan(BUILD)


def test_unreadable_plan_entry_is_refused(dispatcher, home, monkeypatch):
    write_plan(home, "**Repos:** a/b")
    real = dispatcher.os.stat

    def fd_stat(p, *a, **k):
        # find_plan probes candidates relative to the pinned build-plans fd (FW-25).
        if str(p).endswith("plan.md") and k.get("dir_fd") is not None:
            raise PermissionError(13, "denied")
        return real(p, *a, **k)

    monkeypatch.setattr(dispatcher.os, "stat", fd_stat)
    with pytest.raises(dispatcher.FixerRefused, match="unreadable"):
        dispatcher.find_plan(BUILD)


def test_flooded_build_plans_is_refused(dispatcher, home, monkeypatch):
    root = home / ".claude" / "comms" / "artifacts" / "build-plans"
    monkeypatch.setattr(dispatcher, "FIXER_PLAN_DIRS_MAX", 5)
    for i in range(6):
        (root / f"p{i}").mkdir()
    with pytest.raises(dispatcher.FixerRefused, match="more than 5 entries"):
        dispatcher.find_plan(BUILD)


@pytest.mark.parametrize("which", ["comms", "artifacts", "build-plans", "fixer-handoffs"])
def test_linked_root_or_ancestor_is_refused(
    dispatcher, queue, ready, failures, launches, approvals, tmp_path, which
):
    # FW-25: a link swapped in for the root (or a directory above it, under $HOME) used to
    # be followed, and the containment check passed against the link's target.
    base = ready / ".claude" / "comms"
    target = {"comms": base, "artifacts": base / "artifacts"}.get(which, base / "artifacts" / which)
    moved = tmp_path / f"moved-{which}"
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=True)
    refused(launch(dispatcher, queue), failures, launches, approvals, "not a directory")


def test_two_repos_lines_in_the_plan_header_are_refused(
    dispatcher, queue, ready, failures, launches, approvals
):
    write_plan(
        ready, "**Repos:** TadMSTR/task-queue-mcp, host-forge/scripts\n**Repos:** x/hidden-repo"
    )
    refused(launch(dispatcher, queue), failures, launches, approvals, "2 **Repos:** lines")


@pytest.mark.parametrize("bad", ["<b>x</b>/y", "a/b\nc", "x" * 101 + "/y", "a/b/c", "'a'/b"])
def test_malformed_repos_entry_reason_does_not_echo_the_value(
    dispatcher, queue, ready, failures, launches, approvals, bad
):
    write_plan(ready, f"**Repos:** TadMSTR/task-queue-mcp, {bad}".replace("\n", " "))
    refused(launch(dispatcher, queue), failures, launches, approvals, "at position 2")
    assert bad.replace("\n", " ") not in failures[0]


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"task_type": "<img src=x>"}, "<img"),
        ({"source_agent": "<b>x", "target_agent": "<b>x"}, "<b>"),
    ],
)
def test_task_field_reasons_do_not_echo_values(
    dispatcher, queue, ready, failures, launches, approvals, override, field
):
    refused(launch(dispatcher, queue, fixer_task(**override)), failures, launches, approvals, "")
    assert field not in failures[0]


@pytest.mark.parametrize("payload", [None, [], "x", 3])
def test_non_mapping_payload_is_refused_not_raised(
    dispatcher, queue, ready, failures, launches, approvals, payload
):
    refused(
        launch(dispatcher, queue, fixer_task(payload=payload)),
        failures,
        launches,
        approvals,
        "payload is not a mapping",
    )


def test_requires_approval_is_refused(dispatcher, queue, ready, failures, launches, approvals):
    refused(
        launch(dispatcher, queue, fixer_task(requires_approval=True)),
        failures,
        launches,
        approvals,
        "requires_approval",
    )


@pytest.mark.parametrize("status", [["in-progress"], {"a": 1}, 3, None])
def test_unhashable_or_odd_build_task_status_is_refused_not_raised(
    dispatcher, queue, ready, failures, launches, approvals, build_task, status
):
    path, task = build_task
    task["status"] = status
    path.write_text(yaml.safe_dump(task, sort_keys=False))
    refused(launch(dispatcher, queue), failures, launches, approvals, "is not live")


@pytest.mark.parametrize("scalar", ["2026-02-30", "2026-13-01", "2026-02-30 25:61:00"])
def test_impossible_timestamp_in_front_matter_is_refused_not_raised(
    dispatcher, queue, ready, failures, launches, approvals, scalar
):
    p = write_handoff(ready)
    p.write_text(p.read_text().replace("artifact-type: handoff", f"created: {scalar}", 1))
    refused(launch(dispatcher, queue), failures, launches, approvals, "does not parse")


def test_any_other_check_exception_still_routes_to_failure(
    dispatcher, queue, ready, failures, launches, approvals, monkeypatch
):
    def boom(*a, **k):
        raise KeyError("x")

    monkeypatch.setattr(dispatcher, "plan_repos", boom)
    refused(launch(dispatcher, queue), failures, launches, approvals, "malformed input (KeyError)")


@pytest.mark.parametrize("bad", ["group", "world", "owner"])
def test_guard_ancestor_chain_is_checked(
    dispatcher, queue, ready, failures, launches, approvals, guard, monkeypatch, bad
):
    # FW-35: the leaf and its directory were checked; an ancestor above them was not.
    mid = guard.parent / "mid"
    mid.mkdir()
    g2 = mid / "inner"
    g2.mkdir()
    leaf = g2 / guard.name
    leaf.write_text("#!/usr/bin/env python3\n")
    leaf.chmod(0o755)
    g2.chmod(0o755)
    mid.chmod({"group": 0o775, "world": 0o757, "owner": 0o755}[bad])
    monkeypatch.setattr(dispatcher, "FIXER_GUARD_PATH", leaf)
    if bad == "owner":
        real = dispatcher.os.lstat

        def lstat(p, *a, **k):
            st = real(p, *a, **k)
            if Path(p) == mid:
                return os.stat_result(
                    (
                        st.st_mode,
                        st.st_ino,
                        st.st_dev,
                        st.st_nlink,
                        st.st_uid + 1,
                        st.st_gid,
                        st.st_size,
                        st.st_atime,
                        st.st_mtime,
                        st.st_ctime,
                    )
                )
            return st

        monkeypatch.setattr(dispatcher.os, "lstat", lstat)
    refused(launch(dispatcher, queue), failures, launches, approvals, "PreCompact guard")
    assert str(mid) in failures[0]


def test_tick_never_sends_a_pre_audit_to_operator_approval(
    dispatcher, queue, ready, write_task, monkeypatch, launches
):
    # IV-81: a pre_audit with requires_approval: true (or high risk) went to
    # request_approval ahead of launch_kind, and an operator approval left it `approved`
    # with no check run. It must reach launch_fixer, which refuses it.
    asked: list = []
    monkeypatch.setattr(dispatcher, "request_approval", lambda *a, **k: asked.append(a))
    routed: list = []
    monkeypatch.setattr(
        dispatcher, "handle_routing_failure", lambda p, t, reason: routed.append(reason)
    )
    write_task(**fixer_task(requires_approval=True, risk_level="critical"))
    dispatcher.process_submitted(dispatcher.load_manifests())
    assert asked == [], "a pre_audit must never be sent for operator approval"
    assert launches == []
    assert routed and "requires_approval" in routed[0]


def test_log_dir_is_created_0700(dispatcher, queue, ready, failures, launches, approvals):
    logs = ready / ".pm2" / "logs"
    logs.rmdir()
    (ready / ".pm2").rmdir()
    old = os.umask(0o002)
    try:
        assert launch(dispatcher, queue) is True, failures
    finally:
        os.umask(old)
    assert stat.S_IMODE(logs.stat().st_mode) == 0o700


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


# --- pre-audit pass 2 (audit-fixer-2026-11-p2 round 1): each fails on b1b00ce -----------


@pytest.mark.parametrize(
    "plant",
    ["symlink", "fifo", "directory", "oversize"],
)
def test_plan_refusal_reasons_carry_no_programme_dir_name(
    dispatcher, queue, ready, failures, launches, approvals, tmp_path, plant
):
    """OE-01/OV-19: a programme directory name is agent-written and the reason reaches a
    markdown Matrix alert on dead-letter. The reason names the plan, never its path."""
    plans = ready / ".claude" / "comms" / "artifacts" / "build-plans"
    for p in plans.rglob("plan.md"):
        p.unlink()
    name = "**pwn**<img src=x onerror=alert(1)>"
    d = plans / name / BUILD
    d.mkdir(parents=True)
    target = d / "plan.md"
    if plant == "symlink":
        real = tmp_path / "elsewhere.md"
        real.write_text("**Repos:** TadMSTR/task-queue-mcp\n")
        target.symlink_to(real)
    elif plant == "fifo":
        os.mkfifo(target)
    elif plant == "directory":
        target.mkdir()
    else:
        target.write_text("x" * (dispatcher.FIXER_HANDOFF_MAX + 1))
    refused(launch(dispatcher, queue), failures, launches, approvals, "")
    assert "pwn" not in failures[0] and "<img" not in failures[0], failures[0]
    assert str(plans) not in failures[0]


def test_read_contained_reasons_name_the_position_not_the_value(dispatcher, tmp_path):
    (tmp_path / "secret-name").mkdir()
    (tmp_path / "secret-name" / "f").symlink_to("/etc/hostname")
    with pytest.raises(dispatcher.FixerRefused) as e:
        dispatcher.read_contained(tmp_path, ["secret-name", "f"], 10, "plan")
    assert "secret-name" not in str(e.value) and str(tmp_path) not in str(e.value)
    assert "component 2 of 2" in str(e.value)


def test_new_log_dirs_are_0700_under_a_loose_umask(
    dispatcher, queue, ready, failures, launches, approvals
):
    """FW-07/FW-12: mkdir's mode is masked by the umask, and parents=True made ~/.pm2 with
    it. Both levels the launcher creates end 0700."""
    (ready / ".pm2" / "logs").rmdir()
    (ready / ".pm2").rmdir()
    old = os.umask(0o002)
    try:
        assert launch(dispatcher, queue) is True, failures
    finally:
        os.umask(old)
    for d in (ready / ".pm2", ready / ".pm2" / "logs"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d


@pytest.mark.parametrize("which", ["", ".pm2"])
def test_a_group_writable_log_dir_others_can_reach_is_refused_before_approval(
    dispatcher, queue, ready, failures, launches, approvals, which
):
    (ready / which).chmod(0o775)
    if which == "":
        (ready / ".pm2").chmod(0o755)
    refused(launch(dispatcher, queue), failures, launches, approvals, "group/world-writable")


def test_pm2s_own_0775_logs_under_a_0700_pm2_launches(
    dispatcher, queue, ready, failures, launches, approvals
):
    """The live shape on forge. Refusing it would refuse every fixer."""
    assert stat.S_IMODE((ready / ".pm2" / "logs").stat().st_mode) == 0o775
    assert launch(dispatcher, queue) is True, failures
    assert stat.S_IMODE((ready / ".pm2" / "logs").stat().st_mode) == 0o775


def test_a_log_dir_owned_by_another_uid_is_refused(
    dispatcher, queue, ready, failures, launches, approvals, monkeypatch
):
    real_lstat = os.lstat
    logs = ready / ".pm2" / "logs"

    def lstat(p, *a, **k):
        st = real_lstat(p, *a, **k)
        if Path(p) == logs:
            return os.stat_result(
                (
                    st.st_mode,
                    st.st_ino,
                    st.st_dev,
                    st.st_nlink,
                    os.getuid() + 1,
                    st.st_gid,
                    st.st_size,
                    st.st_atime,
                    st.st_mtime,
                    st.st_ctime,
                )
            )
        return st

    monkeypatch.setattr(dispatcher.os, "lstat", lstat)
    refused(launch(dispatcher, queue), failures, launches, approvals, "not owned by this user")


def test_a_linked_log_dir_is_refused_before_approval(
    dispatcher, queue, ready, failures, launches, approvals, tmp_path
):
    logs = ready / ".pm2" / "logs"
    logs.rmdir()
    other = tmp_path / "other-logs"
    other.mkdir(mode=0o700)
    logs.symlink_to(other)
    refused(launch(dispatcher, queue), failures, launches, approvals, "not a directory")


@pytest.mark.parametrize("plant", ["symlink", "fifo"])
def test_a_planted_log_file_is_refused_before_approval(
    dispatcher, queue, ready, failures, launches, approvals, tmp_path, plant
):
    log = ready / ".pm2" / "logs" / f"fixer-developer-{BUILD}-r1-h0.log"
    if plant == "symlink":
        (tmp_path / "victim").write_text("")
        log.symlink_to(tmp_path / "victim")
    else:
        os.mkfifo(log)
    refused(launch(dispatcher, queue), failures, launches, approvals, "session log")


def test_an_existing_loose_log_file_is_tightened_to_0600(
    dispatcher, queue, ready, failures, launches, approvals
):
    log = ready / ".pm2" / "logs" / f"fixer-developer-{BUILD}-r1-h0.log"
    log.write_text("earlier run\n")
    log.chmod(0o644)
    assert launch(dispatcher, queue) is True, failures
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_popen_failure_after_approval_routes_to_failure(
    dispatcher, queue, ready, failures, approvals, monkeypatch
):
    """OV-18: an approved task whose session never started must not stay `approved`."""

    def boom(*a, **k):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(dispatcher.subprocess, "Popen", boom)
    assert launch(dispatcher, queue) is False
    assert approvals == ["Auto-approved: test"]
    assert len(failures) == 1 and "failed to start (FileNotFoundError)" in failures[0]


@pytest.mark.parametrize("payload", [None, [], "x", 3])
def test_non_mapping_payload_through_the_tick_routes_to_failure(
    dispatcher, ready, launches, write_task, payload
):
    """IV-52: the submitted tick read payload.get() before launch_fixer saw the task."""
    path, _ = _queue_fixer(write_task, payload=payload)
    dispatcher.process_submitted({})
    assert launches == []
    data = yaml.safe_load(path.read_text())
    assert data["status"] == "routing-failed"
    assert "payload is not a mapping" in data["retry_policy"]["last_failure_reason"]


def test_build_plans_lstat_error_is_refused_not_read_as_absent(
    dispatcher, queue, ready, failures, launches, approvals, monkeypatch
):
    """SH-15: os.path.lexists() read a PermissionError on build-plans/ as "no plan"."""
    root = dispatcher.build_plans_root()
    real_lstat = os.lstat

    def lstat(p, *a, **k):
        if Path(p) == root:
            raise PermissionError(13, "denied")
        return real_lstat(p, *a, **k)

    monkeypatch.setattr(dispatcher.os, "lstat", lstat)
    refused(launch(dispatcher, queue), failures, launches, approvals, "build-plans is unreadable")


def test_an_inherited_bearer_token_does_not_stand_in_for_the_agents(
    dispatcher, queue, ready, failures, launches, approvals, monkeypatch
):
    """SC-43: the presence check must see the agent's own token, not the dispatcher's."""
    monkeypatch.setenv("SCOPED_MCP_BEARER_TOKEN", "the-dispatchers-own")
    monkeypatch.setattr(dispatcher, "load_agent_env", lambda a: {})
    refused(launch(dispatcher, queue), failures, launches, approvals, "unresolved")


def test_the_agents_token_replaces_an_inherited_one(
    dispatcher, queue, ready, failures, launches, approvals, monkeypatch
):
    monkeypatch.setenv("SCOPED_MCP_BEARER_TOKEN", "the-dispatchers-own")
    assert launch(dispatcher, queue) is True, failures
    assert launches[0]["env"]["SCOPED_MCP_BEARER_TOKEN"] == "t"


def _hostile_front_matter() -> dict[str, str]:
    """IV-80's required shapes, each inside the 16 KiB front-matter cap."""
    # Block nesting on one line: each `- ` / `? ` opens a level two bytes wide.
    block_seq = "a:\n  " + "- " * 7000 + "x\n"
    complex_keys = "a:\n  " + "? " * 7000 + "x\n"
    laughs = (
        "a: &a ["
        + ",".join(["x" * 20] * 20)
        + "]\n"
        + "".join(f"b{i}: [*a, *a, *a, *a, *a, *a, *a, *a]\n" for i in range(400))
    )
    chain = "k0: &k0 {v: 1}\n" + "".join(f"k{i}: &k{i}\n  <<: *k{i - 1}\n" for i in range(1, 600))
    flow = "a: " + "[" * 8000 + "]" * 8000 + "\n"
    return {
        "block-seq": block_seq,
        "complex-keys": complex_keys,
        "laughs": laughs,
        "merge-chain": chain,
        "flow": flow,
    }


@pytest.mark.parametrize("shape", sorted(_hostile_front_matter()))
def test_hostile_yaml_shapes_are_refused_quickly(
    dispatcher, queue, ready, failures, launches, approvals, shape
):
    import time

    body = _hostile_front_matter()[shape]
    assert len(body) < dispatcher.FIXER_FRONT_MAX, (shape, len(body))
    (_handoff_dir(ready) / "r1-h0.md").write_text("---\n" + body + "---\n")
    t0 = time.monotonic()
    result = launch(dispatcher, queue)
    assert time.monotonic() - t0 < 5, shape
    refused(result, failures, launches, approvals, "")


def test_yaml_aliases_are_refused(dispatcher):
    with pytest.raises(dispatcher.FixerRefused, match="does not parse"):
        dispatcher.parse_front_matter(b"---\na: &x [1]\nb: *x\n---\n")


@pytest.mark.parametrize("name", ["TASK_QUEUE_TOKEN", "GITHOST_MCP_AUTH_TOKEN"])
def test_other_inherited_agent_credentials_are_dropped(
    dispatcher, queue, ready, failures, launches, approvals, monkeypatch, name
):
    """SC-43: the dispatcher's own copy of a per-agent credential never reaches a fixer."""
    monkeypatch.setenv(name, "the-dispatchers-own")
    assert launch(dispatcher, queue) is True, failures
    assert name not in launches[0]["env"]


def test_a_poison_queue_file_does_not_abort_the_tick(dispatcher, ready, launches, write_task):
    """OV-33: an unhashable risk_level raised out of process_submitted and stopped every
    task after it, every tick. It now goes to failure and the next task still runs."""
    bad_path, _ = write_task(
        id="cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        task_type="build",
        source_agent="research",
        target_agent="developer",
        status="submitted",
        risk_level=["high"],
        requires_approval=None,  # so the risk fallback reads risk_level
        summary="poison",
    )
    good_path, _ = _queue_fixer(write_task)
    dispatcher.process_submitted({})
    assert len(launches) == 1
    assert yaml.safe_load(good_path.read_text())["status"] == "approved"
    _assert_dead_lettered(dispatcher, bad_path, "dispatcher error while routing (TypeError)")


def _assert_dead_lettered(dispatcher, path, reason):
    assert not path.exists()
    dead = yaml.safe_load((dispatcher.DEAD_LETTER_DIR / path.name).read_text())
    assert dead["status"] == "failed"
    assert reason in dead["failed_reason"]["reason"]
    assert not any(h.get("status") == "approved" for h in dead["history"])


def test_a_poison_file_never_reaches_the_unapproved_retry(
    dispatcher, ready, launches, write_task, monkeypatch
):
    """OV-24: the poison file raised before its approval decision ran. Parked in
    routing-failed, the retry pass's generic branch approved it (or, under auto, launched
    it headless) without that decision. It is dead-lettered instead, and the retry pass
    finds nothing to approve."""
    approved: list[str] = []
    monkeypatch.setattr(
        dispatcher, "approve_and_write", lambda p, t, note: approved.append(t.get("id"))
    )
    headless: list[str] = []
    monkeypatch.setattr(dispatcher, "launch_agent_headless", lambda t: headless.append(t["id"]))
    bad_path, _ = write_task(
        id="cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        task_type="build",
        source_agent="research",
        target_agent="developer",
        status="submitted",
        workflow_mode="auto",
        risk_level=["high"],
        requires_approval=None,
        summary="poison",
    )
    dispatcher.process_submitted({})
    dispatcher.process_routing_failed({})
    _assert_dead_lettered(dispatcher, bad_path, "dispatcher error while routing (TypeError)")
    assert approved == [] and headless == []


def _poison_routing_failed():
    return {
        "unhashable task_type": {"task_type": ["build"]},
        "unhashable workflow_mode": {"workflow_mode": {"auto": 1}},
        "retry_policy a list": {"retry_policy": ["x"]},
        "history not a list": {"task_type": ["build"], "history": "x"},
    }


@pytest.mark.parametrize("shape", sorted(_poison_routing_failed()))
def test_a_poison_routing_failed_file_does_not_stop_the_retry_drain(
    dispatcher, ready, launches, write_task, shape
):
    """OV-33/OV-18: process_routing_failed had no per-item guard, so a poison file parked
    there raised every tick and stopped the drain, the dead-letter alert, archive_expired
    and the run-complete line. Now it is dead-lettered and the next task still retries."""
    fields = {
        "id": "00000000-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        "task_type": "build",
        "source_agent": "research",
        "target_agent": "developer",
        "status": "routing-failed",
        "summary": "poison",
        "retry_policy": {"retry_count": 1, "next_retry_at": None},
    }
    fields.update(_poison_routing_failed()[shape])
    bad_path, _ = write_task(**fields)
    good_path, _ = _queue_fixer(write_task, status="routing-failed")
    dispatcher.process_routing_failed({})
    _assert_dead_lettered(dispatcher, bad_path, "dispatcher error while retrying routing")
    assert len(launches) == 1
    assert yaml.safe_load(good_path.read_text())["status"] == "approved"


def test_a_poison_routing_failed_file_does_not_stop_the_tick(dispatcher, queue, write_task, caplog):
    """OV-18: the rest of main() still runs: archive_expired archives an expired task
    queued after the poison file, and the run-complete liveness line is logged."""
    from datetime import UTC, datetime, timedelta

    bad, _ = write_task(
        task_type=["build"],
        status="routing-failed",
        retry_policy={"retry_count": 1, "next_retry_at": None},
    )
    expired, _ = write_task(
        status="completed",
        created=(datetime.now(UTC) - timedelta(days=40)).isoformat(),
        ttl_days=30,
    )
    with caplog.at_level("INFO"):
        assert dispatcher.main() == 0
    assert not bad.exists() and not expired.exists()
    assert (dispatcher.ARCHIVE_DIR / expired.name).exists()
    assert "=== task-dispatcher run complete ===" in caplog.text


# ── Pass 5: the quarantine class (OV-33, OV-01, FW-26, FW-09) ────────────────


@pytest.fixture
def alerts(dispatcher, monkeypatch):
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        dispatcher,
        "matrix_notify",
        lambda room, title, body: sent.append((room, title + body)) or True,
    )
    return sent


_UNROUTABLE_BODIES = {
    "unparseable": "a: [unclosed\n",
    "a list": "- one\n- two\n",
    "a string": "just text\n",
}


@pytest.mark.parametrize("shape", sorted(_UNROUTABLE_BODIES))
def test_an_unroutable_queue_file_is_quarantined_with_one_alert(dispatcher, queue, alerts, shape):
    """OV-33/OV-01: a file that does not parse, or parses to something not a mapping, was
    logged and left in the queue, failing the same way every tick and never alerted."""
    bad = queue / "task-900.yml"
    bad.write_text(_UNROUTABLE_BODIES[shape])
    assert dispatcher.main() == 0
    assert not bad.exists()
    assert (dispatcher.DEAD_LETTER_DIR / "task-900.yml").read_text() == _UNROUTABLE_BODIES[shape]
    assert [room for room, _ in alerts] == ["alerts"]
    assert "Moved to dead-letters/: 1." in alerts[0][1]
    assert dispatcher.main() == 0
    assert len(alerts) == 1  # nothing left to report on the next tick


def test_quarantine_is_capped_per_tick_with_one_summary_alert(
    dispatcher, queue, alerts, write_task
):
    """FW-26: each poison file used to dead-letter with its own #alerts message, so an
    agent writing N files sent N alerts. Now at most QUARANTINE_PER_TICK_MAX move per tick,
    the rest wait, and each tick sends one summary."""
    cap = dispatcher.QUARANTINE_PER_TICK_MAX
    for _ in range(cap + 5):
        write_task(risk_level=["high"], requires_approval=None)
    assert dispatcher.main() == 0
    assert len(list(dispatcher.DEAD_LETTER_DIR.iterdir())) == cap
    assert len(alerts) == 1 and f"cap of {cap}: 5." in alerts[0][1]
    assert dispatcher.main() == 0
    assert len(list(dispatcher.DEAD_LETTER_DIR.iterdir())) == cap + 5
    assert len(alerts) == 2


def test_a_failed_quarantine_alerts_and_is_debounced(dispatcher, queue, alerts, monkeypatch):
    """OV-01: if the move itself fails, the file stays and fails every tick. That used to
    be a log line only; now it alerts, at most once per debounce window."""
    (queue / "task-900.yml").write_text("- not a mapping\n")

    def no_link(*a, **k):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(dispatcher.os, "link", no_link)
    assert dispatcher.main() == 0
    assert len(alerts) == 1 and "Could not be moved: 1." in alerts[0][1]
    assert dispatcher.main() == 0
    assert len(alerts) == 1  # debounced
    assert (queue / "task-900.yml").exists()


def test_a_dead_letter_never_overwrites_an_earlier_one(dispatcher, queue, alerts, write_task):
    """FW-09: dead-letters/<name> was written with atomic_write, which replaces."""
    path, _ = write_task(risk_level=["high"], requires_approval=None)
    dispatcher.DEAD_LETTER_DIR.mkdir()
    earlier = dispatcher.DEAD_LETTER_DIR / path.name
    earlier.write_text("earlier dead letter\n")
    assert dispatcher.main() == 0
    assert earlier.read_text() == "earlier dead letter\n"
    second = dispatcher.DEAD_LETTER_DIR / f"{path.stem}.1{path.suffix}"
    assert yaml.safe_load(second.read_text())["status"] == "failed"


# ── Pass 5: FW-25, FW-35 ──────────────────────────────────────────────────────


def test_a_linked_build_dir_with_no_plan_is_refused_not_unverified(dispatcher, home, tmp_path):
    """FW-25: the existence probe stat'ed `<build>/plan.md` relative to the pinned fd with
    follow_symlinks=False, which still followed a link at <build>. A link to a directory
    with no plan read as "no plan" and launched `Repos: unverified`."""
    root = home / ".claude" / "comms" / "artifacts" / "build-plans"
    empty = tmp_path / "elsewhere-empty"
    empty.mkdir()
    (root / BUILD).symlink_to(empty, target_is_directory=True)
    with pytest.raises(dispatcher.FixerRefused, match="linked directory"):
        dispatcher.find_plan(BUILD)


def test_a_linked_programme_with_no_plan_is_refused_not_followed(dispatcher, home, tmp_path):
    root = home / ".claude" / "comms" / "artifacts" / "build-plans"
    elsewhere = tmp_path / "elsewhere-programme"
    elsewhere.mkdir()
    (root / "linked-programme").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(dispatcher.FixerRefused, match="linked directory"):
        dispatcher.find_plan(BUILD)


@pytest.mark.parametrize("mode", [0o777, 0o1777, 0o757])
def test_a_world_writable_root_component_is_refused(dispatcher, home, mode):
    """FW-35: _pin_root accepted any root- or self-owned directory whatever its mode, a
    sticky world-writable one included."""
    comms = home / ".claude" / "comms"
    comms.chmod(mode)
    try:
        with pytest.raises(dispatcher.FixerRefused, match="another user can write"):
            dispatcher.find_plan(BUILD)
    finally:
        comms.chmod(0o755)


def test_a_group_writable_component_with_another_member_is_refused(dispatcher, home, monkeypatch):
    import grp

    comms = home / ".claude" / "comms"
    comms.chmod(0o775)
    real = grp.getgrgid

    class G:
        gr_mem = ("someone-else",)

    monkeypatch.setattr(
        grp, "getgrgid", lambda gid: G() if gid == comms.stat().st_gid else real(gid)
    )
    with pytest.raises(dispatcher.FixerRefused, match="another user can write"):
        dispatcher.find_plan(BUILD)


def test_a_group_writable_component_only_this_user_is_in_is_accepted(dispatcher, home, monkeypatch):
    """The control: forge's ~/.claude, comms and artifacts are 0775 group ted, and ted is
    group ted's only member. That must keep launching."""
    import grp
    import pwd

    comms = home / ".claude" / "comms"
    comms.chmod(0o775)
    gid = comms.stat().st_gid

    class G:
        gr_mem = ()

    real_g, real_all = grp.getgrgid, pwd.getpwall
    monkeypatch.setattr(grp, "getgrgid", lambda g: G() if g == gid else real_g(g))
    monkeypatch.setattr(
        pwd,
        "getpwall",
        lambda: [u for u in real_all() if u.pw_gid != gid or u.pw_uid == os.getuid()],
    )
    assert dispatcher.find_plan(BUILD) is None


# ── Audit round 1: F-01 (archive_expired), F-03 (quarantine alert delivery) ──


@pytest.mark.parametrize(
    "fields",
    [{"status": ["completed"]}, {"status": {"completed": 1}}],
    ids=["status a list", "status a mapping"],
)
def test_an_unhashable_status_is_quarantined_by_the_archive_pass(
    dispatcher, queue, alerts, write_task, caplog, fields
):
    """F-01 (OV-33, OV-18): archive_expired had no per-item guard. A status that is not a
    string raised TypeError out of main() every tick, ahead of flush_quarantine_alert()
    and the run-complete line. The drain passes skip it (status != "submitted"), so the
    archive pass is the only one that sees it."""
    from datetime import UTC, datetime, timedelta

    bad, _ = write_task(**fields)
    expired, _ = write_task(
        status="completed",
        created=(datetime.now(UTC) - timedelta(days=40)).isoformat(),
        ttl_days=30,
    )
    with caplog.at_level("INFO"):
        assert dispatcher.main() == 0
    assert not bad.exists()
    assert (dispatcher.ARCHIVE_DIR / expired.name).exists()
    assert [room for room, _ in alerts] == ["alerts"]
    assert "Moved to dead-letters/: 1." in alerts[0][1]
    assert "=== task-dispatcher run complete ===" in caplog.text


@pytest.mark.parametrize("ttl", ["x", "30", None, 1.5, True, [30]], ids=repr)
def test_a_non_integer_ttl_falls_back_to_the_default_not_the_dead_letters(
    dispatcher, queue, alerts, write_task, caplog, ttl
):
    """F-01: a non-integer ttl_days raised TypeError at the age comparison. It is not
    quarantined: that would rewrite a completed task as failed and emit task.failed for
    it. It archives on the 30-day default instead."""
    from datetime import UTC, datetime, timedelta

    old, _ = write_task(
        status="completed",
        created=(datetime.now(UTC) - timedelta(days=40)).isoformat(),
        ttl_days=ttl,
    )
    young, _ = write_task(
        status="completed",
        created=(datetime.now(UTC) - timedelta(days=10)).isoformat(),
        ttl_days=ttl,
    )
    with caplog.at_level("INFO"):
        assert dispatcher.main() == 0
    assert (dispatcher.ARCHIVE_DIR / old.name).exists()
    assert yaml.safe_load((dispatcher.ARCHIVE_DIR / old.name).read_text())["status"] == "completed"
    assert young.exists()
    assert alerts == []
    assert "=== task-dispatcher run complete ===" in caplog.text


def test_the_quarantine_alert_is_flushed_even_when_a_later_pass_raises(
    dispatcher, queue, alerts, monkeypatch
):
    """F-01: flush_quarantine_alert() ran after archive_expired, so anything raising there
    lost the tick's dead-letter summary. It now runs in main()'s finally."""
    (queue / "task-900.yml").write_text("- not a mapping\n")

    def boom():
        raise RuntimeError("archive pass failed")

    monkeypatch.setattr(dispatcher, "archive_expired", boom)
    with pytest.raises(RuntimeError):
        dispatcher.main()
    assert len(alerts) == 1 and "Moved to dead-letters/: 1." in alerts[0][1]


def test_a_failed_quarantine_alert_is_retried_with_its_counts(dispatcher, queue, monkeypatch):
    """F-03: the counts were reset and the debounce stamp written before and regardless of
    the send, so a moved-files summary whose send failed was never retried, and a failed
    held/failed alert suppressed the next one for 15 minutes."""
    sent: list[str] = []
    ok = iter([False, True])

    def notify(room, title, body):
        sent.append(body)
        return next(ok)

    monkeypatch.setattr(dispatcher, "matrix_notify", notify)
    (queue / "task-900.yml").write_text("- not a mapping\n")
    assert dispatcher.main() == 0
    assert not dispatcher.QUARANTINE_ALERT_STAMP.exists()
    assert dispatcher.main() == 0
    assert len(sent) == 2 and "Moved to dead-letters/: 1." in sent[1]
    assert dispatcher.QUARANTINE_ALERT_STAMP.exists()
    assert dispatcher.main() == 0
    assert len(sent) == 2  # delivered: nothing carried over


def test_an_unsent_moved_count_does_not_shrink_the_next_ticks_cap(
    dispatcher, queue, write_task, monkeypatch
):
    """F-03: the carried count is reported, not charged against QUARANTINE_PER_TICK_MAX."""
    monkeypatch.setattr(dispatcher, "matrix_notify", lambda room, title, body: False)
    cap = dispatcher.QUARANTINE_PER_TICK_MAX
    for _ in range(cap * 2):
        write_task(risk_level=["high"], requires_approval=None)
    assert dispatcher.main() == 0
    assert dispatcher.main() == 0
    assert len(list(dispatcher.DEAD_LETTER_DIR.iterdir())) == cap * 2


def test_a_future_alert_stamp_does_not_silence_the_alert(dispatcher, queue, alerts, monkeypatch):
    """F-03: a stamp dated in the future read as "alerted just now" for as long as it
    stayed in the future. It is treated as absent."""
    from datetime import UTC, datetime, timedelta

    dispatcher.QUARANTINE_ALERT_STAMP.write_text(
        (datetime.now(UTC) + timedelta(days=365)).isoformat()
    )
    (queue / "task-900.yml").write_text("- not a mapping\n")

    def no_link(*a, **k):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(dispatcher.os, "link", no_link)
    assert dispatcher.main() == 0
    assert len(alerts) == 1 and "Could not be moved: 1." in alerts[0][1]
