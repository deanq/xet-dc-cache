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


def cli_undeploy(env_name: str, names: tuple[str, ...] = ENDPOINTS) -> None:
    # `flash undeploy` deletes by endpoint name (there is no --env); --all would
    # nuke unrelated endpoints in the account. Tolerate not-found per endpoint
    # (teardown-on-failure often runs before deploy created them). env_name is
    # unused by the CLI but kept for the teardown(flash_undeploy=...) contract.
    for name in names:
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


def _no_volume_delete(volume_id: str) -> None:
    raise RuntimeError(f"volume {volume_id}: deletion not wired (Task 17)")


_default_volume_delete = _no_volume_delete   # Task 17 rebinds to volumes.delete_network_volume


def teardown_all(mech, state, *, flash_undeploy=None, delete_volume=None) -> list[str]:
    """Mechanism resources -> baseline endpoint -> network volumes. Best-effort:
    every failure is returned, never raised, so one stuck resource can't stop
    the rest from being torn down (they'd keep billing).

    Defaults resolve at call time (module attributes), so tests can monkeypatch
    `down.cli_undeploy`; Task 17 points `_default_volume_delete` at the REST helper."""
    from runpod_testbed.mechanisms.base import BASELINE_LABEL, endpoint_name
    flash_undeploy = flash_undeploy or cli_undeploy
    delete_volume = delete_volume or _default_volume_delete
    errored = []
    try:
        mech.teardown(state)
    except Exception as e:
        errored.append(f"{mech.name} teardown: {e}")
    try:
        flash_undeploy(f"xet-{state.runid}", names=(endpoint_name(BASELINE_LABEL),))
    except Exception as e:
        errored.append(f"baseline undeploy: {e}")
    for label, vid in state.volumes.items():
        try:
            delete_volume(vid)
        except Exception as e:
            errored.append(f"volume {label}={vid}: {e}")
    return errored


def main() -> None:
    import sys
    from runpod_testbed.mechanisms import get_mechanism
    from runpod_testbed.mechanisms.base import ProvisionState
    st = ProvisionState.load(sys.argv[1])
    err = teardown_all(get_mechanism(st.mechanism), st)
    print(f"teardown done; errored (tolerated): {err}")


if __name__ == "__main__":
    main()
