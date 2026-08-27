#!/usr/bin/env python3
"""
Tests for the shared agent launch policy (task-queue-plugin-repair-2026-08, vikunja#523).

The property under test is NOT "the loader parses YAML". It is that extracting three
hardcoded rosters into one data file did not lose the guarantee the literals provided:
no value outside a closed set can reach subprocess.Popen, and a bad file fails loudly
instead of degrading to an empty roster.

That degradation is the one that matters. An empty roster leaves `run_as_user` absent
for EVERY agent, and an absent run_as_user is how steward gets launched as ted — a
session that looks like steward in every log and holds none of steward's credentials
(vikunja#404). So "raises" is asserted explicitly for every malformed input, and
`== {}` is asserted nowhere.

Hermetic: redirects HOME to a tmpdir BEFORE importing the dispatcher (module-level code
resolves TASK_QUEUE_DIR and opens a log file under it), and validates against injected
fixtures rather than the host's real directories — except for one test that loads the
SHIPPED scripts/agent-launch.yml, which is the file the dispatcher will actually read.
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
(_HOME / ".pm2" / "logs").mkdir(parents=True)
for _agent in ("developer", "security", "steward", "sysadmin", "writer", "research"):
    (_HOME / ".claude" / "projects" / _agent).mkdir(parents=True)
os.environ["HOME"] = str(_HOME)

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
MODULE_PATH = SCRIPTS_DIR / "task-dispatcher.py"
_spec = importlib.util.spec_from_file_location("task_dispatcher_lp", MODULE_PATH)
td = importlib.util.module_from_spec(_spec)
sys.modules["task_dispatcher_lp"] = td
_spec.loader.exec_module(td)

PROJECT_ROOT = _HOME / ".claude" / "projects"


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def rejects(raw: object, label: str) -> None:
    """Assert validate_launch_policy raises — and raises the NAMED error, not any error."""
    try:
        td.validate_launch_policy(raw, project_root=PROJECT_ROOT)
    except td.LaunchPolicyError:
        print(f"  ok   {label}")
        return
    except Exception as e:  # noqa: BLE001 — a TypeError here is still a bug, not a pass
        print(f"  FAIL {label} (raised {type(e).__name__}, not LaunchPolicyError: {e})")
        FAILURES.append(label)
        return
    print(f"  FAIL {label} (accepted)")
    FAILURES.append(label)


def good(**over) -> dict:
    """A minimal valid document; `over` replaces or adds agents."""
    doc = {"developer": {"project_dir": "~/.claude/projects/developer"}}
    doc.update(over)
    return doc


STEWARD = {
    "project_dir": "~/.claude/projects/steward",
    "run_as_user": "agent-steward",
    "launcher": "/usr/local/sbin/forge/run-steward.sh",
}


# --- the shipped file --------------------------------------------------------
print("\nthe shipped scripts/agent-launch.yml")

SHIPPED = SCRIPTS_DIR / "agent-launch.yml"
check(SHIPPED.is_file(), "exists beside task-dispatcher.py")
check(
    td.LAUNCH_POLICY_PATH.resolve() == SHIPPED.resolve(),
    "is the file LAUNCH_POLICY_PATH points at (no $HOME dependence)",
)

shipped_raw = yaml.safe_load(SHIPPED.read_text())
shipped = td.validate_launch_policy(shipped_raw, project_root=PROJECT_ROOT)

# The exact roster the three literals used to spell, asserted as a set so a dropped
# agent is a failure rather than a silently smaller queue.
check(
    set(shipped) == {"sysadmin", "developer", "research", "writer", "security", "steward"},
    "carries exactly the six agents the old literals did",
)
check(
    [a for a, e in shipped.items() if e["run_as_user"]] == ["steward"],
    "steward is the only run-as agent",
)
check(
    shipped["steward"]["run_as_user"] == "agent-steward"
    and shipped["steward"]["launcher"] == "/usr/local/sbin/forge/run-steward.sh",
    "steward's run-as pair matches the sudoers grant exactly",
)
# The plugin gained a steward entry it never had — that IS #523.
check("steward" in shipped, "steward is present (the plugin's roster never had it — #523)")

# --- the degradation that must never happen ----------------------------------
print("\na bad file raises; it never degrades to an empty roster")

rejects(None, "empty file (yaml -> None)")
rejects({}, "empty mapping")
rejects([], "a list instead of a mapping")
rejects("developer", "a bare string")

missing = _HOME / "no-such-policy.yml"
try:
    td.load_launch_policy(missing)
    check(False, "a missing file raises")
except td.LaunchPolicyError as e:
    check("cannot read launch policy" in str(e), "a missing file raises, naming the path")

bad_yaml = _HOME / "bad.yml"
bad_yaml.write_text("developer: {project_dir: [unclosed\n")
try:
    td.load_launch_policy(bad_yaml)
    check(False, "unparseable YAML raises")
except td.LaunchPolicyError as e:
    check("cannot parse launch policy" in str(e), "unparseable YAML raises, naming the path")

# --- the whole document is rejected, not partially honoured ------------------
print("\none bad entry rejects the whole document")

# This is the subtle one. A loader that skipped bad entries and kept good ones would
# pass every test above. Here steward is VALID and the other entry is not: if the
# loader returned a partial roster, steward would launch correctly and `sysadmin`
# would silently vanish from the queue's reach.
rejects(
    {"steward": STEWARD, "sysadmin": {"project_dir": "/etc/passwd"}},
    "a valid steward entry does not rescue a document with one bad entry",
)

# --- closed set: project_dir -------------------------------------------------
print("\nproject_dir is constrained to ~/.claude/projects")

rejects(good(x={"project_dir": "/tmp/evil"}), "project_dir outside the projects root")
rejects(good(x={"project_dir": "~/.claude/projects/../../evil"}), "project_dir escaping via ..")
rejects(good(x={"project_dir": "relative/path"}), "a relative project_dir")
rejects(good(x={"project_dir": ""}), "an empty project_dir")
rejects({"developer": {}}, "a missing project_dir")
rejects(good(x={"project_dir": 42}), "a non-string project_dir")
# `~/.claude/projectsX` must not pass a naive startswith check.
rejects(good(x={"project_dir": "~/.claude/projectsX/evil"}), "a sibling dir sharing the root's prefix")

ok = td.validate_launch_policy(good(), project_root=PROJECT_ROOT)
check(
    ok["developer"]["project_dir"] == PROJECT_ROOT / "developer",
    "a valid project_dir expands ~ and resolves under the root",
)
check(
    isinstance(ok["developer"]["project_dir"], Path),
    "project_dir comes back as a Path, as the old literal was",
)
check(ok["developer"]["run_as_user"] is None, "a normal agent has run_as_user None")

# --- closed set: run_as_user / launcher --------------------------------------
print("\nrun_as_user and launcher are constrained and paired")

rejects(
    good(x={"project_dir": "~/.claude/projects/steward", "run_as_user": "agent-steward"}),
    "run_as_user without a launcher",
)
rejects(
    good(x={"project_dir": "~/.claude/projects/steward", "launcher": "/usr/local/sbin/forge/run-steward.sh"}),
    "a launcher without a run_as_user",
)
for bad_user in ("root", "ted", "agent-steward; rm -rf /", "AGENT-STEWARD", "agent_steward", ""):
    rejects(
        good(x={"project_dir": "~/.claude/projects/steward", "run_as_user": bad_user,
                "launcher": "/usr/local/sbin/forge/run-steward.sh"}),
        f"run_as_user {bad_user!r}",
    )
for bad_launcher in (
    "/usr/bin/env",
    "/usr/local/sbin/forge/../../../bin/sh",
    "run-steward.sh",
    "/usr/local/sbin/forgery/run-steward.sh",
):
    rejects(
        good(x={"project_dir": "~/.claude/projects/steward", "run_as_user": "agent-steward",
                "launcher": bad_launcher}),
        f"launcher {bad_launcher!r}",
    )

# A launcher that does not exist is SHAPE-valid on purpose: existence is checked at
# launch time by os.access, which reports it as a routing failure naming the script and
# telling the operator to deploy it. Validating existence here would make an undeployed
# launcher stop the dispatcher from importing at all — for every other agent too.
shape_ok = td.validate_launch_policy(
    good(x={"project_dir": "~/.claude/projects/steward", "run_as_user": "agent-steward",
            "launcher": "/usr/local/sbin/forge/does-not-exist.sh"}),
    project_root=PROJECT_ROOT,
)
check(
    shape_ok["x"]["launcher"] == "/usr/local/sbin/forge/does-not-exist.sh",
    "a nonexistent but well-shaped launcher loads (existence is a launch-time check)",
)

# --- closed set: agent names and keys ----------------------------------------
print("\nagent names and keys are constrained")

for bad_name in ("Developer", "9developer", "dev eloper", "../developer", "", "dev/eloper"):
    rejects({bad_name: {"project_dir": "~/.claude/projects/developer"}}, f"agent name {bad_name!r}")
rejects(good(x={"project_dir": "~/.claude/projects/developer", "extra": "x"}), "an unknown key")
rejects({"developer": "not-a-mapping"}, "an agent entry that is not a mapping")

# --- symlinked root: the two validators must agree -----------------------------
print("\na symlinked project root does not change the verdict")

# The audit finding this pins (task-queue-plugin-repair-2026-08, Low). validate_launch_policy
# used to compute `root` with .resolve(), which follows symlinks, while the plugin's
# validateLaunchPolicy() uses a plain join. Neither resolves the CANDIDATE project_dir, so
# resolving only the root compared a canonical path against an uncanonical one — and the two
# languages then disagreed the moment anything on the path was a symlink.
#
# It was NOT a rejected-entry bug. LAUNCH_POLICY = load_launch_policy() runs at import, so a
# symlinked ~/.claude/projects took the whole dispatcher down on every tick, for every agent.
_SYM = tempfile.TemporaryDirectory()
_sym_base = Path(_SYM.name)
(_sym_base / "real-projects" / "steward").mkdir(parents=True)
(_sym_base / "fake-home" / ".claude").mkdir(parents=True)
(_sym_base / "fake-home" / ".claude" / "projects").symlink_to(_sym_base / "real-projects")

_sym_root = _sym_base / "fake-home" / ".claude" / "projects"
check(_sym_root.is_symlink(), "fixture: the project root really is a symlink")
check(
    _sym_root.resolve() != _sym_root,
    "fixture: resolving it really does change the path (otherwise this test proves nothing)",
)

_sym_doc = {"steward": {"project_dir": str(_sym_root / "steward"),
                        "run_as_user": "agent-steward",
                        "launcher": "/usr/local/sbin/forge/run-steward.sh"}}
try:
    _sym_policy = validate_or_none = td.validate_launch_policy(_sym_doc, project_root=_sym_root)
    check(True, "an entry under a symlinked root is accepted")
    # And the stored path is the UNRESOLVED one, matching what the plugin returns for the
    # same input — that equality is the actual cross-language property.
    check(
        _sym_policy["steward"]["project_dir"] == Path(os.path.normpath(str(_sym_root / "steward"))),
        "the stored project_dir is the unresolved path, as the plugin's also is",
    )
except td.LaunchPolicyError as e:
    check(False, f"an entry under a symlinked root is accepted (got: {e})")
    check(False, "the stored project_dir is the unresolved path, as the plugin's also is")

# --- the derived views still read like the old literals -----------------------
print("\nAGENT_PROJECT_DIRS / AGENT_RUN_AS are views of one source")

check(
    set(td.AGENT_PROJECT_DIRS) == set(td.LAUNCH_POLICY),
    "AGENT_PROJECT_DIRS covers every agent in the policy",
)
check(
    all(a in td.AGENT_PROJECT_DIRS for a in td.AGENT_RUN_AS),
    "every run-as agent also has a project dir",
)
check(
    td.AGENT_RUN_AS.get("steward") == ("agent-steward", "/usr/local/sbin/forge/run-steward.sh"),
    "AGENT_RUN_AS still yields the (user, launcher) tuple the launch path unpacks",
)
check(
    all(e["run_as_user"] is None for a, e in td.LAUNCH_POLICY.items() if a not in td.AGENT_RUN_AS),
    "no agent outside AGENT_RUN_AS carries a run_as_user",
)

print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("all agent launch policy checks passed")
