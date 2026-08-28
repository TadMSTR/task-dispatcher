#!/usr/bin/env python3
"""
`--version` must answer on a host with no agent roster.

WHY THIS EXISTS

--version is the deploy drift-check surface (vikunja#535 gap 4): the way anyone asks
"what is actually installed in /opt/venvs/task-dispatcher". cli.py loads the roster at
module level and raises LaunchPolicyError when it cannot, on purpose — an empty roster
drops run_as_user for every agent, which is how steward gets launched as the wrong user.

Those two facts collide. If --version is handled inside cli.main(), then importing cli to
reach main() raises first, and the drift check fails exactly on the hosts where someone is
most likely to be running it — a fresh deploy, a half-configured host, a roster someone
just broke. This was verified as a real failure, not a hypothetical: with --version in
main(), `HOME=/tmp/empty task-dispatcher --version` exited 1 with a LaunchPolicyError
traceback instead of printing a version.

So --version is answered in task_dispatcher._console before cli is imported. This test
pins that ordering, and it pins the other half too: a REAL run against a missing roster
must still fail loudly. A fix that made --version work by making the roster optional
would pass the first check and break the guarantee.
"""

from __future__ import annotations

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

with tempfile.TemporaryDirectory() as tmp:
    # A home with no ~/scripts/agent-launch.yml at all.
    env = dict(os.environ, HOME=tmp, PYTHONPATH=str(REPO_ROOT / "src"))
    env.pop("AGENT_LAUNCH_POLICY", None)

    print("--version on a host with no roster")
    r = subprocess.run(
        [sys.executable, "-m", "task_dispatcher", "--version"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp,
    )
    check(r.returncode == 0, f"exits 0 (got {r.returncode})")
    check(r.stdout.startswith("task-dispatcher "), f"prints a version (got {r.stdout!r})")
    check("LaunchPolicyError" not in r.stderr, "does not raise LaunchPolicyError")

    print("\na real run on the same host still fails loudly")
    r = subprocess.run(
        [sys.executable, "-m", "task_dispatcher"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp,
    )
    check(r.returncode != 0, f"exits non-zero (got {r.returncode})")
    check(
        "LaunchPolicyError" in r.stderr,
        "raises LaunchPolicyError rather than degrading to an empty roster",
    )

# --- version parity -------------------------------------------------------------------
# The two spellings of the version can drift, and --version is the surface that answers
# "what is actually installed in /opt/venvs/task-dispatcher" (vikunja#535 gap 4). A
# --version that reports 1.0.0 while the deployed package is 1.1.0 does not merely fail to
# help — it actively asserts the wrong answer to the one question it exists for, and the
# drift check reports agreement with whatever it is compared against.
#
# pyproject.toml is read as TEXT, not via importlib.metadata. Metadata reflects what is
# *installed*, which under an editable install is whatever was there at `pip install -e`
# time — so a metadata-based check compares __init__.py against a stale copy of itself and
# passes while the source disagrees.
print("\npyproject.toml and __version__ agree")

pyproject = (REPO_ROOT / "pyproject.toml").read_text()
declared = None
for line in pyproject.splitlines():
    if line.startswith("version = "):
        declared = line.split("=", 1)[1].strip().strip('"')
        break

check(declared is not None, "pyproject.toml declares a version")

module_version = None
for line in (REPO_ROOT / "src" / "task_dispatcher" / "__init__.py").read_text().splitlines():
    if line.startswith("__version__ = "):
        module_version = line.split("=", 1)[1].strip().strip('"')
        break

check(module_version is not None, "__init__.py declares __version__")
check(
    declared == module_version,
    f"they match (pyproject={declared!r}, __version__={module_version!r})",
)

# And the version --version actually prints is that one, not a third thing.
with tempfile.TemporaryDirectory() as tmp:
    env = dict(os.environ, HOME=tmp, PYTHONPATH=str(REPO_ROOT / "src"))
    env.pop("AGENT_LAUNCH_POLICY", None)
    r = subprocess.run(
        [sys.executable, "-m", "task_dispatcher", "--version"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp,
    )
    check(
        r.stdout.strip() == f"task-dispatcher {declared}",
        f"--version prints it (got {r.stdout.strip()!r}, expected 'task-dispatcher {declared}')",
    )

print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("all --version / no-roster checks passed")
