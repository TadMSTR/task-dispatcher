"""Tests for the concurrency caps, the auth-outage hold and the operator sweep (Phase 4).

The property that matters most here is a NEGATIVE one: a task that cannot get a launch
slot must still be `submitted` afterwards. Not a new status, not `approved`-but-unlaunched
— `submitted`, byte-identical to how it arrived, so the next tick reads it from scratch.
A new status would have to be added to task-queue-mcp and the plugin's vocabulary gate in
that order, and adding it on one side only is the drift this whole programme exists to
close.

The sweep tests assert the ROUTE as well as the outcome. `failed` reached by writing the
queue file directly and `failed` reached through the operator route look the same in a
status field and completely different in history — one reads as a sweep years later, the
other as the agent having quietly closed its own work.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def api(dispatcher, monkeypatch):
    """Capture control-API posts, with a secret in place so the guard passes."""
    calls: list[dict] = []

    class _Resp:
        status_code = 200
        text = "{}"

    def _post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "body": json, "headers": headers})
        return _Resp()

    monkeypatch.setattr(dispatcher, "task_queue_api_secret", lambda: "s3cret")
    monkeypatch.setattr(dispatcher.httpx, "post", _post)
    return calls


def _record(dispatcher, agent, pid, task_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", **kw):
    rec = {
        "run_id": f"run-{pid}",
        "task_id": task_id,
        "agent": agent,
        "launched_by": "dispatcher",
        "run_as_user": None,
        "launcher": None,
        "workflow_mode": "auto",
        "started": dispatcher.now_iso(),
        "pid": pid,
        "pid_start_ticks": 1,
        "ended": None,
        "exit_code": None,
        "log_path": "/tmp/x.log",
    }
    rec.update(kw)
    dispatcher.LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    dispatcher.atomic_write_json(dispatcher.LAUNCH_DIR / f"{agent}-{pid}.json", rec)
    return rec


# --- launch_kind: one decision, two readers ----------------------------------


@pytest.mark.parametrize(
    "task,agent,expected",
    [
        ({"task_type": "workflow"}, "developer", "temporal"),
        ({"task_type": "audit"}, "security", "audit"),
        ({"task_type": "audit"}, "developer", "operator-pickup"),
        ({"task_type": "build", "workflow_mode": "auto"}, "developer", "headless"),
        ({"task_type": "build", "workflow_mode": "semi-auto"}, "developer", "operator-pickup"),
        ({"task_type": "build"}, "developer", "operator-pickup"),
    ],
)
def test_launch_kind(dispatcher, task, agent, expected):
    assert dispatcher.launch_kind(task, agent) == expected


def test_manual_then_auto_is_not_a_headless_launch(dispatcher):
    """The one edit that would destroy this mode's only property.

    `manual-then-auto` gates its own leg and nothing else. An `!= "semi-auto"` test here
    reads like a tidy-up and silently auto-launches it.
    """
    assert (
        dispatcher.launch_kind(
            {"task_type": "build", "workflow_mode": "manual-then-auto"}, "developer"
        )
        == "operator-pickup"
    )


# --- The caps ----------------------------------------------------------------


def test_no_live_runs_means_a_slot(dispatcher, live_pids):
    assert dispatcher.slot_denial("developer") is None


def test_the_global_cap_holds(dispatcher, live_pids, monkeypatch):
    monkeypatch.setattr(dispatcher, "MAX_CONCURRENT_RUNS", 2)
    monkeypatch.setattr(dispatcher, "MAX_RUNS_PER_AGENT", 0)
    for pid, agent in ((11, "writer"), (12, "sysadmin")):
        _record(dispatcher, agent, pid)
        live_pids.add(pid)
    denial = dispatcher.slot_denial("developer")
    assert denial is not None and "global cap" in denial


def test_the_per_agent_cap_holds_while_the_global_one_has_room(dispatcher, live_pids, monkeypatch):
    monkeypatch.setattr(dispatcher, "MAX_CONCURRENT_RUNS", 8)
    monkeypatch.setattr(dispatcher, "MAX_RUNS_PER_AGENT", 1)
    _record(dispatcher, "developer", 11)
    live_pids.add(11)
    assert "per-agent cap" in dispatcher.slot_denial("developer")
    assert dispatcher.slot_denial("writer") is None


def test_a_dead_run_does_not_hold_a_slot(dispatcher, live_pids, monkeypatch):
    """The record is on disk and open, but its process is gone. It must not count."""
    monkeypatch.setattr(dispatcher, "MAX_RUNS_PER_AGENT", 1)
    _record(dispatcher, "developer", 11)  # live_pids is empty: pid 11 is dead
    assert dispatcher.slot_denial("developer") is None


def test_an_ended_run_does_not_hold_a_slot(dispatcher, live_pids, monkeypatch):
    monkeypatch.setattr(dispatcher, "MAX_RUNS_PER_AGENT", 1)
    _record(dispatcher, "developer", 11, ended="2026-01-01T00:00:00+00:00")
    live_pids.add(11)
    assert dispatcher.slot_denial("developer") is None


@pytest.mark.parametrize("cap", [0, -1])
def test_a_non_positive_cap_is_unlimited(dispatcher, live_pids, monkeypatch, cap):
    """The documented escape hatch for draining a queue by hand."""
    monkeypatch.setattr(dispatcher, "MAX_CONCURRENT_RUNS", cap)
    monkeypatch.setattr(dispatcher, "MAX_RUNS_PER_AGENT", cap)
    for pid in range(20, 30):
        _record(dispatcher, "developer", pid)
        live_pids.add(pid)
    assert dispatcher.slot_denial("developer") is None


def test_int_env_survives_a_typo(dispatcher, monkeypatch):
    """A bad crontab value must not take down every tick before a task is read."""
    monkeypatch.setenv("DISPATCHER_TEST_CAP", "four")
    assert dispatcher._int_env("DISPATCHER_TEST_CAP", 4) == 4
    monkeypatch.setenv("DISPATCHER_TEST_CAP", "  ")
    assert dispatcher._int_env("DISPATCHER_TEST_CAP", 4) == 4
    monkeypatch.setenv("DISPATCHER_TEST_CAP", "7")
    assert dispatcher._int_env("DISPATCHER_TEST_CAP", 4) == 7


# --- The gate, end to end through a tick -------------------------------------


def test_n_plus_2_auto_tasks_against_a_cap_of_n(
    dispatcher, launches, live_pids, monkeypatch, write_task
):
    """The plan's own verification, as a test.

    Six auto tasks for six different agents, a global cap of four: four launch and two
    are held. The two that are held are still `submitted` — that is the assertion this
    file exists for.
    """
    monkeypatch.setattr(dispatcher, "MAX_CONCURRENT_RUNS", 4)
    monkeypatch.setattr(dispatcher, "MAX_RUNS_PER_AGENT", 0)
    # Every launch this tick makes must count against the next one, so liveness follows
    # the stub's pid rather than being fixed up front.
    live_pids.add(4242)
    agents = ["developer", "sysadmin", "writer", "research", "security", "steward"]
    paths = [write_task(target_agent=a, workflow_mode="auto")[0] for a in agents]

    dispatcher.process_submitted({})

    assert len(launches) == 4
    statuses = [dispatcher.load_yaml(p)["status"] for p in paths]
    assert statuses.count("approved") == 4
    assert statuses.count("submitted") == 2


def test_a_held_task_is_untouched_not_annotated(
    dispatcher, launches, live_pids, monkeypatch, write_task
):
    """No new status, and no history entry either.

    A history line per held task per tick would be a queue file growing a row every two
    minutes for as long as the cap is full.
    """
    monkeypatch.setattr(dispatcher, "MAX_CONCURRENT_RUNS", 0)
    monkeypatch.setattr(dispatcher, "MAX_RUNS_PER_AGENT", 1)
    live_pids.add(11)
    _record(dispatcher, "developer", 11)
    path, _ = write_task(target_agent="developer", workflow_mode="auto")
    before = path.read_bytes()

    dispatcher.process_submitted({})

    assert launches == []
    assert path.read_bytes() == before


def test_a_semi_auto_task_is_never_held(dispatcher, launches, live_pids, monkeypatch, write_task):
    """The cap counts sessions. An operator-pickup task starts none, so it must not queue
    behind one."""
    monkeypatch.setattr(dispatcher, "MAX_CONCURRENT_RUNS", 1)
    live_pids.add(11)
    _record(dispatcher, "developer", 11)
    path, _ = write_task(target_agent="developer", workflow_mode="semi-auto")

    dispatcher.process_submitted({})

    assert dispatcher.load_yaml(path)["status"] == "approved"


def test_an_auth_outage_holds_launches_at_submitted(
    dispatcher, launches, live_pids, monkeypatch, write_task
):
    """alert_auth_blocked() debounced the alert but not the attempt, so a dead OAuth sent
    every queued task to routing-failed with a backoff. Holding costs nothing."""
    dispatcher.AUTH_ALERT_STAMP.write_text(dispatcher.now_iso())
    path, _ = write_task(target_agent="developer", workflow_mode="auto")

    dispatcher.process_submitted({})

    assert launches == []
    assert dispatcher.load_yaml(path)["status"] == "submitted"


def test_a_stale_auth_stamp_does_not_hold_launches(
    dispatcher, launches, live_pids, monkeypatch, write_task
):
    monkeypatch.setattr(dispatcher, "AUTH_ALERT_DEBOUNCE_SEC", 1)
    dispatcher.AUTH_ALERT_STAMP.write_text("2020-01-01T00:00:00+00:00")
    write_task(target_agent="developer", workflow_mode="auto")

    dispatcher.process_submitted({})

    assert len(launches) == 1


def test_an_unreadable_auth_stamp_is_not_an_outage(dispatcher):
    """Three cases collapse to 'not a known-live outage': never, unreadable, expired."""
    assert dispatcher.auth_outage_active() is False
    dispatcher.AUTH_ALERT_STAMP.write_text("garbage")
    assert dispatcher.auth_outage_active() is False


# --- The sweep ---------------------------------------------------------------


def test_a_dead_run_sweeps_its_task_through_the_operator_route(
    dispatcher, live_pids, api, write_task
):
    _, t = write_task(target_agent="developer", status="in-progress")
    rec = _record(dispatcher, "developer", 11, task_id=t["id"])

    dispatcher.sweep_dead_runs([rec])

    assert len(api) == 1
    assert api[0]["url"].endswith(f"/tasks/{t['id']}/update")
    assert api[0]["body"]["status"] == "failed"
    # on_behalf_of is what makes the history read as a sweep rather than as the agent
    # closing its own work. Its absence is the whole difference.
    assert api[0]["body"]["on_behalf_of"] == "developer"
    assert api[0]["headers"]["X-Task-Queue-Secret"] == "s3cret"
    # And the note says the exit code is unknown rather than implying one.
    assert "Exit code unknown" in api[0]["body"]["note"]


def test_the_sweep_never_writes_the_queue_file_itself(
    dispatcher, live_pids, monkeypatch, write_task
):
    """With no secret the task is LEFT ALONE, byte for byte.

    A direct atomic_write() here would be indistinguishable in history from the agent
    having closed its own work — the dishonest close the operator route was built to
    replace — and the dispatcher already holds atomic_write(), so nothing but this
    assertion stops one being added. Compared as bytes rather than by status, because a
    fallback that wrote `failed` and a fallback that only appended a history line are
    both the same mistake.
    """
    monkeypatch.setattr(dispatcher, "task_queue_api_secret", lambda: "")
    path, t = write_task(target_agent="developer", status="in-progress")
    rec = _record(dispatcher, "developer", 11, task_id=t["id"])
    before = path.read_bytes()

    dispatcher.sweep_dead_runs([rec])

    assert path.read_bytes() == before
    assert dispatcher.control_api_update(t["id"], "failed", "n", "developer") is False


def test_a_refused_sweep_leaves_the_task_alone(dispatcher, live_pids, monkeypatch, write_task):
    class _Resp:
        status_code = 409
        text = "Invalid transition"

    monkeypatch.setattr(dispatcher, "task_queue_api_secret", lambda: "s3cret")
    monkeypatch.setattr(dispatcher.httpx, "post", lambda *a, **k: _Resp())
    path, t = write_task(target_agent="developer", status="in-progress")
    rec = _record(dispatcher, "developer", 11, task_id=t["id"])

    dispatcher.sweep_dead_runs([rec])

    assert dispatcher.load_yaml(path)["status"] == "in-progress"


def test_an_unreachable_control_api_is_not_fatal(dispatcher, live_pids, monkeypatch, write_task):
    import httpx as _httpx

    monkeypatch.setattr(dispatcher, "task_queue_api_secret", lambda: "s3cret")

    def _boom(*a, **k):
        raise _httpx.ConnectError("refused")

    monkeypatch.setattr(dispatcher.httpx, "post", _boom)
    path, t = write_task(target_agent="developer", status="in-progress")
    dispatcher.sweep_dead_runs([_record(dispatcher, "developer", 11, task_id=t["id"])])
    assert dispatcher.load_yaml(path)["status"] == "in-progress"


@pytest.mark.parametrize("status", ["completed", "approved", "failed", "cancelled", "parked"])
def test_only_an_in_progress_task_is_swept(dispatcher, live_pids, api, write_task, status):
    """A task the agent already closed, or never claimed, has nothing to sweep."""
    _, t = write_task(target_agent="developer", status=status)
    dispatcher.sweep_dead_runs([_record(dispatcher, "developer", 11, task_id=t["id"])])
    assert api == []


def test_a_run_for_an_unknown_task_sweeps_nothing(dispatcher, live_pids, api):
    dispatcher.sweep_dead_runs([_record(dispatcher, "developer", 11)])
    assert api == []


def test_a_malformed_task_id_never_reaches_a_url(dispatcher, live_pids, api, write_task):
    """The id is interpolated into a request path; a loose one must not get there.

    The task is written with the malformed id AND in-progress on purpose: without it,
    find_task_by_id() returns None and the sweep stops for an unrelated reason, so the
    test would pass with the id guard deleted.
    """
    bad = "../../etc/passwd"
    write_task(id=bad, target_agent="developer", status="in-progress")
    assert dispatcher.find_task_by_id(bad) is not None  # the guard is the only thing left
    dispatcher.sweep_dead_runs([_record(dispatcher, "developer", 11, task_id=bad)])
    assert api == []


def test_the_existing_backlog_is_unreachable_from_here(dispatcher, live_pids, api, write_task):
    """The 13 in-progress tasks predating this build have no run record.

    Nothing in the sweep enumerates tasks; it enumerates DEAD RUNS and looks their task
    up. A task with no run record is therefore structurally out of reach — which is what
    makes 'do not bulk-close the backlog' a property of the code and not a promise.
    """
    write_task(target_agent="developer", status="in-progress")
    dispatcher.sweep_dead_runs(dispatcher.reap_runs())
    assert api == []


def test_reap_then_sweep_is_one_pass(dispatcher, live_pids, api, write_task):
    """The wiring main() uses: reap produces the dead list, the sweep consumes it."""
    _, t = write_task(target_agent="developer", status="in-progress")
    _record(dispatcher, "developer", 11, task_id=t["id"])

    dispatcher.sweep_dead_runs(dispatcher.reap_runs())

    assert len(api) == 1
    rec = json.loads((dispatcher.LAUNCH_DIR / "developer-11.json").read_text())
    assert rec["reaped"] == "pid-gone" and rec["exit_code"] is None


# --- The secret ---------------------------------------------------------------


def test_the_secret_comes_from_the_environment_first(dispatcher, monkeypatch, tmp_path):
    monkeypatch.setenv("TASK_QUEUE_API_SECRET", "from-env")
    monkeypatch.setattr(dispatcher, "SECRET_FILES", ())
    assert dispatcher.task_queue_api_secret() == "from-env"


def test_the_secret_falls_back_to_a_file(dispatcher, monkeypatch, tmp_path):
    """The dispatcher runs from a bare crontab line, so a file is the normal source."""
    monkeypatch.delenv("TASK_QUEUE_API_SECRET", raising=False)
    absent = tmp_path / "nope.env"
    present = tmp_path / "forge.env"
    present.write_text('# comment\nTASK_QUEUE_API_SECRET="from-file"\nOTHER=x\n')
    monkeypatch.setattr(dispatcher, "SECRET_FILES", (absent, present))
    assert dispatcher.task_queue_api_secret() == "from-file"


def test_no_secret_anywhere_is_an_empty_string_not_a_crash(dispatcher, monkeypatch, tmp_path):
    monkeypatch.delenv("TASK_QUEUE_API_SECRET", raising=False)
    monkeypatch.setattr(dispatcher, "SECRET_FILES", (tmp_path / "nope.env",))
    assert dispatcher.task_queue_api_secret() == ""


# --- Tick ordering -----------------------------------------------------------


def test_a_tick_reaps_before_it_dispatches(dispatcher, monkeypatch):
    """The reaper reports the previous tick; dispatch decides this one. Pin the order.

    Deliberately NOT claimed here: that reversing it would leak concurrency slots. It
    would not — live_runs() tests pid liveness, so a run stops holding a slot when its
    process dies rather than when the record is stamped. This pins a sequence that makes
    a tick readable in order; the slot property is asserted separately below.
    """
    order: list[str] = []
    monkeypatch.setattr(dispatcher, "reap_runs", lambda: order.append("reap") or [])
    monkeypatch.setattr(dispatcher, "sweep_dead_runs", lambda dead: order.append("sweep"))
    monkeypatch.setattr(dispatcher, "process_submitted", lambda m: order.append("dispatch"))
    monkeypatch.setattr(dispatcher, "process_routing_failed", lambda m: None)
    monkeypatch.setattr(dispatcher, "archive_expired", lambda: None)

    assert dispatcher.main() == 0
    assert order == ["reap", "sweep", "dispatch"]


def test_a_dead_runs_record_holds_no_slot_through_a_whole_tick(
    dispatcher, launches, live_pids, monkeypatch, write_task
):
    """An open record for a dead process must not gate the next launch, at any point.

    This is the property that makes the cap self-healing without operator intervention:
    a session that crashed leaves its record open, and if `ended` rather than pid
    liveness decided occupancy, that one crash would block its agent forever.
    """
    monkeypatch.setattr(dispatcher, "MAX_RUNS_PER_AGENT", 1)
    _record(dispatcher, "developer", 11)  # open record, pid 11 is not in live_pids
    path, _ = write_task(target_agent="developer", workflow_mode="auto")

    dispatcher.main()

    assert len(launches) == 1
    assert dispatcher.load_yaml(path)["status"] == "approved"
