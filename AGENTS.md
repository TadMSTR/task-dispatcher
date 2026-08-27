# AGENTS.md

Notes for agents and humans changing this repository.

## What this is

The launch-control plane for an agent fleet. Cron runs it every two minutes; it is the
only path by which an agent session starts. Assume any change here can stop the entire
fleet from launching, and that the failure will be two minutes old before anyone sees it.

## Layout

```
src/task_dispatcher/__init__.py   # version + console entry point (--version lives here)
src/task_dispatcher/cli.py        # everything else
tests/                            # standalone scripts, not pytest
tests/fixtures/agent-launch.yml   # FIXTURE COPY — not the live roster
```

## Two files this package depends on but does not ship

Both live in the operator's `~/scripts` and are versioned in a different repository. They
are runtime dependencies that no packaging metadata expresses, so they are recorded here.

| Path | Relationship |
|---|---|
| `~/scripts/agent-launch.yml` | The live agent roster. Read at import. Also read by the CloudCLI task-queue plugin, which hardcodes this path. |
| `~/scripts/temporal-workflow-start.sh` | Invoked by path, not imported. Survives independently of this package. |

**Do not vendor the roster into this repository as the live file.** It has two consumers,
and giving each its own copy is a bug this project has already shipped once: the plugin's
copy drifted, lost its entry for the run-as agent, and its launch button stopped working
for that agent entirely. One file, two readers, one path.

`tests/fixtures/agent-launch.yml` is a copy for tests only. It can drift from the live
roster, and that is a known limit: these tests prove the loader and validator are correct,
not that the live roster is well-formed. Validating the live file belongs to whichever
repository holds it.

## Invariants — do not "simplify" these

**The roster default must stay `$HOME`-relative.** Not `Path(__file__).parent`. This
package installs to a venv; a `__file__`-relative default resolves next to the installed
module, finds nothing, and hard-fails every tick — while the plugin carries on reading the
real roster. `tests/test_agent_launch_policy.py` fails if this regresses.

**A missing or malformed roster raises.** It must never return `{}`. An empty roster drops
`run_as_user` for every agent, so an agent meant to run under its own identity runs as the
caller instead — correct-looking in every log, holding none of the right credentials.

**An agent with `run_as_user` set is launched only through its `launcher`.**
`load_agent_env()` must never be called for such an agent: the launcher runs as that user
and is the only thing that can read its environment file.

**`--version` is answered in `_console()`, before `cli` is imported.** Because importing
`cli` loads the roster and raises when it cannot, and `--version` is the deploy
drift-check — it has to answer on exactly the misconfigured hosts where importing would
fail. `tests/test_version_no_roster.py` pins both halves.

## Tests

They are standalone scripts with a `check()` harness, not a pytest suite. Run them
directly; each exits non-zero on failure. CI gives each one **its own step** so an early
failure cannot mask a later one.

The vocabulary parity check runs as a **separate CI job**. It compares this package's
queue vocabulary against `task-queue-mcp`'s `main` over HTTPS, so it goes red when that
repository changes — an alarm about the world, not about your commit. Keeping it in its
own job stops it being confused with a genuine regression. It has no skip path: a check
that passes when it could not reach the upstream reports the same thing whether or not the
two sides agree.

## Releasing

1. Bump `__version__` in `src/task_dispatcher/__init__.py` and update `CHANGELOG.md`.
2. Merge to `main` with CI green.
3. Tag `vX.Y.Z` and push the tag — `release.yml` cuts the GitHub Release.
4. Deploy is a separate, operator-side step: install the tag into the venv and confirm
   `task-dispatcher --version` matches before trusting anything else.

Do not treat a green test run as evidence of a deploy. Check the deploy timestamp against
the test timestamp; confusing the two has produced a false pass here before.
