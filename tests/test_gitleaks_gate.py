#!/usr/bin/env python3
"""
Assert the secret-scanning gate actually fires. Behaviour, not configuration.

WHY THIS EXISTS

This repository is public, and `.gitleaks.toml` is its only automated leak gate. A gate
that silently stops matching is indistinguishable from a clean repository — the same green
result either way — which is the failure mode that lets a real secret through.

The audit of task-dispatcher-breakout-2026-08 raised the narrower question of whether the
config's allowlist could mask a real secret sharing a line with an allowlisted variable
name. Testing it produced a more useful answer: on the pinned gitleaks version the global
allowlist suppressed nothing at all, in twelve config combinations. So the allowlist was
removed rather than re-scoped — see the note in `.gitleaks.toml`.

The durable protection is not a config setting, because the ambiguity was a config setting.
It is this: prove the gate fires on a planted secret, including one co-located with the
credential variable names this codebase legitimately mentions. That assertion survives a
gitleaks upgrade that changes allowlist semantics, because it tests what the gate does.

NOT SKIPPABLE. If gitleaks is missing this fails rather than skipping, for the same reason
tests/test_task_queue_vocabulary.py refuses to skip: a gate check that passes when it could
not run reports the same thing as one that verified something.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FAILURES: list[str] = []
REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / ".gitleaks.toml"


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def scan(directory: Path) -> bool:
    """True if gitleaks reports at least one leak."""
    r = subprocess.run(
        [
            "gitleaks",
            "detect",
            "--source",
            str(directory),
            "--no-git",
            "--redact",
            "--config",
            str(CONFIG),
        ],
        capture_output=True,
        text=True,
    )
    return r.returncode != 0


if shutil.which("gitleaks") is None:
    print("FATAL: gitleaks is not installed.")
    print("       This is a hard failure, not a skip — see the module docstring. The gate")
    print("       is this repository's only automated leak check and it is public.")
    sys.exit(2)

check(CONFIG.is_file(), ".gitleaks.toml exists")

# Every fixture below is ASSEMBLED AT RUNTIME rather than written as a literal, and that
# is load-bearing. Written out in full, each one is detected by the very gate this file
# tests — the repo scan then fails on its own test fixtures. That is not hypothetical: it
# is what happened, and CI caught it (`Scan full history`, run 33120910500, three findings,
# all of them these three lines). Splitting the strings breaks the rules' prefix anchors so
# the file scans clean, while the reassembled values are byte-identical at runtime and
# still fire. Do not "tidy" these back into literals.
#
# A synthetic GitHub PAT — not a real credential; accepted by the default ruleset purely
# on shape.
PAT = "ghp" + "_016C7f4d9aB2eF8c1D3a5B7e9F0a2C4d6E8f01"
INTERNAL_HOST = "https://box." + "internal"
# Must be RFC1918 or the rule correctly declines to fire — an address in RFC 2544
# benchmarking space was tried and did not, which is the rule behaving properly. The value
# below is in the 172.16/12 block and matches no host this project touches. An earlier
# draft used the operator's actual host address, which put a real internal IP into a public
# commit for the sake of testing the rule that exists to catch exactly that. Note the
# address cannot be written out even in this comment — the rule reads comments too.
RFC1918 = "172." + "31.255.254"

print("\nthe gate fires on a planted secret")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)

    # Control first. If this does not fire, every result below is meaningless — the probe
    # is broken rather than the repo being clean. An earlier version of this check used a
    # secret shape gitleaks did not recognise and reported "no leaks" for BOTH the planted
    # case and the control, which reads exactly like a pass.
    (d / "c.txt").write_text(f"token={PAT}\n")
    check(scan(d), "CONTROL: a bare secret is detected (else the probe proves nothing)")
    (d / "c.txt").unlink()

    # The case the audit asked about: a real secret sharing a line with each credential
    # variable name this codebase mentions.
    for name in [
        "SCOPED_MCP_BEARER_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "TASK_QUEUE_API_SECRET",
    ]:
        (d / "c.txt").write_text(f"{name}={PAT}\n")
        check(scan(d), f"detected when co-located with {name}")
        (d / "c.txt").unlink()

    # And on the same line as the loopback address the dispatcher legitimately contains.
    (d / "c.txt").write_text(f'curl -H "Authorization: {PAT}" http://127.0.0.1:8487/mcp\n')
    check(scan(d), "detected when co-located with 127.0.0.1")
    (d / "c.txt").unlink()

print("\nthe custom topology rules fire")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    (d / "c.txt").write_text(f"see {INTERNAL_HOST} for details\n")
    check(scan(d), "internal-hostname rule fires")
    (d / "c.txt").write_text(f"host is {RFC1918}\n")
    check(scan(d), "rfc1918-address rule fires")

print("\nand the repository itself is clean")
check(not scan(REPO_ROOT / "src"), "src/ scans clean")
check(not scan(REPO_ROOT / "tests" / "fixtures"), "tests/fixtures/ scans clean")

print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("all gitleaks gate checks passed")
