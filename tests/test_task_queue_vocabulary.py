#!/usr/bin/env python3
"""
Assert task_dispatcher/cli.py's task-queue vocabulary equals task-queue-mcp's.

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

THE DIRECTORY NAMES ARE THE SAME CONTRACT, ONE LAYER DOWN

Carried in from the Phase 1 audit of agent-workflow-interop-2026-08 (INFO, deferred
to Phase 5 by name). `task-dispatcher` WRITES `~/.claude/task-queue/dead-letters/`
and task-queue-mcp READS it, via two independent string literals that nothing pinned
to each other. If the writer's name drifts, `get_task` and `list_tasks` silently stop
finding new dead letters — which is vikunja#557 exactly, recurring in the fix for
vikunja#557. Seventeen security audit requests went into that directory over three
months and no interface could show them.

The names live in different syntax on each side (`TASK_QUEUE_DIR / "dead-letters"`
here, a bare `DEAD_LETTER_DIRNAME = "dead-letters"` upstream), which is why they need
their own extractor below rather than falling out of the set comparison.

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
block in src/task_dispatcher/cli.py and it goes green.
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
DISPATCHER = Path(__file__).resolve().parent.parent / "src" / "task_dispatcher" / "cli.py"

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
        print(
            f"FATAL: no set literals parsed from {origin} — the extraction is broken, "
            f"not the vocabulary. Failing rather than reporting a vacuous pass."
        )
        sys.exit(2)
    return found


# The shared directory names: dispatcher constant → MCP constant. The dispatcher spells
# each as a path join off TASK_QUEUE_DIR; the MCP spells it as a bare string, because it
# joins with os.path. What must agree is the SEGMENT, not the expression.
SHARED_DIRNAMES = {
    "DEAD_LETTER_DIR": "DEAD_LETTER_DIRNAME",
    "ARCHIVE_DIR": "ARCHIVE_DIRNAME",
}


def literal_strings(source: str, origin: str) -> dict[str, str]:
    """Module-level `NAME = "value"` and `NAME = <anything> / "value"` string literals.

    The second form is the dispatcher's (`TASK_QUEUE_DIR / "dead-letters"`); only the
    final path segment is taken, since that is the part the two repos must agree on. A
    multi-segment join would yield only its last component, which would be a wrong
    comparison rather than a missing one — so it is refused below instead.
    """
    found: dict[str, str] = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        value = node.value
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Div):
            # `A / "b"` — take the right operand, and only if it is a plain literal.
            value = value.right
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            found[target.id] = value.value
    return found


def check_shared_dirnames() -> None:
    """The dead-letter/archive directory names, writer vs reader."""
    disp = literal_strings(DISPATCHER.read_text(), "src/task_dispatcher/cli.py")
    mcp = literal_strings(fetch_upstream(), "task-queue-mcp/src/tools/queue.py")

    for disp_name, mcp_name in sorted(SHARED_DIRNAMES.items()):
        ours, theirs = disp.get(disp_name), mcp.get(mcp_name)
        if ours is None:
            # Not "assume it moved and pass" — an unparseable writer side is exactly
            # the state in which this contract is least verified and most likely broken.
            check(False, f"{disp_name} is missing or not a plain literal in cli.py")
            continue
        if theirs is None:
            check(False, f"{mcp_name} is missing from task-queue-mcp — renamed upstream?")
            continue
        check(
            ours == theirs,
            f"{disp_name} == {mcp_name} ({ours!r})"
            if ours == theirs
            else f"{disp_name}={ours!r} != {mcp_name}={theirs!r} — the writer and the "
            f"reader disagree about where dead letters live, so nothing will find them",
        )


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
        print(f"FATAL: could not read the vocabulary source of truth at {UPSTREAM_URL}")
        print(f"       {type(exc).__name__}: {exc}")
        print(
            "       This is a hard failure on purpose — see the module docstring. A "
            "vocabulary check that skips when it cannot read the upstream reports the "
            "same result whether or not the two sides agree."
        )
        sys.exit(2)


def main() -> int:
    print("task queue vocabulary parity (dispatcher ↔ task-queue-mcp)")
    print(f"  upstream: {UPSTREAM_URL}")
    if UPSTREAM_REF != "main":
        print(
            f"  NOTE: comparing against ref {UPSTREAM_REF!r}, NOT main. This is a "
            f"pre-merge check and does not prove parity with what is deployed."
        )

    mcp = literal_sets(fetch_upstream(), "task-queue-mcp/src/tools/queue.py")
    disp = literal_sets(DISPATCHER.read_text(), "src/task_dispatcher/cli.py")

    for disp_name, mcp_name in sorted(SHARED_VOCABULARY.items()):
        if mcp_name not in mcp:
            check(False, f"{mcp_name} is missing from task-queue-mcp — renamed upstream?")
            continue
        if disp_name not in disp:
            check(False, f"{disp_name} is missing from src/task_dispatcher/cli.py")
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

    check_shared_dirnames()

    print()
    if FAILURES:
        print(f"VOCABULARY DRIFT ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        print()
        print(
            "Fix by editing the vocabulary block in src/task_dispatcher/cli.py to match "
            "task-queue-mcp — or, for a DIRECTORY NAME failure, by agreeing the segment "
            "on both sides: the dispatcher writes that directory and task-queue-mcp "
            "reads it, so a mismatch means dead letters exist that no tool can "
            "enumerate. Do not edit this test to make it pass."
        )
        return 1
    print("vocabulary is in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
