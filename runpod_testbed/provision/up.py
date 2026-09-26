from __future__ import annotations
import json
import os
import sys
import time
from dataclasses import asdict, dataclass


def flash_deploy_env(addrs: dict, cfg, hf_token: str) -> dict:
    env = {f"POD_ADDR_{g}": a for g, a in addrs.items()}
    env["HF_TOKEN"] = hf_token
    env["WORKER_CPU"] = cfg.worker_cpu
    env["WORKER_GPU"] = cfg.worker_gpu
    env["WORKER_DEPS"] = ",".join(cfg.worker_deps)
    env["WORKER_MAX"] = str(cfg.burst)
    return env


@dataclass
class State:
    runid: str
    pods: list
    flash_env: str

    def save(self, path: str) -> None:
        with open(path, "w") as fh:
            json.dump(asdict(self), fh, indent=2)

    @classmethod
    def load(cls, path: str) -> "State":
        with open(path) as fh:
            return cls(**json.load(fh))


def main(argv: list | None = None, *, environ=os.environ, get_mech=None,
         load_config=None, now=time.strftime) -> None:
    from runpod_testbed.mechanisms import get_mechanism
    from runpod_testbed.mechanisms.base import ProvisionState, state_path
    from runpod_testbed.provision.down import teardown_all
    from runpod_testbed.config import load
    argv = sys.argv[1:] if argv is None else argv
    get_mech = get_mech or get_mechanism
    load_config = load_config or load

    cfg = load_config(argv[0] if argv else "config.toml", mechanism_override=environ.get("MECHANISM"))
    mech = get_mech(cfg.mechanism)
    runid = now("%Y%m%d-%H%M%S")
    os.makedirs("data", exist_ok=True)
    try:
        state = mech.provision(cfg, runid)
    except Exception:
        print("provision failed; tearing down")
        if os.path.exists(state_path(runid)):
            print(teardown_all(mech, ProvisionState.load(state_path(runid))))
        raise
    print(f"UP runid={runid} mechanism={mech.name} endpoints={state.endpoints} "
          f"pods={state.pods} volumes={state.volumes}")


if __name__ == "__main__":
    main()
