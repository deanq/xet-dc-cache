"""Guarantees `runpod_testbed.*` imports resolve regardless of pytest's invocation cwd.

pytest's default rootdir-insertion (walking up through `__init__.py` files) already
makes `uv run --with pytest pytest runpod_testbed/tests -v` work when invoked from
the repo root. This conftest makes that resolution explicit and invocation-order
independent, so the suite is not silently dependent on cwd.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
