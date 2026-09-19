from __future__ import annotations
import subprocess
from runpod_testbed.provision.up import State


def cli_undeploy(env_name: str) -> None:
    subprocess.run(["flash", "undeploy", "--all", "--env", env_name], check=True)


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
