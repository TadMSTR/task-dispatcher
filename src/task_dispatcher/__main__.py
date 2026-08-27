"""`python -m task_dispatcher` — same entry point as the console script."""

import sys

from task_dispatcher import _console

if __name__ == "__main__":
    sys.exit(_console())
