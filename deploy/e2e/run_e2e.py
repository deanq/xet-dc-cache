#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["huggingface_hub[hf_xet]>=0.24"]
# ///
"""End-to-end validation of the deploy/e2e Docker harness scenarios.

Boots the deploy/e2e docker-compose stack and drives real `hf_hub_download`s
through it against the live HuggingFace CDN. Scenarios live in scenarios/ and
are dispatched by name (see scenarios.ORDER); select a subset with
E2E_SCENARIOS=name1,name2 (default: all).

    uv run deploy/e2e/run_e2e.py            # from repo root
    E2E_SCENARIOS=peering uv run deploy/e2e/run_e2e.py
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    from harness import COMPOSE, NODES, Report, reset_cache, sh, wait_healthy
    from scenarios import ORDER, parse_scenarios

    rep = Report()
    selected = parse_scenarios(os.environ.get("E2E_SCENARIOS"))
    started = False
    try:
        print(f"scenarios: {', '.join(selected)}")
        print("== building + starting stack ==")
        # Only reset/wait on nodes actually defined in this compose stack, in
        # case harness.NODES ever lists a node the compose file doesn't (a
        # missing service would otherwise hang wait_healthy). Today all four
        # (node-a/b/c + node-evict) are defined, so this filter is a no-op guard.
        active = sh(*COMPOSE, "config", "--services").stdout.split()
        for node in NODES:
            if node in active:
                reset_cache(node)
        sh(*COMPOSE, "up", "-d", "--build")
        started = True
        wait_healthy([n for n in NODES if n in active])
        print("all nodes healthy\n")
        for name, fn in ORDER:
            if name in selected:
                fn(rep)
        print("== summary ==")
        print("RESULT:", "PASS" if rep.ok() else "FAIL")
        return 0 if rep.ok() else 1
    finally:
        if started:
            sh(*COMPOSE, "down", check=False)


if __name__ == "__main__":
    sys.exit(main())
