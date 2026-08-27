# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-08-27

First release as a standalone package. Previously a single file in a heterogeneous
scripts repository, run by cron directly out of a git working tree.

The commit history from that repository is preserved — this is a `git filter-repo`
extraction, not a fresh copy.

### Added

- Packaging: `pyproject.toml`, pinned `httpx` and `pyyaml`, console entry point.
- `--version`, answered before the agent roster is read so it remains usable as a deploy
  drift-check on a host where the roster is missing or malformed.
- `tests/test_version_no_roster.py`, pinning both halves of that behaviour: `--version`
  works without a roster, and a real run without one still fails loudly.
- GitHub Actions CI on push and pull request, across Python 3.11/3.12/3.13, with ruff
  blocking. The previous home ran these tests only on a manual trigger.
- `.gitleaks.toml`, `.gitignore` with secrets patterns, `SECURITY.md`, `AGENTS.md`.
- A `secret-scan` CI job that installs a pinned gitleaks, asserts the gate still fires on a
  planted secret, then scans full history. The config previously shipped without anything in
  CI running it.
- `tests/test_gitleaks_gate.py` — proves the leak gate fires, including on a secret sharing
  a line with each credential variable name this codebase mentions. Carries an explicit
  control assertion, because a probe that cannot fire reports "clean" and a broken probe
  reports the same thing.
- `tests/test_task_id_validation` — pins the exact 8-4-4-4-12 task id form.

### Changed

- **The agent roster now resolves from `$HOME`, not from the module's own directory.**
  This package deploys to a venv, so a `__file__`-relative default would look beside the
  installed module, find nothing, and hard-fail every tick — while the CloudCLI plugin,
  which hardcodes `~/scripts/agent-launch.yml`, carried on reading the real file. The two
  consumers now agree by construction.
- `--version` moved out of `main()` into the console entry point. Handling it in `main()`
  meant importing the module first, and importing the module loads the roster and raises
  when it cannot — so the drift check failed precisely on the hosts where it was needed.
- The `agent-bus` client is a real optional dependency (`[bus]` extra) instead of an
  absolute path inserted into `sys.path`, which worked on exactly one machine.
- Comments describing the run-as design now state the invariant rather than the
  exploitation path.
- Lint debt cleared: dead imports removed, `contextlib.suppress` for two
  `try`/`except`/`pass` blocks, modernised `datetime` usage, consistent formatting. Ruff
  was non-blocking in the previous repository. No behaviour change — the long-line rewrap
  was verified AST-identical.

### Security

- Task ids are validated against the exact 8-4-4-4-12 UUID form. The previous
  `[0-9a-f\-]{36}` spelling accepted 36 dashes, 36 undelimited hex characters, and any
  regrouping of the right charset. Not a traversal or shell sink — `Popen` takes a list and
  the charset excludes `/` and `.` — so this tightens a validator rather than closing a
  hole. Checked against 200 live queue ids first; all are canonical, so nothing in flight is
  rejected. The pattern was spelled three times and is now one constant.
- `.gitignore` covers core dumps. A core file from this process holds resolved bearer tokens
  in memory, and this repository is public.
- The `.gitleaks.toml` allowlist was removed rather than re-scoped. Testing showed a global
  allowlist suppresses nothing on the pinned gitleaks version — not the default ruleset, not
  even this file's own custom rules — across twelve config combinations. It failed safe, but
  a block documenting a protection it does not provide will be trusted by the next reader.
  The behaviour is now asserted by a test instead of implied by config.

### Notes

- `tests/fixtures/agent-launch.yml` is a fixture copy. The live roster stays at
  `~/scripts/agent-launch.yml`, shared with the CloudCLI task-queue plugin.
- This package invokes `~/scripts/temporal-workflow-start.sh` by path. That script is not
  vendored here. See `AGENTS.md`.
