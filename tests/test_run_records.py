"""Tests for the run record — the entity Phase 3 added because a launch was a side effect.

The properties asserted here are the ones the build exists for, so they are written as
behaviour and not as shape:

  * a launch produces a record beside its log, and does NOT change the log's name
  * the record carries a pid that can be checked later, and a start time that survives
    pid reuse
  * a dead run is reaped with a NULL exit code and an explicit reason — never a zero
  * a live-but-overrunning run releases its slot and its task is left alone

WHAT IS DELIBERATELY NOT ASSERTED: that a real `claude -p` session's pid appears in
/proc. Popen is stubbed for every test in this suite, so the pid in a record here is the
stub's. The /proc parse itself is covered separately against this test process's own pid,
which is the only pid a test can know is alive.
"""

from __future__ import annotations

import json
import os

import pytest


@pytest.fixture
def task(write_task):
    path, t = write_task(target_agent="developer", workflow_mode="auto", requires_approval=False)
    return path, t


def _boom(*a, **kw):
    raise RuntimeError("nope")


def _boom_os(*a, **kw):
    raise OSError("nope")


def _records(dispatcher):
    return sorted(p.name for p in dispatcher.LAUNCH_DIR.glob("*.json"))


def _logs(dispatcher):
    return sorted(p.name for p in dispatcher.LAUNCH_DIR.glob("*.log"))


# --- The filename convention -------------------------------------------------


def test_the_record_is_a_sibling_of_the_log_not_a_replacement(
    dispatcher, launches, launchable, task
):
    """The `.log` name is load-bearing in two other repos; the record takes the same stem.

    The plugin's parseLaunchLogName() and the task-launches retention job (vikunja#545)
    both key on `<agent>-<task8>.log`. If this build had renamed the log to make room for
    a record, both would have silently stopped matching.
    """
    _, t = task
    dispatcher.launch_agent_headless(t)
    assert _logs(dispatcher) == ["developer-00000001.log"]
    assert _records(dispatcher) == ["developer-00000001.json"]


def test_launch_log_name_is_the_only_producer(dispatcher):
    """One spelling of the convention, so the reader in the other repo has a target."""
    assert dispatcher.launch_log_name("steward", "abcdef12-3456-4789-8abc-def012345678") == (
        "steward-abcdef12.log"
    )
    assert dispatcher.run_record_name("steward", "abcdef12-3456-4789-8abc-def012345678") == (
        "steward-abcdef12.json"
    )


# --- What the record carries -------------------------------------------------


def test_a_launch_records_the_pid_and_the_task_it_serves(dispatcher, launches, launchable, task):
    _, t = task
    dispatcher.launch_agent_headless(t)
    rec = json.loads((dispatcher.LAUNCH_DIR / "developer-00000001.json").read_text())
    assert rec["pid"] == 4242
    assert rec["task_id"] == t["id"]
    assert rec["agent"] == "developer"
    assert rec["launched_by"] == "dispatcher"
    assert rec["workflow_mode"] == "auto"
    assert rec["log_path"].endswith("developer-00000001.log")
    # Open, not finished. Nothing has observed this run end.
    assert rec["ended"] is None
    assert rec["exit_code"] is None


def test_the_child_is_told_which_run_it_is(dispatcher, launches, launchable, task):
    """FORGE_RUN_ID/FORGE_TASK_ID are what make a Langfuse trace joinable to a task."""
    _, t = task
    dispatcher.launch_agent_headless(t)
    env = launches[0]["env"]
    rec = json.loads((dispatcher.LAUNCH_DIR / "developer-00000001.json").read_text())
    assert env["FORGE_RUN_ID"] == rec["run_id"]
    assert env["FORGE_TASK_ID"] == t["id"]
    # And the mode is still there — the new vars are added beside it, not instead of it.
    assert env["FORGE_WORKFLOW_MODE"] == "auto"


def test_the_run_id_in_the_env_is_the_one_in_the_record(dispatcher, launches, launchable, task):
    """The env is built BEFORE Popen and the record written after; they must still agree.

    A regression that minted the id inside write_run_record would leave the session
    reporting one run id and the record holding another — and nothing would fail.
    """
    _, t = task
    dispatcher.launch_agent_headless(t)
    rec = json.loads((dispatcher.LAUNCH_DIR / "developer-00000001.json").read_text())
    assert launches[0]["env"]["FORGE_RUN_ID"] == rec["run_id"]


def test_an_audit_launch_is_recorded_too(dispatcher, monkeypatch, launches, audit_root, write_task):
    """The third launcher. Uncounted, the concurrency cap would be a cap in name only.

    An audit launches headlessly even in semi-auto, which makes it the commonest session
    type on this host.
    """
    monkeypatch.setattr(dispatcher, "anthropic_creds_usable", lambda env: True)
    monkeypatch.setattr(dispatcher, "load_agent_env", lambda a: {"SCOPED_MCP_BEARER_TOKEN": "x"})
    (audit_root / "some-build").mkdir()
    (audit_root / "some-build" / "request.md").write_text("audit me")
    path, t = write_task(
        target_agent="security",
        task_type="audit",
        payload={"request": str(audit_root / "some-build" / "request.md")},
    )
    assert dispatcher.launch_security_audit(path, t) is True
    rec = json.loads((dispatcher.LAUNCH_DIR / f"security-{t['id'][:8]}.json").read_text())
    assert rec["agent"] == "security"
    assert rec["launched_by"] == "dispatcher-audit"
    # Its log lives outside the launch directory, and the record says where.
    assert rec["log_path"].endswith("security-audit-some-build.log")
    assert "task-launches" not in rec["log_path"]


# --- /proc liveness ----------------------------------------------------------


def test_pid_start_ticks_reads_this_process(dispatcher, real_pid_alive):
    """The only pid a test can know is alive is its own."""
    ticks = dispatcher.pid_start_ticks(os.getpid())
    assert isinstance(ticks, int) and ticks > 0
    assert dispatcher.pid_alive(os.getpid(), ticks) is True


def test_a_pid_whose_start_time_disagrees_is_not_our_process(dispatcher, real_pid_alive):
    """Pid reuse. Without this, a recycled pid holds a concurrency slot forever.

    A cap that has silently stopped issuing launches is indistinguishable from a quiet
    queue, which is why this is checked rather than trusted.
    """
    ticks = dispatcher.pid_start_ticks(os.getpid())
    assert dispatcher.pid_alive(os.getpid(), ticks + 1) is False


def test_a_record_with_no_start_ticks_falls_back_to_presence(dispatcher, real_pid_alive):
    """Records written before the field existed must still be reapable, not immortal."""
    assert dispatcher.pid_alive(os.getpid(), None) is True


@pytest.mark.parametrize("pid", [None, 0, -1, "4242", True])
def test_a_nonsense_pid_is_not_alive(dispatcher, real_pid_alive, pid):
    """`True` is in this list on purpose: bool is an int subclass, and os.kill(True, 0)
    would signal pid 1."""
    assert dispatcher.pid_alive(pid) is False


# --- Reaping -----------------------------------------------------------------


def _write_record(dispatcher, **kw):
    rec = {
        "run_id": "r-1",
        "task_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        "agent": "developer",
        "launched_by": "dispatcher",
        "run_as_user": None,
        "launcher": None,
        "workflow_mode": "auto",
        "started": dispatcher.now_iso(),
        "pid": 4242,
        "pid_start_ticks": 99,
        "ended": None,
        "exit_code": None,
        "log_path": "/tmp/x.log",
    }
    rec.update(kw)
    dispatcher.LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    path = dispatcher.LAUNCH_DIR / f"{rec['agent']}-{rec['task_id'][:8]}.json"
    dispatcher.atomic_write_json(path, rec)
    return path


def test_a_dead_run_is_reaped_with_a_null_exit_code(dispatcher, live_pids):
    """THE central assertion of this build. A fabricated zero is the failure it exists
    to stop: a counter reporting success for something nobody observed succeed."""
    path = _write_record(dispatcher)
    dead = dispatcher.reap_runs()
    assert len(dead) == 1
    rec = json.loads(path.read_text())
    assert rec["ended"] is not None
    assert rec["exit_code"] is None
    assert rec["reaped"] == "pid-gone"


def test_a_live_run_is_left_open(dispatcher, live_pids):
    live_pids.add(4242)
    path = _write_record(dispatcher)
    assert dispatcher.reap_runs() == []
    assert json.loads(path.read_text())["ended"] is None


def test_an_already_ended_run_is_not_reaped_twice(dispatcher, live_pids):
    path = _write_record(dispatcher, ended="2026-08-01T00:00:00+00:00", reaped="pid-gone")
    assert dispatcher.reap_runs() == []
    assert json.loads(path.read_text())["ended"] == "2026-08-01T00:00:00+00:00"


def test_an_overrunning_live_run_releases_its_slot_but_is_not_called_dead(
    dispatcher, live_pids, monkeypatch
):
    """max-runtime frees the slot and NOTHING else.

    Reporting it dead would sweep the task of an agent that is demonstrably still
    working — and would additionally strand it, because `completed` is only reachable
    from `in-progress`, so the agent's own close would then be refused.
    """
    live_pids.add(4242)
    monkeypatch.setattr(dispatcher, "MAX_RUN_SECONDS", 1)
    path = _write_record(dispatcher, started="2020-01-01T00:00:00+00:00")
    dead = dispatcher.reap_runs()
    assert dead == []  # not offered to the sweep
    rec = json.loads(path.read_text())
    assert rec["reaped"] == "max-runtime"  # but closed, so the slot is free
    assert rec["exit_code"] is None
    assert dispatcher.live_runs() == []


def test_max_run_seconds_of_zero_disables_the_backstop(dispatcher, live_pids, monkeypatch):
    live_pids.add(4242)
    monkeypatch.setattr(dispatcher, "MAX_RUN_SECONDS", 0)
    path = _write_record(dispatcher, started="2020-01-01T00:00:00+00:00")
    assert dispatcher.reap_runs() == []
    assert json.loads(path.read_text())["ended"] is None


def test_an_unparseable_start_time_does_not_reap_a_live_run(dispatcher, live_pids, monkeypatch):
    """An unreadable age is not evidence of an overrun."""
    live_pids.add(4242)
    monkeypatch.setattr(dispatcher, "MAX_RUN_SECONDS", 1)
    path = _write_record(dispatcher, started="not-a-timestamp")
    assert dispatcher.reap_runs() == []
    assert json.loads(path.read_text())["ended"] is None


def test_a_corrupt_record_is_skipped_not_fatal(dispatcher, live_pids):
    dispatcher.LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    (dispatcher.LAUNCH_DIR / "developer-deadbeef.json").write_text("{not json")
    _write_record(dispatcher)
    assert len(dispatcher.reap_runs()) == 1


def test_a_json_array_is_not_a_record(dispatcher, live_pids):
    """json.loads succeeds on a list; .get() on one does not."""
    dispatcher.LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    (dispatcher.LAUNCH_DIR / "developer-deadbeef.json").write_text("[1, 2]")
    assert dispatcher.reap_runs() == []


def test_a_missing_launch_directory_is_an_empty_list(dispatcher):
    """A first-ever tick has no launch directory; that is an empty list, not a crash."""
    dispatcher.LAUNCH_DIR.rmdir()
    assert not dispatcher.LAUNCH_DIR.exists()
    assert dispatcher.iter_run_records() == []
    assert dispatcher.live_runs() == []
    assert dispatcher.reap_runs() == []


# --- The run-as channel: sudo scrubs the env, so the flag is the only way in ------


@pytest.fixture
def steward_launch(dispatcher, monkeypatch, tmp_path, launches, launchable):
    """A steward launch with a fake launcher whose contents the test controls."""
    launcher = tmp_path / "run-steward.sh"

    def _go(launcher_text, task_id="bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"):
        launcher.write_text(launcher_text)
        launcher.chmod(0o755)
        monkeypatch.setitem(dispatcher.AGENT_RUN_AS, "steward", ("agent-steward", str(launcher)))
        dispatcher.launch_agent_headless(
            {"id": task_id, "target_agent": "steward", "workflow_mode": "auto"}
        )
        return launches[0]["argv"]

    return _go


def test_the_run_id_flag_is_passed_when_the_deployed_launcher_takes_it(steward_launch):
    argv = steward_launch("#!/bin/bash\ncase $1 in --workflow-mode|--run-id|--task-id) ;; esac\n")
    assert "--run-id" in argv and "--task-id" in argv
    # Still after --workflow-mode and still before the `--` separator, so the prompt
    # cannot be parsed as a flag.
    assert argv.index("--run-id") > argv.index("--workflow-mode")
    assert argv.index("--run-id") < argv.index("--")


def test_the_flag_is_withheld_from_a_launcher_that_has_not_learned_it(steward_launch, dispatcher):
    """The two artefacts deploy separately and are out of step TODAY.

    run-steward.sh refuses an unknown option outright, so passing the flag ahead of the
    redeploy would not degrade — it would kill every steward launch. The record is still
    written; only the id in the child's environment is missing.
    """
    argv = steward_launch("#!/bin/bash\ncase $1 in --workflow-mode) ;; esac\n")
    assert "--run-id" not in argv
    assert argv[:5] == ["sudo", "-n", "-u", "agent-steward", argv[4]]
    assert argv[-2:] == ["--", argv[-1]]
    assert [q.name for q in dispatcher.LAUNCH_DIR.glob("steward-*.json")] == [
        "steward-bbbbbbbb.json"
    ]


def test_a_run_as_launch_still_records_its_user_and_launcher(steward_launch, dispatcher):
    steward_launch("#!/bin/bash\n--run-id\n")
    rec = json.loads((dispatcher.LAUNCH_DIR / "steward-bbbbbbbb.json").read_text())
    assert rec["run_as_user"] == "agent-steward"
    assert rec["launcher"].endswith("run-steward.sh")
    assert rec["agent"] == "steward"


def test_a_direct_launch_records_no_run_as_user(dispatcher, launches, launchable, write_task):
    _, t = write_task(target_agent="developer", workflow_mode="auto")
    dispatcher.launch_agent_headless(t)
    rec = json.loads((dispatcher.LAUNCH_DIR / "developer-00000001.json").read_text())
    assert rec["run_as_user"] is None and rec["launcher"] is None


def test_launcher_accepts_reads_the_deployed_file(dispatcher, tmp_path):
    """Reading the artefact that will run is the point — a constant in this repo would
    only ever assert something about this repo."""
    p = tmp_path / "l.sh"
    p.write_text("case $1 in --workflow-mode) ;; esac")
    assert dispatcher.launcher_accepts(str(p), "--workflow-mode") is True
    assert dispatcher.launcher_accepts(str(p), "--run-id") is False


def test_an_unreadable_launcher_is_treated_as_not_accepting(dispatcher, tmp_path):
    """Fail towards the older contract, which every deployed launcher satisfies."""
    assert dispatcher.launcher_accepts(str(tmp_path / "gone.sh"), "--run-id") is False


# --- The shared .env parser --------------------------------------------------


def test_read_env_file(dispatcher, tmp_path):
    p = tmp_path / "x.env"
    p.write_text("# a comment\n\nA=1\nB=\"two\"\nC='three'\nnot a pair\nD=has=equals\n")
    assert dispatcher.read_env_file(p) == {
        "A": "1",
        "B": "two",
        "C": "three",
        "D": "has=equals",
    }


def test_read_env_file_missing_is_empty(dispatcher, tmp_path):
    assert dispatcher.read_env_file(tmp_path / "nope.env") == {}


def test_load_agent_env_still_uses_it(dispatcher, monkeypatch, tmp_path, real_load_agent_env):
    """One parser, two callers — the refactor must not have left load_agent_env behind."""
    seen = []
    monkeypatch.setattr(dispatcher, "read_env_file", lambda p: seen.append(p) or {"K": "V"})
    assert dispatcher.load_agent_env("security") == {"K": "V"}
    assert str(seen[0]) == "/opt/appdata/agents/security/.env"


# --- Degradation paths -------------------------------------------------------
#
# Each of these is a case where the dispatcher must keep ticking. The tick runs every two
# minutes against a live queue for six agents; a raised exception here does not just lose
# one run record, it stops every other task in the same pass.


def test_a_failed_record_write_does_not_stop_the_launch(
    dispatcher, monkeypatch, launches, launchable, task
):
    """The session is already running by the time the record is written.

    Raising here would abort process_submitted mid-pass, after the child was spawned —
    losing the record AND every task queued behind it.
    """
    _, t = task
    monkeypatch.setattr(dispatcher, "atomic_write_json", _boom)
    dispatcher.launch_agent_headless(t)
    assert len(launches) == 1
    assert _records(dispatcher) == []


def test_atomic_write_json_leaves_no_tmp_file_behind(dispatcher, monkeypatch):
    """A partial record is worse than none: it would read as a run with a null pid."""
    monkeypatch.setattr(dispatcher.json, "dump", _boom)
    target = dispatcher.LAUNCH_DIR / "developer-00000001.json"
    with pytest.raises(RuntimeError):
        dispatcher.atomic_write_json(target, {"a": 1})
    assert list(dispatcher.LAUNCH_DIR.iterdir()) == []


def test_an_unreadable_launch_directory_is_an_empty_list(dispatcher, monkeypatch):
    monkeypatch.setattr(type(dispatcher.LAUNCH_DIR), "glob", _boom_os)
    assert dispatcher.iter_run_records() == []


def test_a_malformed_proc_stat_is_not_a_live_pid(dispatcher, real_pid_alive, monkeypatch, tmp_path):
    """Field 22 missing or non-numeric. Guessing 'alive' would make the run immortal and
    its slot unrecoverable."""
    import builtins

    real_open = builtins.open

    def fake_open(path, *a, **kw):
        if str(path).startswith("/proc/"):
            return real_open(tmp_path / "stat", *a, **kw)
        return real_open(path, *a, **kw)

    (tmp_path / "stat").write_bytes(b"1 (sh) S 0 0\n")
    monkeypatch.setattr(builtins, "open", fake_open)
    assert dispatcher.pid_start_ticks(1) is None
    assert dispatcher.pid_alive(1, 5) is False


def test_naive_timestamps_are_read_as_utc(dispatcher, live_pids, monkeypatch):
    """A record or stamp written without an offset must not raise on comparison.

    datetime.now(UTC) - a naive datetime is a TypeError, which in the auth stamp's case
    would abort the tick.
    """
    monkeypatch.setattr(dispatcher, "MAX_RUN_SECONDS", 1)
    live_pids.add(4242)
    path = _write_record(dispatcher, started="2020-01-01T00:00:00")
    dispatcher.reap_runs()
    assert json.loads(path.read_text())["reaped"] == "max-runtime"

    dispatcher.AUTH_ALERT_STAMP.write_text("2020-01-01T00:00:00")
    assert dispatcher.auth_outage_active() is False
