from __future__ import annotations
from runpod_testbed.provision.up import State

# Endpoint names deployed by worker/flash_app.py, fixed by the A/B/C overlap
# contract (config.load_str enforces overlap keys == {"A","B","C"}).
ENDPOINTS = ("xet-dl-A", "xet-dl-B", "xet-dl-C")


def cli_undeploy(env_name: str, names: tuple[str, ...] = ENDPOINTS) -> None:
    """Delete the named endpoints of the run's Flash environment (runpod.apps
    registry, not local state). Missing env/endpoints are tolerated: teardown-on-
    failure often runs before deploy created them. Undeploying the whole env
    (`rp flash undeploy`) would also remove the baseline, which teardown_all
    handles separately so per-mechanism teardown stays scoped."""
    from runpod_testbed.provision.flash import undeploy_endpoints
    undeploy_endpoints(env_name, names)


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


from runpod_testbed.provision.volumes import delete_network_volume

_default_volume_delete = delete_network_volume   # best-effort REST DELETE; 404 tolerated


def _default_env_delete(env_name: str) -> None:
    from runpod_testbed.provision.flash import delete_environment
    delete_environment(env_name)


def teardown_all(mech, state, *, flash_undeploy=None, delete_volume=None,
                 delete_env=None) -> list[str]:
    """Mechanism resources -> baseline endpoint -> Flash environment, the run's
    app and its leaked endpoint templates -> network volumes. Flash never deletes the
    volumes it creates, so they go last, after every endpoint using them. Best-effort:
    every failure is returned, never raised, so one stuck resource can't stop
    the rest from being torn down (they'd keep billing).

    Defaults resolve at call time (module attributes), so tests can monkeypatch
    `down.cli_undeploy` / `down._default_volume_delete` / `down._default_env_delete`."""
    from runpod_testbed.mechanisms.base import BASELINE_LABEL, endpoint_name
    flash_undeploy = flash_undeploy or cli_undeploy
    delete_volume = delete_volume or _default_volume_delete
    delete_env = delete_env or _default_env_delete
    errored = []
    try:
        mech.teardown(state)
    except Exception as e:
        errored.append(f"{mech.name} teardown: {e}")
    try:
        flash_undeploy(f"xet-{state.runid}", names=(endpoint_name(BASELINE_LABEL),))
    except Exception as e:
        errored.append(f"baseline undeploy: {e}")
    try:
        delete_env(f"xet-{state.runid}")
    except Exception as e:
        errored.append(f"flash env xet-{state.runid}: {e}")
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
