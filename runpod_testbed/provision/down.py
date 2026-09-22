from __future__ import annotations
import subprocess
from runpod_testbed.provision.up import State

# Endpoint names deployed by worker/flash_app.py, fixed by the A/B/C overlap
# contract (config.load_str enforces overlap keys == {"A","B","C"}).
ENDPOINTS = ("xet-dl-A", "xet-dl-B", "xet-dl-C")


# Flash tracks deployed endpoints in worker/.flash, so undeploy must run from
# the same dir up.py deploys from — otherwise `flash undeploy` reports "no
# endpoints found" and silently leaves the endpoints (and their workers) billing.
_WORKER_DIR = "runpod_testbed/worker"


def cli_undeploy(env_name: str) -> None:
    # `flash undeploy` deletes by endpoint name (there is no --env); --all would
    # nuke unrelated endpoints in the account. Tolerate not-found per endpoint
    # (teardown-on-failure often runs before deploy created them). env_name is
    # unused by the CLI but kept for the teardown(flash_undeploy=...) contract.
    for name in ENDPOINTS:
        subprocess.run(["flash", "undeploy", name, "--force"],
                       cwd=_WORKER_DIR, check=False)


def teardown(fleet, state: State, flash_undeploy=cli_undeploy) -> list[str]:
    errored = []
    try:
        flash_undeploy(state.flash_env)  # endpoints first (Flash)
    except Exception:
        errored.append(state.flash_env)
    for pid in state.pods:  # then cache pods (SDK)
        try:
            fleet.terminate_pod(pid)
        except Exception:
            errored.append(pid)
    return errored


def main() -> None:
    import sys
    from runpod_testbed.provision.fleet import Fleet
    st = State.load(sys.argv[1])
    err = teardown(Fleet(), st)
    print(f"teardown done; errored (tolerated): {err}")


if __name__ == "__main__":
    main()
