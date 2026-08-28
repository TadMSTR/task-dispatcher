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
4. Launches the target agent headlessly, or notifies and waits for a human.
5. Archives expired tasks and retries ones whose routing failed.

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
| `TASK_QUEUE_MCP_REF` | `main` | Test-only: the ref the vocabulary check compares against |

## The agent roster

`agent-launch.yml` maps each agent to a project directory and, optionally, to a user and
launcher it must be started through. It is **not** shipped by this package. It lives at
`~/scripts/agent-launch.yml` because a second consumer — the CloudCLI task-queue plugin —
hardcodes that path, and two copies of one roster is a bug this project has already had.

`tests/fixtures/agent-launch.yml` is a fixture. It is not the live roster and editing it
changes nothing about how any agent launches.

## Two invariants worth knowing before changing anything

**A missing or malformed roster is a hard error.** It must never degrade to an empty
mapping. An empty roster silently drops `run_as_user` for every agent, which means an
agent that is supposed to run under its own identity instead runs as the calling user —
a session that looks correct in every log while holding none of the right credentials.
Failing the tick loudly is strictly better.

**An agent with `run_as_user` set is launched only through its `launcher`.** The
dispatcher must never read that agent's environment file itself; the launcher runs as
that user and is the only thing that can. `sudo` independently pins both the user and the
launcher path, so the roster selects from what is already permitted and cannot widen it.

## Tests

Two harnesses, deliberately. Most tests are pytest; six predate it and remain standalone
scripts with their own `check()` harness, each exiting non-zero on failure.

```bash
pytest                                          # the unit suite — 207 tests

python tests/test_agent_launch_policy.py        # loader + validator closed sets
python tests/test_dispatcher_headless_chain.py  # launch decisions, nothing spawned
python tests/test_version_no_roster.py          # --version without a roster
python tests/test_gitleaks_gate.py              # the leak gate fires (needs gitleaks)
python tests/test_task_queue_vocabulary.py      # parity with task-queue-mcp (needs network)
python tests/test_bus_emitter_live.py           # the bus emitter is real (needs [bus])
```

The six are not pytest tests and are excluded from collection (`conftest.py`'s
`collect_ignore`). Each redirects `$HOME` and imports the dispatcher at module scope with a
setup specific to what it pins, so importing them under pytest would run their checks at
collection time against a `$HOME` they did not set up. They keep one CI step each, which is
also what keeps a failure attributable to a single file.

`tests/test_ci_wiring.py` is what holds that split together: it asserts every
`tests/test_*.py` is either collected by pytest or named in `collect_ignore` **and** run by
its own named CI job. Without it, moving a file into `collect_ignore` is a one-line way to
stop running it while every check stays green.

The vocabulary check has no "no network, skip" path on purpose. A check that quietly
passes when it could not read the upstream is indistinguishable from one that verified
something. The same applies to the gitleaks and bus-emitter gates.

### Coverage

CI enforces a floor of 94%. CI measures 95.19%; a local run measures 94.86% — the small
gap is environmental and expected, so do not read it as a regression. The floor is a
ratchet, not a target: raise it when the measured value rises, never lower it to make a
commit go green.

```bash
coverage run -p -m pytest -q
for t in tests/test_agent_launch_policy.py \
         tests/test_dispatcher_headless_chain.py \
         tests/test_version_no_roster.py; do
  coverage run -p --source=task_dispatcher "$t" > /dev/null
done
coverage combine && coverage report --fail-under=94
```

**Both passes are required.** `pytest` alone measures ~90%; the standalone scripts carry the
remaining ~5 points. Collapsing this to a single `pytest --cov` invocation drops the number
below the floor and fails for a reason that looks like a regression but is not one.

The three test files not listed above are excluded from measurement because this job does
not provide what they need (a `gitleaks` binary, the network, the `bus` extra), and none of
them contributes coverage anyway — each runs the dispatcher in a subprocess or not at all.

## License

MIT — see [LICENSE](LICENSE).
