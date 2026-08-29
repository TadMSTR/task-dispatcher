"""Shared fixtures for the pytest suite.

WHY THE $HOME REDIRECT IS AT MODULE SCOPE. `task_dispatcher.cli` resolves TASK_QUEUE_DIR,
MANIFEST_DIR, OAUTH_CRED_PATH and AUTH_ALERT_STAMP from $HOME at IMPORT time, and it loads
the agent roster at import time too — refusing to degrade to an empty one, because a tick
with an empty roster is how steward gets launched as the wrong user (vikunja#404). So $HOME
has to already point somewhere safe before the first `import task_dispatcher.cli` anywhere
in the session. conftest.py is imported before any test module, which makes this the only
place that ordering can be guaranteed.

The import is of the INSTALLED package, never of cli.py by path. The deployed artifact is a
package on a venv path; loading the file by path would test a shape that is never what runs.

WHAT THE FIXTURES DO NOT DO. Nothing here stubs a function under test. `isolate` redirects
the module's directory constants at a fresh tmp_path per test and neutralises the three
outbound channels that would otherwise reach the network (matrix_notify,
bus_log) plus subprocess.Popen. Tests that assert on those channels take the recorder
fixture for the one they care about, which re-stubs it with a capturing version — so a test
that forgets to record still cannot send anything.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

# --- $HOME redirect. Must precede the cli import below. ---
_TMP = tempfile.TemporaryDirectory()
_HOME = Path(_TMP.name)
(_HOME / ".claude" / "task-queue").mkdir(parents=True)
(_HOME / ".claude" / "manifests").mkdir(parents=True)
(_HOME / ".pm2" / "logs").mkdir(parents=True)
for _agent in ("developer", "security", "steward", "sysadmin", "writer", "research"):
    (_HOME / ".claude" / "projects" / _agent).mkdir(parents=True)
(_HOME / ".claude" / "comms" / "artifacts" / "audit-requests").mkdir(parents=True)
os.environ["HOME"] = str(_HOME)

# The roster is resolved from $HOME/scripts/agent-launch.yml at import and must exist.
# Use the repo's fixture copy, which exercises the real default resolution rather than the
# AGENT_LAUNCH_POLICY override. It is a FIXTURE COPY, never the live roster.
_REPO_ROOT = Path(__file__).resolve().parent.parent
(_HOME / "scripts").mkdir(parents=True)
(_HOME / "scripts" / "agent-launch.yml").write_text(
    (_REPO_ROOT / "tests" / "fixtures" / "agent-launch.yml").read_text()
)

import pytest  # noqa: E402

from task_dispatcher import cli as td  # noqa: E402

# The three standalone scripts predate this suite and are still run as their own CI steps,
# each with its own $HOME redirect and its own check() harness. Importing them here would
# run every one of their checks at collection time, against a $HOME they did not set up,
# and pytest would report neither the passes nor the failures. They are excluded from
# collection, NOT from CI — see .github/workflows/ci.yml.
collect_ignore = [
    "test_agent_launch_policy.py",
    "test_dispatcher_headless_chain.py",
    "test_version_no_roster.py",
    "test_gitleaks_gate.py",
    "test_task_queue_vocabulary.py",
    "test_bus_vocabulary.py",
    "test_bus_emitter_live.py",
]


# `td.subprocess` IS the stdlib subprocess module — the dispatcher does `import subprocess`,
# it does not hold a private copy. So patching td.subprocess.Popen patches it for the WHOLE
# process, including any test that wants to run a real child. Captured here before anything
# stubs it, and handed back by the `real_popen` fixture.
_REAL_POPEN = subprocess.Popen
# Same reasoning for the dispatcher's own outbound helpers: `isolate` replaces them for
# every test, so the one file that tests their real implementations has to get them back.
# Restoring by name is deliberate — `monkeypatch.undo()` would work, but it reverts the
# WHOLE fixture, putting TASK_QUEUE_DIR back on the shared home and silently un-isolating
# the test that called it.
_REAL_MATRIX_NOTIFY = td.matrix_notify
# pid_alive() reads /proc, i.e. the HOST's process table. Left real, every test that
# writes a run record would count as live-or-not depending on whether this machine
# happens to have a process at the stub's pid right now — the concurrency cap would pass
# or fail by coincidence. /proc is host state in exactly the sense the network is, so it
# is cut here for the same reason, and handed back by `real_pid_alive`.
_REAL_PID_ALIVE = td.pid_alive
# load_agent_env() reads /opt/appdata/agents/<agent>/.env — real files on this host, which
# is the second and less obvious credential channel `isolate` has to cut. The tests that
# are ABOUT that parser take it back through `real_load_agent_env`.
_REAL_LOAD_AGENT_ENV = td.load_agent_env


class FakeProc:
    """Stand-in for a Popen handle. Only `pid` is ever read by the dispatcher."""

    pid = 4242

    def __init__(self, argv=None, **kw):
        self.argv = argv
        self.returncode = 0


@pytest.fixture
def dispatcher():
    """The dispatcher module under test."""
    return td


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Point every filesystem constant at a fresh tmp_path and cut every outbound channel.

    Autouse on purpose. A test that forgets to isolate would read and WRITE the redirected
    HOME's shared queue, so failures would depend on test order — and a test that forgets
    to stub an outbound channel would try to reach matrix-mcp on localhost.
    """
    queue = tmp_path / "task-queue"
    queue.mkdir()
    monkeypatch.setattr(td, "TASK_QUEUE_DIR", queue)
    monkeypatch.setattr(td, "ARCHIVE_DIR", queue / "archive")
    monkeypatch.setattr(td, "DEAD_LETTER_DIR", queue / "dead-letters")
    monkeypatch.setattr(td, "AUTH_ALERT_STAMP", queue / ".auth-alert-stamp")
    # Run records. Without this every launch test writes into the module-scope $HOME and
    # the records outlive the test — which would silently make the concurrency cap the
    # NEXT test measures depend on how many launches ran before it.
    launches_dir = tmp_path / "task-launches"
    launches_dir.mkdir()
    monkeypatch.setattr(td, "LAUNCH_DIR", launches_dir)
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    monkeypatch.setattr(td, "MANIFEST_DIR", manifests)
    monkeypatch.setattr(td, "OAUTH_CRED_PATH", tmp_path / ".credentials.json")

    # Cut the credential supply a launch checks for. TWO CHANNELS, and the second is the
    # one that bites: this host has real /opt/appdata/agents/<agent>/.env files, and
    # load_agent_env() reads them into child_env before the bearer-token guard runs. A
    # test needing a launch therefore passed on this workstation and failed in CI, which
    # has neither the files nor the exported vars — in the worst direction for this
    # suite, because a launch that never happened is indistinguishable from a concurrency
    # cap that refused it.
    #
    # load_agent_env is stubbed rather than pointed at a tmp path: reading /opt/appdata is
    # host state in the same sense /proc and the network are, and this file cuts those too.
    # A test that WANTS a launch takes the `launchable` fixture, which is explicit and
    # cannot be satisfied by accident.
    for var in (
        "SCOPED_MCP_BEARER_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(td, "load_agent_env", lambda agent_type: {})

    monkeypatch.setattr(td, "matrix_notify", lambda room, title, body: None)
    monkeypatch.setattr(td, "bus_log", lambda *a, **k: None)
    monkeypatch.setattr(td.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(td, "pid_alive", lambda pid, start_ticks=None: False)
    return queue


@pytest.fixture
def queue(isolate):
    """The isolated task-queue directory."""
    return isolate


@pytest.fixture
def launchable(dispatcher, monkeypatch):
    """Satisfy the two credential guards, so a test reaches the logic it is about.

    NOT part of `isolate`, deliberately: three tests exist to prove those guards REFUSE a
    launch, and an autouse stub would quietly disarm them.

    It is here rather than copied into each file because leaving it local is how a test
    ends up depending on the developer's own shell. Tests written without it passed on a
    workstation that carries a real SCOPED_MCP_BEARER_TOKEN and failed in CI, which has
    none — a launch that never happened looks identical to a cap that refused it.
    """
    monkeypatch.setattr(dispatcher, "load_agent_env", lambda a: {"SCOPED_MCP_BEARER_TOKEN": "t"})
    monkeypatch.setattr(dispatcher, "anthropic_creds_usable", lambda env: True)


@pytest.fixture
def real_popen(isolate, monkeypatch):
    """Undo `isolate`'s Popen stub for a test that must run a real child process.

    Depends on `isolate` so it is torn down in the right order and, more importantly, so
    it always runs AFTER the stub is installed rather than racing it.
    """
    monkeypatch.setattr(td.subprocess, "Popen", _REAL_POPEN)


@pytest.fixture
def live_pids(isolate, monkeypatch):
    """Declare which pids are alive. Returns the mutable set the stub consults."""
    alive: set[int] = set()
    monkeypatch.setattr(td, "pid_alive", lambda pid, start_ticks=None: pid in alive)
    return alive


@pytest.fixture
def real_pid_alive(isolate, monkeypatch):
    """Restore the real pid_alive, for the tests that are about /proc itself."""
    monkeypatch.setattr(td, "pid_alive", _REAL_PID_ALIVE)


@pytest.fixture
def real_load_agent_env(isolate, monkeypatch):
    """Restore the real load_agent_env, for the tests that are about the parser itself."""
    monkeypatch.setattr(td, "load_agent_env", _REAL_LOAD_AGENT_ENV)


@pytest.fixture
def real_matrix_notify(isolate, monkeypatch):
    """Restore the real matrix_notify. Its transport must still be stubbed by the test."""
    monkeypatch.setattr(td, "matrix_notify", _REAL_MATRIX_NOTIFY)


@pytest.fixture
def launches(monkeypatch):
    """Capture what WOULD have been launched. Popen is recorded, never executed."""
    captured: list[dict] = []

    def _fake_popen(argv, cwd=None, stdout=None, stderr=None, env=None, **kw):
        captured.append({"argv": argv, "cwd": cwd, "env": dict(env or {})})
        return FakeProc(argv)

    monkeypatch.setattr(td.subprocess, "Popen", _fake_popen)
    return captured


@pytest.fixture
def notifications(monkeypatch):
    """Capture matrix_notify calls as (room, title, body)."""
    captured: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        td, "matrix_notify", lambda room, title, body: captured.append((room, title, body))
    )
    return captured


@pytest.fixture
def bus(monkeypatch):
    """Capture bus_log calls as (event_type, kwargs)."""
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(td, "bus_log", lambda event_type, **kw: captured.append((event_type, kw)))
    return captured


@pytest.fixture
def audit_root(monkeypatch, tmp_path):
    """A tmp ~/.claude/comms/artifacts/audit-requests, via a redirected Path.home().

    launch_security_audit calls Path.home() at call time rather than reading a module
    constant, so this redirects HOME for the duration of the test. Returns the resolved
    audit-requests root — resolved, because the containment check compares resolved paths
    and on macOS /tmp is a symlink.
    """
    home = tmp_path / "home"
    root = home / ".claude" / "comms" / "artifacts" / "audit-requests"
    root.mkdir(parents=True)
    (home / ".claude" / "projects" / "security").mkdir(parents=True)
    (home / ".pm2" / "logs").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return root.resolve()


@pytest.fixture
def write_task(queue):
    """Write a queue file the way submit_task would have. Returns (path, task)."""
    counter = [0]

    def _write(**kw):
        counter[0] += 1
        n = counter[0]
        task = {
            "id": f"{n:08d}-0000-4000-8000-000000000000",
            "created": td.now_iso(),
            "source_agent": "research",
            "target_agent": "developer",
            "task_type": "build",
            "risk_level": "low",
            "requires_approval": False,
            "workflow_mode": "semi-auto",
            "status": "submitted",
            "summary": f"test task {n}",
            "ttl_days": 30,
            "payload": {},
        }
        task.update(kw)
        path = queue / f"task-{n:03d}.yml"
        td.atomic_write(path, task)
        return path, task

    return _write
