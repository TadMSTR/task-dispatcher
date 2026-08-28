"""Forge task dispatcher — the launch-control plane for the agent fleet."""

import sys

__version__ = "1.2.0"


def _console() -> int:
    """Console entry point.

    --version is answered HERE, before `task_dispatcher.cli` is imported, and that
    ordering is the whole point rather than an optimisation. cli.py loads the agent
    roster at module level and raises LaunchPolicyError when it cannot — deliberately,
    because a cron tick that silently degrades to an empty roster is how steward gets
    launched as the wrong user. The cost is that importing cli requires a valid roster.

    --version is the deploy drift-check surface (vikunja#535 gap 4), so it has to answer
    on a host where the roster is missing, unreadable or malformed — that is precisely
    when someone is checking what is installed. Importing cli first would make the
    drift check fail exactly when it is most needed.

    tests/test_version_no_roster.py pins this. Do not "simplify" it by moving the
    check into cli.main().
    """
    if "--version" in sys.argv[1:]:
        print(f"task-dispatcher {__version__}")
        return 0
    from task_dispatcher.cli import main

    return main()
