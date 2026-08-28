#!/usr/bin/env python3
"""The agent-bus emitter must be WIRED, not merely importable.

WHY THIS EXISTS

cli.py imports the agent-bus client inside a `try/except ImportError` that degrades to a
no-op:

    try:
        from agent_bus_client import log_event as bus_log
    except ImportError:
        def bus_log(*a, **kw):
            pass

That fallback is correct and must stay. A cron job that dies every two minutes because an
optional logging client is missing is worse than one that does not log. What was missing
was any assertion that the *non-degraded* path is the one running in production.

The consequence, measured rather than imagined: the v1.0.0 extraction removed a hardcoded
`sys.path.insert(0, ~/repos/personal/agent-bus)` — correct for a public repo — and the
`bus` extra that was supposed to replace it named `agent-bus-client`, a distribution that
does not exist. So the import failed, the stub took over, and five event types
(see DISPATCHER_EVENTS below — six of them, not the five the ticket counted) stopped
reaching the signed audit trail at 18:32 EDT on 2026-08-27. Nothing looked wrong. The
dispatcher ran clean and logged clean for the entire life of the release. The defect was
found by noticing an absence in a query, which is not a thing anyone does on a schedule.

This is the third configured-but-silently-dead emitter on forge (vikunja#444, vikunja#436,
this one is #550). The pattern is always the same: success is reported by the absence of an
error, and the absence of an error is also what total failure looks like.

WHAT THIS ASSERTS, AND WHY EACH PART IS NEEDED

  1. `cli.bus_log` is not the stub. Checked by `__module__`, because the stub and the real
     function have the same name and the same call signature — the only thing that
     distinguishes them is where they were defined.
  2. Calling it writes a real chained record to a real file. The import succeeding and the
     write working are two different claims; a client that imports and then swallows every
     event would satisfy (1).
  3. The records land in the *cross-agent* log, not the session log. An event written to
     the session file is invisible to `query_events(scope="cross-agent")` and is never
     federated — dead in the way that matters, while an import check and a "something was
     written" check both stay green. This has happened before: agent_bus_client stopped
     keeping its own copy of the vocabulary because that copy went stale and `build.*`
     events vanished into the session file.

     NOTE ON WHAT THIS DOES *NOT* CHECK, because the obvious version of it is a tautology.
     An earlier draft asserted `resolve_scope(et, "cross-agent") == "cross-agent"` for each
     type. That can never fail: resolve_scope returns `"cross-agent" if event_type in
     CROSS_AGENT_EVENTS else scope`, and `scope` is already `"cross-agent"` — the caller's
     default. Dropping a type from the vocabulary would not have failed it. Worse, it would
     not have failed anything, because cli.py never passes `scope` either, so for these
     five call sites vocabulary membership does not affect routing at all. The assertion
     that has teeth is the one on the file the bytes actually landed in.

  4. The event types this test knows about are the ones cli.py actually emits. A sixth
     bus_log call site added later would otherwise leave this file testing five of six and
     reporting complete coverage of the emitter.

NO SKIP PATH — deliberate. If agent-bus is not installed this test FAILS rather than
skipping, exactly as test_gitleaks_gate.py and test_task_queue_vocabulary.py do. A skip
here would restore the original defect one level up: the suite would go green on precisely
the configuration this test exists to reject.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


REPO_ROOT = Path(__file__).resolve().parent.parent

# Every event type cli.py emits. Not maintained by hand: check_event_list_matches_cli()
# below re-derives this set from the call sites and fails if the two disagree, in either
# direction.
#
# SIX, not five. The build plan, vikunja#550 and this repo's own v1.0.0 notes all say five
# — they enumerate the `task.*` lifecycle events and miss `task.workflow_started`, which is
# emitted from the Temporal branch of process_submitted(). That undercount is exactly what
# the parity check exists to catch, and it caught it on the first run.
DISPATCHER_EVENTS = [
    "task.approved",
    "task.completed",
    "task.dispatched",
    "task.failed",
    "task.routing-failed",
    "task.workflow_started",
]

# Run in a child so AGENT_BUS_COMMS_DIR is set before agent_bus_client resolves it at
# import time, and so cli.py's module-level roster load gets a HOME it can satisfy.
_CHILD = r"""
import json, sys
from task_dispatcher import cli

# Report what cli actually bound BEFORE anything else can fail, so a missing client
# produces "the stub is active" rather than a bare ModuleNotFoundError from line 4. The
# stub being active IS the defect; the missing distribution is only its cause, and a
# failure message that names the cause and not the defect is how this got missed once.
if cli.bus_log.__module__ == "task_dispatcher.cli":
    print(json.dumps({"bus_log_module": cli.bus_log.__module__}))
    sys.exit(
        "cli.bus_log is the no-op stub from the `except ImportError` branch — the agent-bus "
        "event stream is DEAD. The `bus` extra did not resolve."
    )

from agent_bus_client import log_event

emitted = [
    cli.bus_log(
        event_type=et,
        source="test-bus-emitter-live",
        summary=f"emitter liveness probe: {et}",
        target="nobody",
        metadata={"probe": True},
    )
    for et in json.loads(sys.argv[1])
]

print(json.dumps({
    "bus_log_module": cli.bus_log.__module__,
    "bus_log_name": cli.bus_log.__qualname__,
    "returned": emitted,
    "log_event_module": log_event.__module__,
}))
"""


def run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        comms = tmpd / "comms"

        # A HOME the roster loads from. cli.py raises at import if it cannot resolve one,
        # so this test cannot reach its assertions without a valid policy file — which is
        # also why it uses the repo's own fixture rather than the live roster.
        policy = REPO_ROOT / "tests" / "fixtures" / "agent-launch.yml"
        if not policy.exists():
            print(f"  FAIL launch policy fixture missing at {policy}")
            FAILURES.append("launch policy fixture present")
            return

        # A MINIMAL env, not dict(os.environ, ...). Two reasons, and the second is the
        # one that matters on forge: an inherited variable this test does not override
        # can change what it measures — AGENT_BUS_* and the roster vars are exactly the
        # kind of thing an operator has set in their shell — so inheriting makes the
        # result depend on who ran it. And a dispatcher session's environment carries the
        # full forge secret set, which a child process has no business receiving to
        # append a line to a JSONL file in a tmpdir.
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmpd),
            "AGENT_BUS_COMMS_DIR": str(comms),
            "AGENT_LAUNCH_POLICY": str(policy),
            "PYTHONPATH": str(REPO_ROOT / "src"),
        }

        print("the emitter is the real client, not the no-op stub")
        r = subprocess.run(
            [sys.executable, "-c", _CHILD, json.dumps(DISPATCHER_EVENTS)],
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp,
        )
        if r.returncode != 0:
            # The overwhelmingly likely cause is that the `bus` extra is not installed.
            # Say so, and still fail — see the module docstring on why there is no skip.
            print(f"  FAIL child exited {r.returncode}")
            print(f"       stdout: {r.stdout}")
            print(f"       stderr: {r.stderr.strip()[-2000:]}")
            print("       (install the extra: pip install -e '.[dev,bus]')")
            FAILURES.append("child process ran")
            return

        out = json.loads(r.stdout.strip().splitlines()[-1])

        # THE CENTRAL ASSERTION. A bus_log defined in task_dispatcher.cli is the stub from
        # the `except ImportError` branch; the real one comes from agent_bus_client.
        check(
            out["bus_log_module"] == "agent_bus_client",
            f"cli.bus_log.__module__ is agent_bus_client (got {out['bus_log_module']!r}"
            + (
                " — this is the no-op stub, the `bus` extra did not resolve)"
                if out["bus_log_module"] == "task_dispatcher.cli"
                else ")"
            ),
        )
        check(
            out["bus_log_name"] != "bus_log",
            f"is not the stub's `bus_log` (got qualname {out['bus_log_name']!r})",
        )
        # The stub returns None for everything. Real log_event returns the event dict.
        check(
            all(isinstance(e, dict) and e.get("id") for e in out["returned"]),
            "every call returned an event dict with an id (the stub returns None)",
        )

        print("\nthe call actually writes a chained record")
        logs_dir = comms / "logs"
        written = sorted(logs_dir.glob("*-cross-agent.jsonl")) if logs_dir.is_dir() else []
        check(bool(written), f"a cross-agent log exists under {logs_dir}")
        if not written:
            return

        lines = [ln for ln in written[0].read_text().splitlines() if ln.strip()]
        records = [json.loads(ln) for ln in lines]
        check(
            [rec["id"] for rec in records] == [e["id"] for e in out["returned"]],
            f"all {len(DISPATCHER_EVENTS)} events landed, in order "
            f"(wrote {len(records)}, expected {len(DISPATCHER_EVENTS)})",
        )
        check(
            [rec["event"] for rec in records] == DISPATCHER_EVENTS,
            "the event types on disk are the ones emitted",
        )
        check(
            all(rec["source"] == "test-bus-emitter-live" for rec in records),
            "source survives the round-trip",
        )
        # Written through event_log's shared append path, not a bare append. `prev_hash` is
        # absent on the first line of a file by construction, so this needs 2+ records.
        import hashlib

        check(
            "prev_hash" not in records[0],
            "first record has no prev_hash (nothing to chain onto)",
        )
        check(
            all(
                records[i]["prev_hash"] == hashlib.sha256(lines[i - 1].encode()).hexdigest()
                for i in range(1, len(records))
            ),
            "every later record chains onto the previous line",
        )

        # Landing in the cross-agent FILE is the assertion with teeth — see the docstring
        # on why the resolve_scope() version of this was a tautology. The glob above
        # already required the cross-agent file to exist and hold all five; this pins the
        # scope recorded inside them too, so a writer that names the file correctly while
        # marking the records session-scoped still fails.
        check(
            all(rec["scope"] == "cross-agent" for rec in records),
            "every record is recorded as cross-agent scope",
        )
        check(
            not list(logs_dir.glob("*-session.jsonl")),
            "nothing leaked into the session log",
        )


def check_event_list_matches_cli() -> None:
    """DISPATCHER_EVENTS must be every event type cli.py emits — no more, no fewer.

    Re-derived from the source rather than maintained by hand, because a hand-maintained
    second roster of the same fact drifts the moment someone adds a call site, and it
    drifts silently: the test keeps passing on the five it knows about.
    """
    import ast

    src = (REPO_ROOT / "src" / "task_dispatcher" / "cli.py").read_text()
    tree = ast.parse(src)

    emitted: set[str] = set()
    unresolvable = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "bus_log"):
            continue
        # `bus_log("task.failed", ...)` — positional — or `bus_log(event_type="...")`.
        arg = None
        if node.args:
            arg = node.args[0]
        else:
            for kw in node.keywords:
                if kw.arg == "event_type":
                    arg = kw.value
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            emitted.add(arg.value)
        else:
            # A computed event type would make this check unsound rather than merely
            # incomplete, so it is surfaced instead of ignored.
            unresolvable += 1

    expected = set(DISPATCHER_EVENTS)
    only_in_cli = sorted(emitted - expected)
    only_in_test = sorted(expected - emitted)
    detail = ""
    if only_in_cli:
        detail += f"; emitted by cli.py but not listed here: {only_in_cli}"
    if only_in_test:
        detail += f"; listed here but no longer emitted: {only_in_test}"

    print("\nthe list above is every event type cli.py emits")
    check(
        unresolvable == 0,
        f"every bus_log call site has a literal event type ({unresolvable} do not)",
    )
    check(emitted == expected, f"DISPATCHER_EVENTS matches the call sites{detail}")


run()
check_event_list_matches_cli()

print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("all bus emitter liveness checks passed")
