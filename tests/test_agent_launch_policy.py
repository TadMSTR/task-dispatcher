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
fixtures rather than the host's real directories.

WHAT THIS TEST NO LONGER PROVES, AND WHO DOES. It used to load the SHIPPED roster, because
the roster sat beside the dispatcher in host-forge/scripts. It no longer does. The live
roster stays at ~/scripts/agent-launch.yml — the CloudCLI plugin hardcodes that path — and
tests/fixtures/agent-launch.yml here is a COPY. So this file proves the loader and the
validator are correct; it does NOT prove the live roster is well-formed, and the fixture
can drift from it. Validating the live file is host-forge/scripts' job.
"""

from __future__ import annotations

import importlib
import json
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

# Plant the roster at the DEFAULT location rather than setting AGENT_LAUNCH_POLICY. The
# default is the thing under test: it must be $HOME-relative so that this package, which
# deploys to a venv, and the CloudCLI plugin, which hardcodes ~/scripts/agent-launch.yml,
# resolve to the same file. Overriding via env would route around exactly that.
os.environ.pop("AGENT_LAUNCH_POLICY", None)
REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "agent-launch.yml"
_DEFAULT_ROSTER = _HOME / "scripts" / "agent-launch.yml"
_DEFAULT_ROSTER.parent.mkdir(parents=True)
_DEFAULT_ROSTER.write_text(FIXTURE.read_text())

# Import the INSTALLED package, not a loose file — see the note in
# test_dispatcher_headless_chain.py. HOME and the default roster are already in place.
td = importlib.import_module("task_dispatcher.cli")

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
    except Exception as e:
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


# --- the default roster path -------------------------------------------------
print("\nthe default roster path (~/scripts/agent-launch.yml)")

check(FIXTURE.is_file(), "tests/fixtures/agent-launch.yml exists")

# THE REGRESSION THIS PINS. If LAUNCH_POLICY_PATH ever goes back to resolving from
# __file__, this fails: __file__ is under the installed package, nowhere near $HOME.
# That regression would hard-fail every cron tick in production while the CloudCLI
# plugin carried on reading the real roster — the two consumers silently disagreeing,
# which is vikunja#523 all over again. $HOME dependence is the requirement here, not
# a defect to be engineered away.
check(
    td.LAUNCH_POLICY_PATH.resolve() == _DEFAULT_ROSTER.resolve(),
    "LAUNCH_POLICY_PATH defaults to $HOME/scripts/agent-launch.yml, not to __file__",
)
check(
    Path(__file__).resolve().parent.parent not in td.LAUNCH_POLICY_PATH.resolve().parents,
    "the default does not resolve inside the package tree",
)

shipped_raw = yaml.safe_load(FIXTURE.read_text())
shipped = td.validate_launch_policy(shipped_raw, project_root=PROJECT_ROOT)

# The exact roster the three literals used to spell, asserted as a set so a dropped
# agent is a failure rather than a silently smaller queue.
check(
    set(shipped) == {"sysadmin", "developer", "research", "writer", "security", "steward"},
    "the fixture carries exactly the six agents the old literals did",
)
check(
    [a for a, e in shipped.items() if e["run_as_user"]] == ["steward"],
    "steward is the only run-as agent",
)
check(
    shipped["steward"]["run_as_user"] == "agent-steward"
    and shipped["steward"]["launcher"] == "/usr/local/sbin/forge/run-steward.sh",
    "steward's run-as pair is the one sudoers permits",
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
rejects(
    good(x={"project_dir": "~/.claude/projectsX/evil"}), "a sibling dir sharing the root's prefix"
)

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
    good(
        x={
            "project_dir": "~/.claude/projects/steward",
            "launcher": "/usr/local/sbin/forge/run-steward.sh",
        }
    ),
    "a launcher without a run_as_user",
)
for bad_user in ("root", "ted", "agent-steward; rm -rf /", "AGENT-STEWARD", "agent_steward", ""):
    rejects(
        good(
            x={
                "project_dir": "~/.claude/projects/steward",
                "run_as_user": bad_user,
                "launcher": "/usr/local/sbin/forge/run-steward.sh",
            }
        ),
        f"run_as_user {bad_user!r}",
    )
for bad_launcher in (
    "/usr/bin/env",
    "/usr/local/sbin/forge/../../../bin/sh",
    "run-steward.sh",
    "/usr/local/sbin/forgery/run-steward.sh",
):
    rejects(
        good(
            x={
                "project_dir": "~/.claude/projects/steward",
                "run_as_user": "agent-steward",
                "launcher": bad_launcher,
            }
        ),
        f"launcher {bad_launcher!r}",
    )

# A launcher that does not exist is SHAPE-valid on purpose: existence is checked at
# launch time by os.access, which reports it as a routing failure naming the script and
# telling the operator to deploy it. Validating existence here would make an undeployed
# launcher stop the dispatcher from importing at all — for every other agent too.
shape_ok = td.validate_launch_policy(
    good(
        x={
            "project_dir": "~/.claude/projects/steward",
            "run_as_user": "agent-steward",
            "launcher": "/usr/local/sbin/forge/does-not-exist.sh",
        }
    ),
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

_sym_doc = {
    "steward": {
        "project_dir": str(_sym_root / "steward"),
        "run_as_user": "agent-steward",
        "launcher": "/usr/local/sbin/forge/run-steward.sh",
    }
}
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

# --- the shared cross-language corpus (plan Phase 5.5) ------------------------
#
# tests/fixtures/launch-policy-corpus.json is validated by BOTH this validator and the
# CloudCLI plugin's validateLaunchPolicy(). The plugin fetches it from this repo's
# `main`; this side reads it from disk. See the corpus's own $schema_notes for why it
# exists and why a verdict-only comparison would not have caught the divergence that
# motivated it.
print("\nshared accept/reject corpus (must agree with the plugin, case for case)")

_CORPUS = json.loads((REPO_ROOT / "tests" / "fixtures" / "launch-policy-corpus.json").read_text())
_CORPUS_ROOT = _HOME / ".claude" / "projects"


def _sub(value):
    """Substitute {HOME} through a nested structure.

    Each side substitutes its OWN home — Python expands `~` against the ambient $HOME
    while the TypeScript side expands it against an argument, so a literal path in the
    corpus would make the two incomparable for exactly the `~` cases.
    """
    if isinstance(value, str):
        return value.replace("{HOME}", str(_HOME))
    if isinstance(value, dict):
        return {k: _sub(v) for k, v in value.items()}
    return value


check(bool(_CORPUS.get("cases")), "the corpus parsed and is non-empty")

for _case in _CORPUS["cases"]:
    _name = _case["name"]
    _policy = _sub(_case["policy"])
    try:
        _got = td.validate_launch_policy(_policy, project_root=_CORPUS_ROOT)
        _verdict = "accept"
        _error = None
    except td.LaunchPolicyError as e:
        _got = None
        _verdict = "reject"
        _error = str(e)

    if _verdict != _case["expect"]:
        detail = f"got {_verdict}" + (f" ({_error})" if _error else "")
        check(False, f"corpus[{_name}]: expected {_case['expect']}, {detail}")
        continue
    check(True, f"corpus[{_name}]: {_verdict}")

    if _case["expect"] != "accept":
        continue

    # A verdict match is not enough. The RESOLVED values are what reach a subprocess —
    # the .resolve() divergence this corpus exists for changed the spawn cwd long
    # before it changed any accept/reject answer.
    _want = _sub(_case["resolved"])
    _flat = {
        agent: {
            "project_dir": str(entry["project_dir"]),
            "run_as_user": entry["run_as_user"],
            "launcher": entry["launcher"],
        }
        for agent, entry in _got.items()
    }
    check(
        _flat == _want,
        f"corpus[{_name}]: resolved values match"
        if _flat == _want
        else f"corpus[{_name}]: resolved {_flat} != expected {_want}",
    )

print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("all agent launch policy checks passed")
