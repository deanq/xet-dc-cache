from __future__ import annotations
from . import peering, snapshot, sharded, concurrency, eviction

# name -> scenario entrypoint, in execution order.
ORDER = [
    ("peering", peering.run),
    ("snapshot", snapshot.run),
    ("sharded", sharded.run),
    ("concurrency", concurrency.run),
    ("eviction", eviction.run),
]


def parse_scenarios(env: str | None) -> list[str]:
    names = [n for n, _ in ORDER]
    if not env or not env.strip():
        return names
    selected = [s.strip() for s in env.split(",") if s.strip()]
    # Fail secure: an unknown/typo'd name must not silently select nothing.
    # Without this, `E2E_SCENARIOS=snpashot` runs zero scenarios and still
    # exits 0 (Report.ok() is all([]) == True) -- a false green for CI.
    unknown = [s for s in selected if s not in names]
    if unknown:
        raise ValueError(
            f"unknown E2E_SCENARIOS: {', '.join(unknown)}; valid: {', '.join(names)}")
    return selected
