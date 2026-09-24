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
    return [s.strip() for s in env.split(",") if s.strip()]
