# task-dispatcher

The launch-control plane for an agent fleet: a cron-driven process that reads a directory
of YAML task files, decides which ones may start a headless agent session, and launches
them.

It is the single path by which any agent starts. That is the whole design premise, and it
is why this repository is shaped the way it is — pinned dependencies, a version string, a
deploy target outside any git working tree, and tests that fail loudly rather than skip.

## What it does

Every two minutes it:

1. Reads task files from `~/.claude/task-queue/`.
2. Validates each against the queue vocabulary — statuses, task types, workflow modes.
   These sets are shared with [task-queue-mcp](https://github.com/TadMSTR/task-queue-mcp),
   which owns the schema, and CI asserts they have not drifted.
3. Applies the approval rules for the task's `workflow_mode` (`auto`, `semi-auto`,
   `manual-then-auto`) and risk level.
4. Reaps the previous tick's launches: a run whose process is gone is stamped ended, and
   if its task is still `in-progress` it is swept to `failed` through the control API's
   operator route.
5. Launches the target agent headlessly — subject to the concurrency caps — or notifies
   and waits for a human.
6. Archives expired tasks and retries ones whose routing failed.

## Run records

Every launch writes `~/.claude/comms/artifacts/task-launches/<agent>-<task8>.json` beside
the log it already wrote. It is a sibling, never a replacement: the `<agent>-<task8>.log`
name is parsed by the CloudCLI task-queue plugin and matched by the launch-log retention
job, and both would silently stop matching if it moved.

```json
{
  "run_id": "…", "task_id": "…", "agent": "developer",
  "launched_by": "dispatcher", "run_as_user": null, "launcher": null,
  "workflow_mode": "auto", "started": "…", "pid": 12345,
  "pid_start_ticks": 990312, "ended": null, "exit_code": null,
  "log_path": "…/developer-8fb669e5.log"
}
```

`run_id` and `task_id` also reach the session as `FORGE_RUN_ID` and `FORGE_TASK_ID`,
which is what lets a Langfuse trace be joined back to the task that paid for it. For the
one run-as agent, `sudo` scrubs the environment, so they travel as `--run-id`/`--task-id`
flags instead — and only if the *deployed* launcher is found to accept them, because it
refuses unknown options outright and the two artefacts deploy separately.

**`exit_code` is null for a dispatcher launch, and that is not a gap to fill.** A cron
tick spawns a detached child and exits, so the child is reparented and its status is
reaped by init: there is no `waitpid()` to call and no `/proc` entry left to read. The
record says `reaped: "pid-gone"` and leaves the code null. A fabricated zero would be a
counter reporting success for something nobody observed succeed. The CloudCLI plugin,
which is a long-lived process and *can* observe its own children exit, records a real
code for the runs it starts.

`pid_start_ticks` is field 22 of `/proc/<pid>/stat`, recorded so a recycled pid cannot
keep a finished run counted as live — a concurrency cap that has silently stopped issuing
launches looks exactly like a quiet queue.

## Backpressure

| Variable | Default | Purpose |
|---|---|---|
| `DISPATCHER_MAX_CONCURRENT_RUNS` | `4` | Live sessions across all agents |
| `DISPATCHER_MAX_RUNS_PER_AGENT` | `1` | Live sessions for one agent |
| `DISPATCHER_MAX_RUN_SECONDS` | `21600` | After this a live run's slot is released |

Non-positive means unlimited, for all three.

A task that cannot get a slot **stays `submitted`** and is re-read from scratch next tick.
There is deliberately no "queued" or "throttled" status: a status is a vocabulary shared
with task-queue-mcp and the CloudCLI plugin's parity gate, and adding one on a single side
is the drift class this dispatcher's vocabulary tests exist to catch.

`DISPATCHER_MAX_RUN_SECONDS` frees the slot and does nothing else — it does not end the
session and does not touch the task. Sweeping the task of an agent that is demonstrably
still working would fabricate a failure, and would strand the work as well: `completed` is
only reachable from `in-progress`, so that agent's own close would then be refused.

## Install

```bash
pip install -e .          # runtime
pip install -e ".[dev]"   # plus ruff and coverage
pip install -e ".[bus]"   # plus the agent-bus event-log client (optional)
```

## Run

```bash
task-dispatcher              # one pass over the queue
task-dispatcher --version    # what is installed
```

`--version` answers without reading the agent roster, so it stays usable as a deploy
drift-check on a host that is not yet fully configured. Everything else requires a valid
roster and fails loudly without one — see below.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_LAUNCH_POLICY` | `~/scripts/agent-launch.yml` | The agent roster |
| `SCOPED_MCP_BEARER_TOKEN` | — | Resolved per-agent at launch; never stored here |
| `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` | — | Checked for usability before launching |
| `TASK_QUEUE_MCP_REF` | `main` | Test-only: the ref the queue-vocabulary check compares against |
| `AGENT_BUS_REF` | `main` | Test-only: the ref the bus-vocabulary check compares against |
| `TASK_QUEUE_API` | `http://127.0.0.1:8485` | task-queue-mcp's control API, used for the sweep |
| `TASK_QUEUE_API_SECRET` | — | Read from `~/.secrets/task-queue-mcp.env` or `forge.env` if unset |

See [Backpressure](#backpressure) for the three concurrency variables.

## The agent roster

`agent-launch.yml` maps each agent to a project directory and, optionally, to a user and
launcher it must be started through. It is **not** shipped by this package. It lives at
`~/scripts/agent-launch.yml` because a second consumer — the CloudCLI task-queue plugin —
hardcodes that path, and two copies of one roster is a bug this project has already had.

`tests/fixtures/agent-launch.yml` is a fixture. It is not the live roster and editing it
changes nothing about how any agent launches.

## Three invariants worth knowing before changing anything

**A missing or malformed roster is a hard error.** It must never degrade to an empty
mapping. An empty roster silently drops `run_as_user` for every agent, which means an
agent that is supposed to run under its own identity instead runs as the calling user —
a session that looks correct in every log while holding none of the right credentials.
Failing the tick loudly is strictly better.

**An agent with `run_as_user` set is launched only through its `launcher`.** The
dispatcher must never read that agent's environment file itself; the launcher runs as
that user and is the only thing that can. `sudo` independently pins both the user and the
launcher path, so the roster selects from what is already permitted and cannot widen it.

**A stuck task is only ever closed through the operator route.** The sweep posts to
`/tasks/{id}/update` with `on_behalf_of`, which records `actor: operator` alongside the
agent's name. This dispatcher holds `atomic_write()` and could set `status: failed`
itself in one line — and that is exactly the dishonest close the route was built to
replace, because in history it is indistinguishable from the agent having quietly closed
its own work. Without the shared secret the sweep does nothing at all.

## Tests

Two harnesses, deliberately. Most tests are pytest; seven predate it or need their own
process, and remain standalone scripts with their own `check()` harness, each exiting
non-zero on failure.

```bash
pytest                                          # the unit suite — 293 tests

python tests/test_agent_launch_policy.py        # loader + validator + shared corpus
python tests/test_dispatcher_headless_chain.py  # launch decisions, nothing spawned
python tests/test_version_no_roster.py          # --version without a roster
python tests/test_gitleaks_gate.py              # the leak gate fires (needs gitleaks)
python tests/test_task_queue_vocabulary.py      # parity with task-queue-mcp (needs network)
python tests/test_bus_vocabulary.py             # parity with agent-bus (needs network)
python tests/test_bus_emitter_live.py           # the bus emitter is real (needs [bus])
```

The seven are not pytest tests and are excluded from collection (`conftest.py`'s
`collect_ignore`). Each redirects `$HOME` and imports the dispatcher at module scope with a
setup specific to what it pins, so importing them under pytest would run their checks at
collection time against a `$HOME` they did not set up. They keep one CI step each, which is
also what keeps a failure attributable to a single file.

`tests/test_ci_wiring.py` is what holds that split together: it asserts every
`tests/test_*.py` is either collected by pytest or named in `collect_ignore` **and** run by
its own named CI job. Without it, moving a file into `collect_ignore` is a one-line way to
stop running it while every check stays green.

### The three parity gates

| Gate | Upstream | What drifts without it |
|---|---|---|
| `test_task_queue_vocabulary.py` | task-queue-mcp `main` | Statuses, task types, workflow modes — and the `dead-letters`/`archive` directory names, where this dispatcher is the writer and the MCP is the reader |
| `test_bus_vocabulary.py` | agent-bus `main` | Event types. `task.workflow_started` was emitted here for months while undeclared upstream; under `AGENT_BUS_STRICT_VOCAB=enforce` that is now a rejection, not a warning |
| `tests/fixtures/launch-policy-corpus.json` | consumed by the CloudCLI plugin from **this** repo's `main` | The two independent launch-policy validators. They have already disagreed once — Python called `.resolve()` on the project root while the TypeScript side did a plain join, and on forge that made the dispatcher module fail to import on every tick |

None of them has a "no network, skip" path, on purpose. A check that quietly passes when
it could not read the upstream is indistinguishable from one that verified something. The
same applies to the gitleaks and bus-emitter gates.

Each of the first two lives in its own CI job. They read different upstreams, and sharing
a job would mean an agent-bus change and a task-queue-mcp change produce the same red —
with the first failure preventing the second from running at all.

The corpus compares **resolved values**, not just accept/reject verdicts. The `.resolve()`
divergence changed what a spawn's `cwd` would be long before it changed any verdict, so a
verdict-only comparison would have reported the two implementations in agreement
throughout.

### Coverage

CI enforces a floor of 96%. A local run measures 96.63%; CI has historically measured
about 0.3 points higher, and that gap is environmental and expected — do not read it as a
regression. The floor is a ratchet, not a target: raise it when the measured value rises,
never lower it to make a commit go green.

```bash
coverage run -p -m pytest -q
for t in tests/test_agent_launch_policy.py \
         tests/test_dispatcher_headless_chain.py \
         tests/test_version_no_roster.py; do
  coverage run -p --source=task_dispatcher "$t" > /dev/null
done
coverage combine && coverage report --fail-under=96
```

**Both passes are required.** `pytest` alone measures 93.28%; the standalone scripts carry
the remaining ~3 points. Collapsing this to a single `pytest --cov` invocation drops the number
below the floor and fails for a reason that looks like a regression but is not one.

The three test files not listed above are excluded from measurement because this job does
not provide what they need (a `gitleaks` binary, the network, the `bus` extra), and none of
them contributes coverage anyway — each runs the dispatcher in a subprocess or not at all.

## License

MIT — see [LICENSE](LICENSE).
