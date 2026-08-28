# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-08-28

Deduplicates the audit-launch block that had drifted between the two dispatch functions,
and takes test coverage from 46.47% to 94.86% with the CI floor ratcheted to match.

Tracker: vikunja#551.

### Fixed

- **An audit relaunched by the routing-failed retry path had no task id in its prompt.**
  `process_submitted` and `process_routing_failed` carried the same 19-statement
  audit-launch block, and it had drifted: only the `process_submitted` copy validated the
  task id against `TASK_ID_RE` and passed `Task ID:` in the prompt. Headlessly there is no
  session-start sweep to fall back on and `build_name` does not identify a queue entry, so
  a security agent launched by the retry path had no route to claiming or closing its own
  work. The two copies are now one function and both call sites pass the id.

  The build plan recorded the copies as differing by one comment and one line wrap. They
  differed by that and by this.

### Changed

- **Three blocks extracted from the two dispatch functions**, with no other behaviour
  change. `launch_security_audit()` (two call sites), `approve_and_write()` (two),
  `request_approval()` (one — extracted for testability, not for duplication). The
  `relative_to(audit_root)` traversal-containment check now exists once instead of twice.
  All six pre-existing test files pass unmodified, which is the check that an extraction
  did not quietly change something.

- **CI coverage floor raised from 46 to 94.** The measured value of what this release
  ships is 94.86%. The build plan asked for 80; 80 would pass today while permitting
  fifteen points of silent regression, so the floor follows this repo's existing rule that
  it is the measured value and a ratchet.

- **New tests are pytest; the six standalone scripts stay standalone.** Each of those
  redirects `$HOME` and imports the dispatcher at module scope with a setup specific to
  what it pins, and rewriting 1,182 lines of working tests buys no coverage. They keep
  their own CI steps so a failure stays attributable to one file.

### Added

- **Test coverage 46.47% -> 94.86%** (`cli.py` 46.90% -> 94.75%; `__init__.py` and
  `__main__.py` both 0-or-partial -> 100%). 207 tests across ten new pytest files, covering
  the surface that only runs when something goes wrong: the whole retry and re-dispatch
  path, TTL archival, dead-lettering, Matrix notification, the Temporal launch, the auth
  guard, both launchers' refusal branches, and a full `main()` tick.

  `tests/test_security_audit.py` is the suite the traversal-containment check never had —
  it was uncovered in both copies, so "the existing tests still pass" was never evidence
  the extraction preserved it. Mutation-verified: 12/12 injected defects caught, including
  `resolve()` -> `os.path.normpath`, which reads as a simplification and is caught only by
  the symlink case.

- **`tests/test_ci_wiring.py`** — asserts every test file is either collected by pytest or
  named in `conftest.collect_ignore`, and that each ignored script is run by its specific
  CI job. Adding a test file was a two-step operation here and the second step is easy to
  forget; a pytest file listed in `collect_ignore` is run by nothing while CI stays green.

## [1.1.0] - 2026-08-28

Repairs the agent-bus emitter that v1.0.0 silently killed, stops the dispatcher writing
41 MB of duplicated unrotated log text, and puts a floor under coverage in CI.

Tracker: vikunja#550; also the code half of vikunja#552.

### Fixed

- **The agent-bus event stream was dead for the entire life of v1.0.0.** The extraction
  correctly removed a hardcoded `sys.path.insert(0, ~/repos/personal/agent-bus)`, and the
  `bus` extra meant to replace it asked for `agent-bus-client` — a distribution that does
  not exist anywhere. The real name is `agent-bus`. Because `cli.py` catches `ImportError`
  and degrades to a no-op logger, nothing failed, nothing warned, and every event type the
  dispatcher emits stopped reaching the signed audit trail at 18:32 EDT on 2026-08-27.
  The dispatcher ran clean and logged clean the whole time.

  The extra now points at `agent-bus @ git+https://github.com/TadMSTR/agent-bus`, pinned by
  commit SHA rather than by the `v0.3.1` tag it points at: tags are mutable on GitHub and
  this installs into a root-owned venv.

  Requires agent-bus ≥ v0.3.1, which is what made the client installable without the
  server's `fastmcp` / `cryptography` / `nats-py` dependencies.

- **Two log sinks, both unrotated, with byte-identical tails.** `logging.basicConfig`
  installed a `FileHandler` on `~/.claude/task-queue/dispatcher.log` *and* a
  `StreamHandler`, while cron already redirects stdout to
  `~/.pm2/logs/task-dispatcher-out.log`. Both files had reached 20.6 MB. The `FileHandler`
  is gone, making cron's redirect the single sink — chosen over the reverse because that
  path is the one logrotate bounds under vikunja#552.

  **This moves the dispatcher log.** `~/.claude/task-queue/dispatcher.log` stops being
  written; the live log is `~/.pm2/logs/task-dispatcher-out.log`. Nothing reads the old
  path at runtime — verified across `~/repos`, `~/scripts`, `~/.claude/skills` and
  `~/.claude/manifests` — but documentation referring to it is now stale.

- Roughly 63% of log lines were a 2-minute heartbeat. `=== task-dispatcher run start ===`
  and `Loaded N agent manifests: [...]` are now DEBUG. The manifest list is also sorted;
  it was emitted in dict order, which defeats `uniq` and would defeat log-based dedup.

  `=== task-dispatcher run complete ===` **stays at INFO, with its exact wording**, and
  carries a comment saying so. vikunja#479 may key a Loki `absent_over_time` cron-liveness
  alert on that string, and demoting it would silence the alert rather than fire it.

### Added

- **`tests/test_bus_emitter_live.py`** — the point of this release. It asserts the emitter
  is *wired*, not merely that the code imports: that `cli.bus_log.__module__` is
  `agent_bus_client` rather than the `except ImportError` stub, that calling it writes
  hash-chained records to the cross-agent log, and that nothing lands in the session log.

  The `try/except ImportError` fallback is deliberately **kept**. A cron job should not die
  because an optional logging client is missing. What was missing is any assertion that the
  non-degraded path is the one in production — this is the third configured-but-dead
  emitter on forge (vikunja#444, #436, #550), and the failure mode is always that success
  is reported by the absence of an error.

  No skip path: if the extra is not installed the test fails, as `test_gitleaks_gate.py`
  and `test_task_queue_vocabulary.py` do. A skip would restore the original defect one
  level up — the suite would go green on exactly the configuration the test exists to
  reject.

  It also re-derives the emitted event types from `cli.py`'s AST and fails if they disagree
  with its own list, in either direction. That check found on its first run that the
  dispatcher emits **six** event types, not the five that vikunja#550, the build plan and
  the v1.0.0 notes all counted — `task.workflow_started`, from the Temporal branch of
  `process_submitted()`, had been missed by every previous enumeration and was absent from
  agent-bus's `CROSS_AGENT_EVENTS` (fixed in agent-bus v0.3.1; the spelling inconsistency
  is tracked as vikunja#553).

- CI `bus-emitter` job — installs `.[dev,bus]` and runs the above. Separate from `test`
  because it is the only job installing from a `git+https` ref, so it is the only one that
  can go red for a network reason rather than a defect in the commit.

- CI `coverage` job — `coverage run -p` per test, `combine`, then
  `coverage report --fail-under=46`. Coverage was configured in `pyproject.toml` and
  measured by nobody, so it drifted unobserved. The floor is the measured value of what
  this release ships, not an aspirational one; raising it is vikunja#551's job.

  46 rather than the 47 the build plan specified — the same measurement. The plan read
  `coverage report` at precision 0, where 46.56% displays as `47`; deleting the now-unused
  `LOG_FILE` constant removed a *covered* statement and took it to 46.47%. Nothing
  regressed, a rounding boundary was crossed. `precision = 2` is now set so the next
  ratchet cannot hide half a point inside rounding.

### Changed

- Module docstring no longer claims transitions are logged to `dispatcher.log`.
- `LOG_FILE` removed — nothing wrote to it after the handler change, and a constant naming
  a file that is never written is worse than no constant.

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
