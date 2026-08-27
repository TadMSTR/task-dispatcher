#!/usr/bin/env python3
"""
Tests for task-dispatcher.py's headless-chain behaviour
(task-queue-headless-chain-2026-08 — vikunja#507, #533, #324).

Two properties, and neither is provable by testing child_workflow_mode() alone:

  1. `manual-then-auto` gates its own leg and downgrades its children — at ALL THREE
     propagation sites. A test of the helper in isolation passes with two of the three
     call sites still copying the parent mode verbatim, which is exactly the bug the
     plan warned about. So every assertion below goes through the real call site and
     reads what the launched process would actually have seen.

  2. `notify` is never launched. Not "usually falls through to a cheap branch" —
     never reaches Popen at all.

Hermetic: redirects HOME to a tmpdir BEFORE importing the dispatcher (module-level
code resolves TASK_QUEUE_DIR and opens a log file from it), stubs subprocess.Popen,
matrix_notify, publish_nats and bus_log. Sends nothing, launches nothing, and never
touches the real queue.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import yaml

FAILURES: list[str] = []
_TMP = tempfile.TemporaryDirectory()
_HOME = Path(_TMP.name)
(_HOME / ".claude" / "task-queue").mkdir(parents=True)
(_HOME / ".claude" / "manifests").mkdir(parents=True)
(_HOME / ".pm2" / "logs").mkdir(parents=True)
for _agent in ("developer", "security", "steward", "sysadmin", "writer", "research"):
    (_HOME / ".claude" / "projects" / _agent).mkdir(parents=True)
os.environ["HOME"] = str(_HOME)

MODULE_PATH = Path(__file__).resolve().parent.parent / "task-dispatcher.py"
_spec = importlib.util.spec_from_file_location("task_dispatcher", MODULE_PATH)
td = importlib.util.module_from_spec(_spec)
sys.modules["task_dispatcher"] = td
_spec.loader.exec_module(td)

# Neutralise every outbound channel. Popen is captured rather than stubbed away: the
# tests need to assert on what WOULD have been launched, and on the fact that some
# cases launch nothing.
LAUNCHES: list[dict] = []
NOTIFICATIONS: list[tuple] = []


class _FakeProc:
    pid = 4242


def _fake_popen(argv, cwd=None, stdout=None, stderr=None, env=None, **kw):
    LAUNCHES.append({"argv": argv, "env": dict(env or {}), "cwd": cwd})
    return _FakeProc()


td.subprocess.Popen = _fake_popen
td.matrix_notify = lambda room, title, body: NOTIFICATIONS.append((room, title, body))
td.publish_nats = lambda *a, **k: None
td.bus_log = lambda *a, **k: None
td.alert_auth_blocked = lambda *a, **k: None
td.anthropic_creds_usable = lambda env: True
td.load_agent_env = lambda agent: {"SCOPED_MCP_BEARER_TOKEN": "test-token"}

QUEUE = td.TASK_QUEUE_DIR
MANIFESTS = {
    a: {"max_auto_risk": "high", "capabilities": ["build", "review", "audit", "notify"]}
    for a in ("developer", "security", "steward", "sysadmin", "writer", "research")
}


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def reset() -> None:
    LAUNCHES.clear()
    NOTIFICATIONS.clear()
    for f in QUEUE.glob("*.yml"):
        f.unlink()


_COUNTER = [0]


def write_task(**kw) -> Path:
    """Write a queue file the way submit_task would have."""
    _COUNTER[0] += 1
    n = _COUNTER[0]
    task_id = f"{n:08d}-0000-4000-8000-{n:012d}"
    task = {
        "id": task_id,
        "source_agent": kw.pop("source_agent", "steward"),
        "target_agent": kw.pop("target_agent", "security"),
        "task_type": kw.pop("task_type", "review"),
        "risk_level": kw.pop("risk_level", "low"),
        "requires_approval": False,
        "workflow_mode": kw.pop("workflow_mode", "semi-auto"),
        "status": kw.pop("status", "submitted"),
        "summary": kw.pop("summary", f"Test task {n}"),
        "ttl_days": 30,
        "payload": {
            "description": kw.pop("description", "test description"),
            "context_refs": [],
            "priority": "normal",
            **({"originating_task_id": kw["originating_task_id"]} if "originating_task_id" in kw else {}),
        },
        "result": {"output": None, "completed_by": None, "completed_at": None},
        "history": [],
        "retry_policy": {"next_retry_at": None, "retry_count": 0},
    }
    kw.pop("originating_task_id", None)
    task.update(kw)
    path = QUEUE / f"2026{n:04d}-000000-{task_id[:8]}.yml"
    path.write_text(yaml.dump(task, default_flow_style=False, sort_keys=False))
    return path


def read(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def launched_mode() -> str | None:
    """FORGE_WORKFLOW_MODE the single launched process would have seen."""
    if len(LAUNCHES) != 1:
        return None
    launch = LAUNCHES[0]
    if launch["argv"][0] == "sudo":
        # Run-as path: sudo scrubs the environment, so the launcher flag IS the channel.
        argv = launch["argv"]
        return argv[argv.index("--workflow-mode") + 1]
    return launch["env"].get("FORGE_WORKFLOW_MODE")


# ---------------------------------------------------------------------------


def test_helper_across_every_input() -> None:
    print("\nchild_workflow_mode()")
    check(td.child_workflow_mode("manual-then-auto") == "auto", "manual-then-auto → auto")
    check(td.child_workflow_mode("auto") == "auto", "auto → auto (unchanged)")
    check(td.child_workflow_mode("semi-auto") == "semi-auto", "semi-auto → semi-auto (unchanged)")
    # Not in the vocabulary, but the helper must not invent a downgrade for it either —
    # the loud rejection belongs to the dispatch guards, not here.
    check(td.child_workflow_mode("manual") == "manual", "unknown value passes through")


def test_site_1_env_var() -> None:
    """The env var a normally-launched agent reads."""
    print("\npropagation site 1 — FORGE_WORKFLOW_MODE")

    reset()
    td.launch_agent_headless(read(write_task(target_agent="developer", workflow_mode="manual-then-auto")))
    check(launched_mode() == "auto", "manual-then-auto parent launches child with auto")

    reset()
    td.launch_agent_headless(read(write_task(target_agent="developer", workflow_mode="auto")))
    check(launched_mode() == "auto", "auto parent still launches with auto")

    reset()
    td.launch_agent_headless(read(write_task(target_agent="developer", workflow_mode="semi-auto")))
    check(launched_mode() == "semi-auto", "semi-auto parent still launches with semi-auto")


def test_site_2_run_as_launcher_flag() -> None:
    """
    The --workflow-mode flag handed to a run-as launcher. sudo scrubs the environment,
    so this flag is the ONLY channel for the one agent (steward) this build exists for.
    Patching site 1 alone would leave exactly that agent broken.
    """
    print("\npropagation site 2 — run-as launcher flag")
    real_access = td.os.access
    td.os.access = lambda p, mode: True
    try:
        reset()
        td.launch_agent_headless(read(write_task(target_agent="steward", workflow_mode="manual-then-auto")))
        check(LAUNCHES and LAUNCHES[0]["argv"][0] == "sudo", "steward goes through the run-as path")
        check(launched_mode() == "auto", "manual-then-auto reaches the launcher as auto")

        reset()
        td.launch_agent_headless(read(write_task(target_agent="steward", workflow_mode="semi-auto")))
        check(launched_mode() == "semi-auto", "semi-auto reaches the launcher unchanged")
    finally:
        td.os.access = real_access


def test_site_3_originating_task_inheritance() -> None:
    """
    A task submitted by an agent that never read FORGE_WORKFLOW_MODE. The dispatcher
    reads the parent off disk and copies its mode down — which must also downgrade.
    """
    print("\npropagation site 3 — originating_task_id inheritance")

    reset()
    parent = write_task(source_agent="ted", target_agent="steward",
                        workflow_mode="manual-then-auto", status="approved")
    child = write_task(source_agent="steward", target_agent="security",
                       workflow_mode="semi-auto",
                       originating_task_id=read(parent)["id"])
    td.process_submitted(MANIFESTS)
    check(read(child)["workflow_mode"] == "auto",
          "child of a manual-then-auto parent is rewritten to auto")
    check(read(parent)["workflow_mode"] == "manual-then-auto",
          "the parent keeps its own mode verbatim")

    reset()
    parent = write_task(target_agent="steward", workflow_mode="semi-auto", status="approved")
    child = write_task(source_agent="steward", target_agent="security",
                       workflow_mode="semi-auto", originating_task_id=read(parent)["id"])
    td.process_submitted(MANIFESTS)
    check(read(child)["workflow_mode"] == "semi-auto",
          "REGRESSION GUARD: a semi-auto parent still yields semi-auto children")

    reset()
    parent = write_task(target_agent="steward", workflow_mode="auto", status="approved")
    child = write_task(source_agent="steward", target_agent="security",
                       workflow_mode="semi-auto", originating_task_id=read(parent)["id"])
    td.process_submitted(MANIFESTS)
    check(read(child)["workflow_mode"] == "auto", "an auto parent still yields auto children")


def test_manual_then_auto_does_not_auto_launch() -> None:
    """The whole value of the mode: its OWN leg waits for an operator."""
    print("\nmanual-then-auto gates its own leg")

    reset()
    path = write_task(target_agent="developer", workflow_mode="manual-then-auto")
    td.process_submitted(MANIFESTS)
    check(LAUNCHES == [], "no session launched")
    check(read(path)["status"] == "approved", "queued as approved for operator pickup")
    check(any("task ready" in t for _, t, _ in NOTIFICATIONS), "operator was notified")

    reset()
    path = write_task(target_agent="developer", workflow_mode="auto")
    td.process_submitted(MANIFESTS)
    check(len(LAUNCHES) == 1, "CONTRAST: plain auto does launch, so the check above means something")


def test_notify_is_never_launched() -> None:
    print("\nnotify never launches (vikunja#507)")

    reset()
    path = write_task(target_agent="steward", task_type="notify", workflow_mode="auto",
                      description="verdict: approve")
    td.process_submitted(MANIFESTS)
    check(LAUNCHES == [], "no session launched even in auto mode")
    task = read(path)
    check(task["status"] == "completed", "written terminal")
    check(task["result"]["output"] == "verdict: approve", "the notification content is the result")
    check(task["result"]["completed_by"] == "dispatcher (notify)", "closed by the dispatcher")
    check(task["history"][-1]["status"] == "completed", "the close is in the history")

    # Same in the retry pass: a guard only on the first pass is not a guard.
    reset()
    path = write_task(target_agent="steward", task_type="notify", workflow_mode="auto",
                      status="routing-failed")
    td.process_routing_failed(MANIFESTS)
    check(LAUNCHES == [], "routing-failed retry does not launch it either")
    check(read(path)["status"] == "completed", "routing-failed retry closes it")


def test_unknown_vocabulary_fails_loudly() -> None:
    """
    #324: the dispatcher must not fall through to a default branch on a value the MCP
    would have rejected. An unknown workflow_mode silently became operator-pickup, which
    is indistinguishable from a correct semi-auto dispatch.
    """
    print("\nunknown vocabulary fails loudly (vikunja#324)")

    reset()
    path = write_task(target_agent="developer", workflow_mode="manual")
    td.process_submitted(MANIFESTS)
    task = read(path)
    check(task["status"] == "routing-failed", "unknown workflow_mode → routing-failed")
    check(task["status"] != "approved", "specifically NOT silently approved as semi-auto")
    check(LAUNCHES == [], "and nothing launched")

    reset()
    path = write_task(target_agent="developer", task_type="totally-made-up")
    td.process_submitted(MANIFESTS)
    check(read(path)["status"] == "routing-failed", "unknown task_type → routing-failed")

    reset()
    path = write_task(target_agent="developer", workflow_mode="manual", status="routing-failed")
    td.process_routing_failed(MANIFESTS)
    check(read(path)["status"] == "routing-failed",
          "the retry pass is not a way around the guard")

    # `workflow` is not in VALID_TASK_TYPES and never was — it is written directly by
    # temporal-workflow-start.sh. The guard must not dead-letter it.
    reset()
    path = write_task(target_agent="developer", task_type="workflow", workflow_mode="semi-auto")
    td.launch_temporal_workflow = lambda p, t: False
    td.process_submitted(MANIFESTS)
    check(read(path)["status"] != "routing-failed", "dispatcher-only `workflow` type still routes")


def test_vocabulary_is_internally_consistent() -> None:
    print("\nvocabulary")
    check(td.VALID_WORKFLOW_MODES == {"semi-auto", "auto", "manual-then-auto"},
          "workflow modes are exactly the three")
    check(td.SELF_TERMINAL_TASK_TYPES <= td.VALID_TASK_TYPES,
          "every self-terminal type is a real task type")
    check(td.TERMINAL_STATES is td.TERMINAL_STATUSES, "the alias is the same object")
    check(td.TERMINAL_STATUSES <= td.VALID_STATUSES, "terminal statuses are real statuses")
    check("manual" not in td.VALID_WORKFLOW_MODES,
          "`manual` is the launcher's word, not the queue's")


def main() -> int:
    test_helper_across_every_input()
    test_site_1_env_var()
    test_site_2_run_as_launcher_flag()
    test_site_3_originating_task_inheritance()
    test_manual_then_auto_does_not_auto_launch()
    test_notify_is_never_launched()
    test_unknown_vocabulary_fails_loudly()
    test_vocabulary_is_internally_consistent()

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("all dispatcher headless-chain checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
