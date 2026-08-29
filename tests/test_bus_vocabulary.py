#!/usr/bin/env python3
"""
Assert every event type this dispatcher emits is declared in agent-bus's vocabulary.

WHY THIS EXISTS (build plan agent-workflow-interop-2026-08, Phase 5.3; vikunja#560)

The dispatcher is agent-bus's most prolific emitter and shares no constant with it. The
two can therefore disagree silently, and did: `task.workflow_started` was emitted here
from v0.9.x while being absent from agent-bus's `CROSS_AGENT_EVENTS` for months. It
reached the cross-agent log anyway, but only because `resolve_scope` returned the
caller's default for an unknown type and every caller happened to leave that default
alone. One `scope="session"` and it would have been filed where no
`query_events(scope="cross-agent")` and no federation would ever look again.

That is now a live consequence rather than a latent one. agent-bus v0.4.0 added
`AGENT_BUS_STRICT_VOCAB`; under `enforce` an undeclared type is REJECTED, so an emitter
that drifts ahead of the vocabulary stops being logged at all. This gate is what turns
that from a production discovery into a red pipeline.

HOW

The emitted types are read out of `cli.py` with `ast` — the first positional argument of
every `bus_log(...)` call — rather than from a hand-maintained list. A hand-maintained
list is a third copy of the same fact and would drift from the code the same way the
code drifted from agent-bus.

agent-bus is a separate public repo, so its vocabulary is read over HTTP and parsed with
`ast`, never imported and never executed. Same technique as
`test_task_queue_vocabulary.py`, for the same reasons.

DELIBERATELY NOT SKIPPABLE

There is no "no network, skip" path. A check that quietly passes when it could not read
the upstream is indistinguishable from one that verified something — that shape is how
vikunja#324 stayed open for months, and it is the reason the sibling gate has the same
note. If this cannot reach the upstream it fails, and the fix is to make the network
work.

CONSEQUENCE WORTH KNOWING: this tracks agent-bus's `main`. Removing a type there turns
this repo's CI red on the next push. That is the alarm, not a malfunction — the
dispatcher is genuinely emitting something the bus no longer accepts at that moment.

A NOTE ON WHAT THIS DOES *NOT* CHECK: the reverse direction. agent-bus declares types no
dispatcher emits (`handoff.*`, `preflight.*`, `security.finding`) because other agents
emit them, so a subset relation is the strongest true statement available here. An
equality check would be a stronger-looking assertion that is simply false.
"""

from __future__ import annotations

import ast
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# `main` is the contract — that is what is deployed and what this dispatcher must agree
# with. The override exists for the one situation that recurs whenever the vocabulary
# changes: a paired change lands in two repos and this one is legitimately ahead of
# agent-bus's `main` until its PR merges.
#
# CI MUST NOT SET THIS. It defaults to `main` precisely so an unset environment gets the
# strict check; a pipeline pinned to a feature branch would assert parity with something
# nobody is running.
UPSTREAM_REF = os.environ.get("AGENT_BUS_REF", "main")

# The ref is interpolated into a URL path, so constrain it. `..` in particular would
# traverse out of this repo's path segment on raw.githubusercontent.com and fetch
# somebody else's event_vocab.py — which this test would then compare against and report
# as authoritative.
if not re.fullmatch(r"[A-Za-z0-9._][A-Za-z0-9._/-]*", UPSTREAM_REF) or ".." in UPSTREAM_REF:
    print(f"FATAL: refusing AGENT_BUS_REF={UPSTREAM_REF!r} — not a plain git ref")
    sys.exit(2)

UPSTREAM_URL = f"https://raw.githubusercontent.com/TadMSTR/agent-bus/{UPSTREAM_REF}/event_vocab.py"
DISPATCHER = Path(__file__).resolve().parent.parent / "src" / "task_dispatcher" / "cli.py"

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def emitted_event_types(source: str) -> dict[str, int]:
    """Every event type passed to `bus_log(...)`, mapped to its line number.

    Only literal first arguments are collected. A computed one would be invisible here
    and is refused rather than ignored — see the caller.
    """
    found: dict[str, int] = {}
    computed: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "bus_log"):
            continue
        if not node.args:
            continue  # the module's own no-op `def bus_log(*a, **kw)` fallback
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.setdefault(first.value, node.lineno)
        else:
            computed.append(node.lineno)

    if computed:
        # A computed event type cannot be checked by this gate, and a gate that silently
        # skips what it cannot see reports the same green as one that verified
        # everything. Refuse instead, and make the emitter use a literal.
        print(
            f"FATAL: bus_log called with a non-literal event type at line(s) {computed}. "
            f"This gate cannot verify a computed type, and passing quietly over it would "
            f"report the same result as checking it. Use a string literal."
        )
        sys.exit(2)

    if not found:
        print(
            "FATAL: no bus_log event types parsed from cli.py — the extraction is "
            "broken, not the vocabulary. Failing rather than reporting a vacuous pass."
        )
        sys.exit(2)
    return found


def upstream_vocabulary(source: str) -> set[str]:
    """agent-bus's CROSS_AGENT_EVENTS and SESSION_EVENTS, without executing anything.

    Both sets, because the question this gate answers is "will agent-bus accept this
    type", and under `AGENT_BUS_STRICT_VOCAB=enforce` it accepts anything declared in
    either. Checking only CROSS_AGENT_EVENTS would fail a dispatcher that legitimately
    emitted a session-scoped type.
    """
    declared: set[str] = set()
    seen_names: list[str] = []
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id not in ("CROSS_AGENT_EVENTS", "SESSION_EVENTS"):
            continue
        try:
            # frozenset({...}) — a Call, so literal_eval the single argument.
            value = node.value
            if isinstance(value, ast.Call) and value.args:
                value = value.args[0]
            parsed = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError):
            continue
        if all(isinstance(v, str) for v in parsed):
            declared |= set(parsed)
            seen_names.append(target.id)

    if "CROSS_AGENT_EVENTS" not in seen_names:
        print(
            f"FATAL: CROSS_AGENT_EVENTS not parsed from {UPSTREAM_URL} — it was renamed "
            f"or restructured upstream. Failing rather than comparing against a partial "
            f"vocabulary, which would report drift that is not there (or hide drift "
            f"that is)."
        )
        sys.exit(2)
    return declared


def fetch_upstream() -> str:
    try:
        # SECURITY[accepted]: urllib follows redirects by default; no redirect handling is
        # set, deviating from baseline pattern SSRF-02. Same disposition as the TypeScript
        # gates in cloudcli-plugin-task-queue, and recorded inline for the same reason —
        # so the next audit of this file reads the ruling instead of re-deriving it.
        # The URL is not caller-supplied: host and path are literals and only the ref
        # segment varies, charset-checked and `..`-rejected above. Nothing fetched is
        # executed — it is parsed with `ast` and never imported. Worst case from a hostile
        # redirect is a false red (loud, blocks CI) or a false green requiring the attacker
        # to serve the exact correct upstream, which achieves nothing. This runs in CI and
        # from a developer shell only. Reviewed and accepted 2026-08-29 —
        # agent-workflow-interop-2026-08-phase5 audit, INFO 3; row in
        # host-forge-knowledge-base/security/accepted-risks.md. Revisit if this is ever
        # made to accept a caller-supplied URL or host.
        with urllib.request.urlopen(UPSTREAM_URL, timeout=30) as resp:
            if resp.status != 200:
                raise urllib.error.HTTPError(
                    UPSTREAM_URL, resp.status, "unexpected status", resp.headers, None
                )
            return resp.read().decode("utf-8")
    except Exception as exc:
        print(f"FATAL: could not read the event vocabulary source of truth at {UPSTREAM_URL}")
        print(f"       {type(exc).__name__}: {exc}")
        print(
            "       This is a hard failure on purpose — see the module docstring. A "
            "vocabulary check that skips when it cannot read the upstream reports the "
            "same result whether or not the two sides agree."
        )
        sys.exit(2)


def main() -> int:
    print("bus event vocabulary (dispatcher emits ⊆ agent-bus declares)")
    print(f"  upstream: {UPSTREAM_URL}")
    if UPSTREAM_REF != "main":
        print(
            f"  NOTE: comparing against ref {UPSTREAM_REF!r}, NOT main. This is a "
            f"pre-merge check and does not prove parity with what is deployed."
        )

    emitted = emitted_event_types(DISPATCHER.read_text())
    declared = upstream_vocabulary(fetch_upstream())
    print(f"  dispatcher emits {len(emitted)} type(s); agent-bus declares {len(declared)}")

    for event_type, lineno in sorted(emitted.items()):
        check(
            event_type in declared,
            f"{event_type} (cli.py:{lineno}) is declared by agent-bus",
        )

    print()
    if FAILURES:
        print(f"BUS VOCABULARY DRIFT ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        print()
        print(
            "The dispatcher is emitting an event type agent-bus does not declare. Under "
            "AGENT_BUS_STRICT_VOCAB=enforce that event is REJECTED and never logged; "
            "under warn it is logged with a warning. Fix by adding the type to "
            "CROSS_AGENT_EVENTS in agent-bus's event_vocab.py (and releasing it), or by "
            "correcting the spelling here. Do not edit this test to make it pass."
        )
        return 1
    print("every emitted event type is declared upstream")
    return 0


if __name__ == "__main__":
    sys.exit(main())
