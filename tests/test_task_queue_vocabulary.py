#!/usr/bin/env python3
"""
Assert task-dispatcher.py's task-queue vocabulary equals task-queue-mcp's.

WHY THIS EXISTS (vikunja#324)

The dispatcher is a fourth writer to ~/.claude/task-queue and shares no validation
code with task-queue-mcp, which owns the schema. The two spellings have drifted
before, and the drift is silent by construction: the MCP rejects a value the
dispatcher would have accepted, or — worse — the dispatcher falls through a
default branch on a value the MCP has since started emitting, and the task simply
does not do what its file says.

task-queue-headless-chain-2026-08 added two values that both sides must agree on
(`notify` as self-terminal, `manual-then-auto`), which is the second instance of
exactly this class. Rather than add it and file the drift, this closes it.

HOW

The MCP is a separate, public repo, so the check reads its source over HTTP and
compares the parsed literals against the dispatcher's own. The upstream is parsed
with `ast`, never imported — this must not depend on the MCP's runtime deps being
installed, and must not execute code fetched over the network.

DELIBERATELY NOT SKIPPABLE

There is no "no network, skip" path. A vocabulary check that quietly passes when it
could not read the upstream is indistinguishable from one that verified something,
and that shape — a probe that reports success without asserting anything — is how
#324 stayed open. If this cannot reach the upstream it fails, and the fix is to
make the network work or to pin a vendored copy deliberately.

CONSEQUENCE WORTH KNOWING: this tracks task-queue-mcp's `main`. A vocabulary change
merged there turns this repo's CI red on the next push. That is the alarm, not a
malfunction — the dispatcher is genuinely out of date at that moment. Update the
block in task-dispatcher.py and it goes green.
"""

from __future__ import annotations

import ast
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# The ref to compare against. `main` is the contract — that is what is deployed and
# what this dispatcher must agree with.
#
# The override exists for ONE situation, which is not hypothetical and will recur every
# time the vocabulary changes: a paired change lands in two repos, and the dispatcher is
# legitimately ahead of the MCP's `main` until the MCP's PR merges. Without it the only
# way to see whether the REST of this pipeline is green is to merge first and find out.
#
# CI MUST NOT SET THIS. It defaults to `main` precisely so an unset environment gets the
# strict check; a pipeline that pinned it to a feature branch would be asserting parity
# with something nobody is running. Use it from a shell, note it in the PR, and let the
# post-merge run be the one that counts.
UPSTREAM_REF = os.environ.get("TASK_QUEUE_MCP_REF", "main")

# The ref is interpolated into a URL path, so constrain it. `..` in particular would
# traverse out of this repo's path segment on raw.githubusercontent.com and fetch
# somebody else's queue.py — which this test would then happily compare against and
# report as authoritative. Low severity (it needs control of the CI environment, and
# anyone holding that has more direct options) but free to close.
if not re.fullmatch(r"[A-Za-z0-9._][A-Za-z0-9._/-]*", UPSTREAM_REF) or ".." in UPSTREAM_REF:
    print(f"FATAL: refusing TASK_QUEUE_MCP_REF={UPSTREAM_REF!r} — not a plain git ref")
    sys.exit(2)

UPSTREAM_URL = (
    f"https://raw.githubusercontent.com/TadMSTR/task-queue-mcp/{UPSTREAM_REF}/src/tools/queue.py"
)
DISPATCHER = Path(__file__).resolve().parent.parent / "task-dispatcher.py"

# Every set both sides must agree on, dispatcher name → MCP name. They are not all
# spelled the same: the dispatcher has called it TERMINAL_STATES since before the MCP
# existed, and renaming it across a live cron script is a worse trade than mapping it
# here. The mapping is the point — what matters is the contents, not the identifier.
SHARED_VOCABULARY = {
    "VALID_STATUSES": "VALID_STATUSES",
    "VALID_TASK_TYPES": "VALID_TASK_TYPES",
    "VALID_WORKFLOW_MODES": "VALID_WORKFLOW_MODES",
    "TERMINAL_STATUSES": "TERMINAL_STATUSES",
    "SELF_TERMINAL_TASK_TYPES": "SELF_TERMINAL_TASK_TYPES",
}

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def literal_sets(source: str, origin: str) -> dict[str, set]:
    """Extract module-level `NAME = {...}` string-set literals without executing anything."""
    found: dict[str, set] = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError):
            continue
        if isinstance(value, set) and all(isinstance(v, str) for v in value):
            found[target.id] = value
    if not found:
        print(f"FATAL: no set literals parsed from {origin} — the extraction is broken, "
              f"not the vocabulary. Failing rather than reporting a vacuous pass.")
        sys.exit(2)
    return found


def fetch_upstream() -> str:
    try:
        with urllib.request.urlopen(UPSTREAM_URL, timeout=30) as resp:
            if resp.status != 200:
                raise urllib.error.HTTPError(
                    UPSTREAM_URL, resp.status, "unexpected status", resp.headers, None
                )
            return resp.read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001 — every failure mode here is a hard failure
        print(f"FATAL: could not read the vocabulary source of truth at {UPSTREAM_URL}")
        print(f"       {type(exc).__name__}: {exc}")
        print("       This is a hard failure on purpose — see the module docstring. A "
              "vocabulary check that skips when it cannot read the upstream reports the "
              "same result whether or not the two sides agree.")
        sys.exit(2)


def main() -> int:
    print("task queue vocabulary parity (dispatcher ↔ task-queue-mcp)")
    print(f"  upstream: {UPSTREAM_URL}")
    if UPSTREAM_REF != "main":
        print(f"  NOTE: comparing against ref {UPSTREAM_REF!r}, NOT main. This is a "
              f"pre-merge check and does not prove parity with what is deployed.")

    mcp = literal_sets(fetch_upstream(), "task-queue-mcp/src/tools/queue.py")
    disp = literal_sets(DISPATCHER.read_text(), "task-dispatcher.py")

    for disp_name, mcp_name in sorted(SHARED_VOCABULARY.items()):
        if mcp_name not in mcp:
            check(False, f"{mcp_name} is missing from task-queue-mcp — renamed upstream?")
            continue
        if disp_name not in disp:
            check(False, f"{disp_name} is missing from task-dispatcher.py")
            continue
        ours, theirs = disp[disp_name], mcp[mcp_name]
        if ours == theirs:
            check(True, f"{disp_name} == {mcp_name} ({len(ours)} values)")
        else:
            only_ours = sorted(ours - theirs)
            only_theirs = sorted(theirs - ours)
            detail = []
            if only_ours:
                detail.append(f"dispatcher-only: {only_ours}")
            if only_theirs:
                detail.append(f"mcp-only: {only_theirs}")
            check(False, f"{disp_name} != {mcp_name} — " + "; ".join(detail))

    print()
    if FAILURES:
        print(f"VOCABULARY DRIFT ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        print()
        print("Fix by editing the vocabulary block in scripts/task-dispatcher.py to match "
              "task-queue-mcp. Do not edit this test to make it pass.")
        return 1
    print("vocabulary is in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
