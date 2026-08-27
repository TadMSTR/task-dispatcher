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

Standalone scripts, not a pytest suite — each exits non-zero on failure:

```bash
python tests/test_agent_launch_policy.py       # loader + validator closed sets
python tests/test_dispatcher_headless_chain.py # launch decisions, nothing spawned
python tests/test_version_no_roster.py         # --version without a roster
python tests/test_task_queue_vocabulary.py     # parity with task-queue-mcp (needs network)
```

The vocabulary check has no "no network, skip" path on purpose. A check that quietly
passes when it could not read the upstream is indistinguishable from one that verified
something.

## License

MIT — see [LICENSE](LICENSE).
