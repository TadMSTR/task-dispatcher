"""Asserts that every test file in this directory is actually executed by CI.

ADDING A TEST FILE IS TWO STEPS in this repo and always has been: write it, and wire it
into .github/workflows/ci.yml. The pytest suite removed that second step for pytest files —
a new one is collected automatically — but it did NOT remove it for the standalone scripts,
and it introduced a new way to lose a file entirely: a pytest file listed in conftest's
collect_ignore is collected by nothing and run by nothing, while CI stays green and the
coverage floor quietly absorbs the loss.

So this file pins the partition. Every tests/test_*.py must be in exactly one of two sets:

  collected by pytest      — measured by the coverage job automatically
  named in collect_ignore  — a standalone script, which must then have its own CI step

Nothing may be in both, and nothing may be in neither. A file in neither is the failure
this test exists to make loud: it looks like a test, it passes review as a test, and it has
never run.

This is a check about the repo's own wiring, not about the dispatcher. It parses ci.yml and
looks inside a NAMED job's `run:` commands, rather than searching the file as a whole: every
standalone script is also named in the coverage job's measurement loop, so a whole-file
substring search still finds the name after its actual test step has been deleted. It did.
"""

from __future__ import annotations

from pathlib import Path

import conftest
import pytest
import yaml

_TESTS_DIR = Path(__file__).resolve().parent
_CI = _TESTS_DIR.parent / ".github" / "workflows" / "ci.yml"


def _all_test_files() -> set[str]:
    return {p.name for p in _TESTS_DIR.glob("test_*.py")}


def test_every_test_file_is_either_collected_or_explicitly_ignored():
    """No file may fall between the two sets."""
    ignored = set(conftest.collect_ignore)
    unaccounted = _all_test_files() - ignored - {Path(__file__).name}

    # Everything not ignored is collected by pytest by construction — testpaths is
    # tests/ and the default python_files glob is test_*.py. The real risk is the
    # reverse: a name in collect_ignore that no longer exists, which silently stops
    # excluding anything and would let a rewritten file be double-run or lost.
    assert ignored <= _all_test_files(), (
        f"collect_ignore names files that do not exist: {sorted(ignored - _all_test_files())}"
    )
    assert unaccounted, "sanity: this suite should contain collected files"


# Which JOB must run each standalone script. Asserting only that the filename appears
# somewhere in ci.yml is too weak: every one of these is also named in the coverage job's
# measurement loop, so deleting a script's actual test step leaves the substring behind and
# the check passes. An earlier version of this file did exactly that and did not fire when
# the `dispatcher headless chain` step was deleted.
#
# The three in their own jobs are there for a reason recorded in ci.yml: each can go red
# for a network or upstream reason rather than for a defect in the commit, and that has to
# stay attributable.
_SCRIPT_JOBS = {
    "test_agent_launch_policy.py": "test",
    "test_dispatcher_headless_chain.py": "test",
    "test_version_no_roster.py": "test",
    "test_gitleaks_gate.py": "secret-scan",
    "test_task_queue_vocabulary.py": "vocabulary-parity",
    "test_bus_emitter_live.py": "bus-emitter",
}


def _run_commands(job: str) -> str:
    workflow = yaml.safe_load(_CI.read_text())
    return "\n".join(s.get("run", "") for s in workflow["jobs"][job]["steps"])


def test_the_script_job_map_covers_exactly_the_ignored_set():
    """The map above must not drift from conftest's collect_ignore in either direction."""
    assert set(_SCRIPT_JOBS) == set(conftest.collect_ignore)


@pytest.mark.parametrize(("script", "job"), sorted(_SCRIPT_JOBS.items()))
def test_every_ignored_script_is_run_by_its_own_ci_job(script, job):
    """A standalone script excluded from pytest MUST still be executed by CI.

    Scoped to the specific job's `run:` commands, not to the file as a whole — see the
    note on _SCRIPT_JOBS for why the looser check could not fail.
    """
    assert script in _run_commands(job), (
        f"{script} is excluded from pytest collection and is not run by the "
        f"{job!r} job — CI would go green without ever executing it"
    )


def test_the_pytest_suite_itself_is_run_by_ci():
    """The counterpart: the collected files are only measured if CI invokes pytest."""
    ci = _CI.read_text()

    assert "-m pytest" in ci or "pytest" in ci, "ci.yml never invokes pytest"


def test_the_coverage_job_enforces_a_floor():
    """`coverage report` alone exits 0 no matter what it prints.

    That is how a coverage step ends up decorative, which is the state this repo was in
    before the floor was added — configured in pyproject and measured by nobody.
    """
    assert "--fail-under" in _CI.read_text()
