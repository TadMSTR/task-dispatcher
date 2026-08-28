"""Tests for the console entry point and `python -m task_dispatcher`.

WHY --version IS ANSWERED BEFORE cli IS IMPORTED. cli.py loads the agent roster at module
level and raises when it cannot — deliberately, because a tick that degrades to an empty
roster is how steward gets launched as the wrong user (vikunja#404). --version is the
deploy drift-check surface (vikunja#535 gap 4), so it has to answer on a host where the
roster is missing, unreadable or malformed: precisely when someone is checking what is
installed. tests/test_version_no_roster.py already pins that against a REAL broken roster
in a subprocess, and stays the authority on it.

This file covers what that one cannot: `python -m task_dispatcher` as a module (__main__.py
was at 0%), and that _console() with no --version actually reaches cli.main().

runpy, not a subprocess, for the module case — a subprocess would need
COVERAGE_PROCESS_START wiring to be measured, and the property under test is which code
path runs, not that a shell can find the interpreter. The subprocess form is covered by
test_version_no_roster.py.
"""

from __future__ import annotations

import runpy
import subprocess
import sys

import pytest

import task_dispatcher


def test_version_flag_prints_the_package_version(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["task-dispatcher", "--version"])

    assert task_dispatcher._console() == 0
    assert capsys.readouterr().out.strip() == f"task-dispatcher {task_dispatcher.__version__}"


def test_console_without_version_delegates_to_cli_main(monkeypatch):
    """The other leg of _console: import cli and hand off. Nothing else may run here."""
    calls = []
    from task_dispatcher import cli

    monkeypatch.setattr(cli, "main", lambda: calls.append(True) or 7)
    monkeypatch.setattr(sys, "argv", ["task-dispatcher"])

    assert task_dispatcher._console() == 7
    assert calls == [True], "_console must delegate, not reimplement"


def test_version_is_not_matched_from_argv_zero(monkeypatch, capsys):
    """`sys.argv[1:]`, not `sys.argv`. A path containing --version is not a request."""
    calls = []
    from task_dispatcher import cli

    monkeypatch.setattr(cli, "main", lambda: calls.append(True) or 0)
    monkeypatch.setattr(sys, "argv", ["/opt/venvs/--version/bin/task-dispatcher"])

    task_dispatcher._console()
    assert calls == [True]


def test_module_entry_point_runs_console(monkeypatch, capsys):
    """`python -m task_dispatcher` must be the same entry point as the console script.

    Two ways to invoke one program is two ways for them to disagree; __main__.py existing
    but never being executed is how that disagreement stays invisible.
    """
    monkeypatch.setattr(sys, "argv", ["task_dispatcher", "--version"])

    with pytest.raises(SystemExit) as exc:
        runpy.run_module("task_dispatcher.__main__", run_name="__main__")

    assert exc.value.code == 0
    assert task_dispatcher.__version__ in capsys.readouterr().out


def test_module_entry_point_works_as_a_real_subprocess(real_popen):
    """The end-to-end form. Not measured by coverage; proves the shell invocation works.

    Takes `real_popen` because the autouse isolation stubs subprocess.Popen process-wide.
    """
    r = subprocess.run(
        [sys.executable, "-m", "task_dispatcher", "--version"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"task-dispatcher {task_dispatcher.__version__}"


def test_version_matches_pyproject():
    """Read pyproject, NOT importlib.metadata — metadata reflects what is INSTALLED.

    An editable install keeps serving the version recorded at install time, so a metadata
    comparison passes against a stale wheel and proves nothing about the tree.
    """
    import re
    from pathlib import Path

    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    declared = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)

    assert task_dispatcher.__version__ == declared
