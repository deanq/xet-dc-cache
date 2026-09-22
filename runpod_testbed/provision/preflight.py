"""Pre-flight checks run before a demo provisions anything.

Fails loudly BEFORE spending money so problems surface early (at your desk),
not on stage: missing secrets, an invalid/placeholder config, or orphaned
resources still billing from a previous run.
"""
from __future__ import annotations
import os
import sys

_REQUIRED_ENV = ("RUNPOD_API_KEY", "HF_TOKEN")


def check(env=None, graphql=None, config_load=None, config_path=None) -> list[str]:
    """Return a list of human-readable problems ([] means good to go).

    Dependencies are injectable for testing: env (mapping), graphql (callable
    like fleet._graphql), and config_load (callable like config.load).
    """
    env = os.environ if env is None else env
    config_path = config_path or "runpod_testbed/config.toml"
    problems: list[str] = []

    for var in _REQUIRED_ENV:
        if not env.get(var):
            problems.append(f"{var} not set (add it to runpod_testbed/.env)")

    if config_load is None:
        from runpod_testbed import config as _config
        config_load = _config.load
    try:
        config_load(config_path)
    except Exception as e:
        problems.append(f"config invalid: {e}")

    # Only query the account if we have a key to query with.
    if env.get("RUNPOD_API_KEY"):
        if graphql is None:
            from runpod_testbed.provision.fleet import _graphql as graphql
        try:
            data = graphql(
                "query { myself { pods { id } endpoints { id } } }",
                env["RUNPOD_API_KEY"],
            )["data"]["myself"]
            n_pods, n_eps = len(data["pods"]), len(data["endpoints"])
            if n_pods or n_eps:
                problems.append(
                    f"orphaned resources on the account: {n_pods} pods, "
                    f"{n_eps} endpoints — tear them down before the demo")
        except Exception as e:
            problems.append(f"could not query the Runpod account (key/network?): {e}")

    return problems


def main() -> None:
    problems = check()
    if problems:
        print("PREFLIGHT FAILED:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("preflight OK: env set, config valid, no orphaned resources")


if __name__ == "__main__":
    main()
