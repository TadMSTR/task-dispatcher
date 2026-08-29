# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.4.0] - 2026-08-29

Build plan `agent-workflow-interop-2026-08`, Phase 5.2–5.5. Tickets vikunja#560, #561.

### Removed

- **`publish_nats()` and its four call sites, outright — no deprecation window.** The
  dispatcher published `tasks.submitted` / `tasks.approved` / `tasks.failed` /
  `tasks.approval-requested` by shelling out to the `nats` CLI, alongside agent-bus
  publishing the same logical events on its own subject: two subjects, two payload
  shapes, one set of events, and only one of them with a consumer.

  Verified before deleting rather than assumed — the preflight measurement was
  re-checked against the live host at build time: **zero** JetStream streams captured
  `tasks.*`, `tasks.{submitted,approved,failed,approval-requested}` appears nowhere in
  `~/repos/personal` or `~/scripts` outside this repo and its own tests, and
  `nats.conf`'s per-agent grants are scoped to `tasks.<agent>.>` subtrees that none of
  those subjects match. Every one of those publishes was being discarded on arrival.

  This also removes the dispatcher's only reason to shell out to a `nats` binary, which
  was a PATH dependency in a cron context — a class of failure this repo has hit before.

### Added

- **Correlation ids on every bus event.** All seven `bus_log()` sites now pass
  `metadata={"task_id", "run_id", "workflow_mode", "risk_level"}` via a new
  `bus_metadata()` helper. Joining a bus event to its task previously meant
  string-parsing a filename out of `artifact_path`, while the `publish_nats()` call on
  the adjacent line was already sending a structured `{"task_id": ...}` — the emitter
  with no consumer was the only one carrying the id.

  Fields with no value are omitted rather than emitted as null, so a consumer can tell
  "this event has no run" from "this event's run id failed to resolve". Only the
  stuck-run sweep holds a run record when it logs, so `run_id` is an argument rather
  than a task field.

  The build plan said six sites. There are seven — `task.dispatched` in
  `request_approval()` was missed when the plan was written, which is why
  `tests/test_bus_correlation.py` asserts the COUNT as well as the property.

- **`tests/test_bus_vocabulary.py`** — a new gate, in its own CI job. Parses the event
  types out of `cli.py` with `ast` and asserts each is declared in agent-bus's
  `event_vocab.py`, read from that repo's `main`. This is the check that would have
  caught `task.workflow_started`, emitted here for months while undeclared upstream; it
  reached the cross-agent log only because every caller happened to leave `scope` at its
  default. agent-bus v0.4.0 turned that accident into a rejection under
  `AGENT_BUS_STRICT_VOCAB=enforce`.

  It refuses a computed event type rather than skipping it, and it has no
  skip-on-no-network path — a check that passes when it could not read the upstream
  reports the same result whether or not the two sides agree.

- **`tests/fixtures/launch-policy-corpus.json`** — 27 accept/reject cases for the launch
  policy validator, loaded by this suite AND by the CloudCLI plugin's, which fetches it
  from this repo's `main`. The plugin's `launch-policy.ts` carried a comment saying the
  Python side "must keep computing this the same way"; this is that comment as a test.
  Resolved values are compared, not just verdicts — the `.resolve()` divergence that
  motivated this changed what a spawn's cwd would be long before it changed any
  accept/reject answer.

- **The agent-bus dependency pin moves to `945ea2d` = `v0.4.0`**, which is where
  `AGENT_BUS_STRICT_VOCAB` and the JetStream publish live. Required rather than
  cosmetic: the new bus-vocabulary gate checks this dispatcher's emitted types against
  that release's vocabulary, and under `enforce` an undeclared type is rejected outright
  instead of warned about. Still pinned by SHA — tags are mutable on GitHub and this
  installs into a root-owned venv — with the tag named in a `pyproject.toml` comment,
  because nobody can read a version out of a bare SHA.

- **The dead-letter and archive directory names are now gated** in
  `test_task_queue_vocabulary.py`. `task-dispatcher` writes
  `~/.claude/task-queue/dead-letters/` and task-queue-mcp reads it, through two
  independent literals nothing pinned together; if the writer's name drifted,
  `get_task` and `list_tasks` would silently stop finding new dead letters — vikunja#557
  recurring inside the fix for vikunja#557. Carried in from the Phase 1 audit, which
  deferred it here by name.


## [1.3.0] - 2026-08-29

Gives every launch a run record, then uses it for the two things that were impossible
without one: a concurrency cap and a stuck-run sweep.

Tracker: vikunja#559. Build plan: agent-workflow-interop-2026-08, Phases 3 and 4.

### Added

- **A run record beside every launch log.**
  `~/.claude/comms/artifacts/task-launches/<agent>-<task8>.json`, carrying `run_id`,
  `task_id`, `agent`, `launched_by`, `run_as_user`, `launcher`, `workflow_mode`,
  `started`, `pid`, `pid_start_ticks`, `ended`, `exit_code` and `log_path`.

  A launch was previously a side effect — `Popen(...)` and return, no pid, no exit code,
  no wait. Nothing recorded that a session had started, so nothing could observe that one
  had stopped, which is why 13 tasks sat at `in-progress` from as far back as 2026-05-28
  with no mechanism in the system able to notice them.

  The record is a **sibling** of the log, not a replacement. `<agent>-<task8>.log` is
  parsed by the CloudCLI task-queue plugin and matched by the launch-log retention job
  (vikunja#545); renaming it to make room would have silently stopped both.

- **`FORGE_RUN_ID` and `FORGE_TASK_ID` in the launched session's environment**, next to
  `FORGE_WORKFLOW_MODE`. This is what makes a Langfuse trace joinable back to the task
  that paid for it — "what did this build cost" was previously unanswerable.

  For the one run-as agent `sudo` scrubs the environment, so they travel as
  `--run-id`/`--task-id` flags to `run-steward.sh` instead. Those flags are passed **only
  if the deployed launcher is found to accept them**: it refuses unknown options outright,
  and the launcher deploys from a different repository by a root script, so the two are
  routinely out of step. Until `host-forge/scripts` is redeployed the flags are withheld
  and a warning is logged; the record itself is unaffected.

- **The security-audit launch is recorded too.** It is a third launcher, and it launches
  headlessly even in `semi-auto`, which makes it the commonest kind of concurrent session
  on this host. A cap that counted the other two and not this one would be a cap in name
  only. Its log stays at `~/.pm2/logs/security-audit-<build>.log`, keyed by build name;
  the record points at it from the launch directory.

- **Concurrency caps.** `DISPATCHER_MAX_CONCURRENT_RUNS` (default 4) and
  `DISPATCHER_MAX_RUNS_PER_AGENT` (default 1), counted from live run records. There was no
  cap at all: ten `auto` tasks landing in one tick spawned ten concurrent Claude sessions
  against one API key. Non-positive is unlimited.

  A task that cannot get a slot **stays `submitted`** and is re-read next tick. No new
  status was invented — a status is a vocabulary shared with task-queue-mcp and the
  plugin's parity gate, and adding one on a single side is the drift class this
  dispatcher's own vocabulary tests exist to catch.

- **A stuck-run sweep.** When a run record is reaped dead and its task is still
  `in-progress`, the task is moved to `failed` through task-queue-mcp's control API at
  `POST /tasks/{id}/update` with `on_behalf_of`, so the history records `actor: operator`
  alongside the agent's name and reads as a sweep years later rather than as the agent
  having quietly closed its own work. There is **no fallback to writing the queue file**:
  without the shared secret the sweep logs and does nothing.

  It is evidence-gated and cannot reach the pre-existing backlog. It enumerates dead runs
  and looks their task up, so a task with no run record is structurally out of reach.

- **`DISPATCHER_MAX_RUN_SECONDS`** (default 6h). A run still alive past it has its
  concurrency slot released and is marked `reaped: "max-runtime"`. It does **not** end the
  session and does **not** touch the task: sweeping the task of an agent that is
  demonstrably still working would fabricate a failure, and would strand the work as well,
  since `completed` is only reachable from `in-progress`.

### Fixed

- **A dead OAuth burned a launch attempt per queued task per tick.**
  `alert_auth_blocked()` debounced the *alert* at 900s but not the *attempt*, so a
  credential outage sent every queued task to `routing-failed` with a retry backoff —
  turning a 15-minute outage into a queue that needed chasing by hand. Launches are now
  held at `submitted` while the same stamp is fresh.

### Changed

- **`launch_kind()` is the single decision about what a tick will do with an approved
  task**, called by both the backpressure gate and the dispatch branch. The gate has to
  run before the approval is written (a held task cannot stay `submitted` once `approved`
  is on disk), which made it a second reader of a decision the dispatch branch already
  made. One function, two callers.
- **`launch_log_name()` is the one producer of the `<agent>-<task8>.log` convention.** It
  was previously an inline f-string with no name, so the reader in the plugin had nothing
  to be pinned to.
- **`read_env_file()` extracted from `load_agent_env()`** — the sweep needs the same parse
  against `~/.secrets/*.env`.
- **`_auth_stamp_age()` extracted from `alert_auth_blocked()`**, now read by the alert and
  the launch gate.
- CI coverage floor raised 94 → 96 (measured 96.64%, up from 95.19%).

### Known gaps

- **`exit_code` is null for every dispatcher-launched run**, with `reaped: "pid-gone"`.
  This is deliberate and not a stub. A cron tick spawns a detached child and exits, so the
  child is reparented and its status is reaped by init — there is no `waitpid()` to call
  and no surviving `/proc` entry to read one from. A zero would be a counter reporting
  success for something nobody observed succeed, which is the failure this record exists
  to expose. The CloudCLI plugin is a long-lived process and records a real exit code for
  the runs it starts.
- **`run-steward.sh` exports `FORGE_WORKFLOW_MODE` before sourcing the agent env file**,
  so a key of that name in the env file would silently replace it. The new
  `FORGE_RUN_ID`/`FORGE_TASK_ID` exports are placed after the source for that reason;
  moving the mode export was left out of scope as it cannot be exercised without `sudo`.

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
