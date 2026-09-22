from __future__ import annotations
import json
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass


def flash_deploy_env(addrs: dict, cfg, hf_token: str) -> dict:
    env = {f"POD_ADDR_{g}": a for g, a in addrs.items()}
    env["HF_TOKEN"] = hf_token
    env["WORKER_CPU"] = cfg.worker_cpu
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


def _wait_addr(fleet, pid, timeout_s):
    from runpod_testbed.provision.fleet import parse_external_addr
    end = time.time() + timeout_s
    while time.time() < end:
        a = parse_external_addr(fleet.get_pod_ports(pid))
        if a:
            return a
        time.sleep(3)
    raise TimeoutError(
        f"pod {pid} never exposed a public tcp addr (usual cause: the image "
        f"failed to pull or the shim never started — check the pod's status/logs "
        f"in the Runpod console and confirm cache_image is pushed and reachable)")


def _wait_healthz(addr, timeout_s):
    end = time.time() + timeout_s
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{addr}/healthz", timeout=5) as r:
                if r.status == 200:
                    return
        except Exception:
            pass
        time.sleep(3)
    raise TimeoutError(f"{addr}/healthz never green")


def main() -> None:
    from runpod_testbed.config import load
    from runpod_testbed.provision.fleet import Fleet

    cfg = load(sys.argv[1] if len(sys.argv) > 1 else "config.toml")
    hf_token = os.environ["HF_TOKEN"]
    auth = os.environ.get("SHIM_AUTH_TOKEN", "")
    api_key = os.environ["RUNPOD_API_KEY"]
    runid = time.strftime("%Y%m%d-%H%M%S")
    flash_env = f"xet-{runid}"
    state = State(runid, [], flash_env)
    statef = f"data/state-{runid}.json"
    os.makedirs("data", exist_ok=True)
    fleet = Fleet()
    try:
        groups = list(cfg.overlap.keys())  # e.g. A,B,C
        if len(groups) > cfg.max_pods:
            raise ValueError("overlap groups exceed max_pods")
        # 1. create cache pods; env carries everything selfconfig needs to
        #    discover siblings by name prefix and query its own addr.
        pod_env = {
            "XORB_CACHE_MAX_GIB": "0",
            "SHIM_AUTH_TOKEN": auth,
            "RUNPOD_API_KEY": api_key,
            "FLEET_PREFIX": f"xet-cache-{runid}-",
            "FLEET_SIZE": str(len(groups)),
        }
        gid = {}
        for g in groups:
            pid = fleet.create_cache_pod(
                name=f"xet-cache-{runid}-{g}", image=cfg.cache_image,
                instance_id=cfg.pod_instance_id, disk_gb=cfg.container_disk_gb,
                dc=cfg.dc, env=pod_env)
            state.pods.append(pid)
            gid[g] = pid
            state.save(statef)
        # 2. selfconfig discovers siblings by name prefix xet-cache-{runid}-
        #    (Task-1 mechanism (b)); no PEER_POD_IDS ordering dependency.
        # 3. wait for external addrs + /healthz; map group -> external addr
        addrs = {}
        for g, pid in gid.items():
            addr = _wait_addr(fleet, pid, 300)
            _wait_healthz(addr, 300)
            addrs[g] = addr
        # 4. deploy the Flash endpoints (env carries POD_ADDR_* + worker knobs)
        deploy_env = {**os.environ, **flash_deploy_env(addrs, cfg, hf_token),
                      "FLASH_ENV": flash_env}
        subprocess.run(["flash", "deploy", "--env", flash_env],
                       cwd="runpod_testbed/worker", env=deploy_env, check=True)
        state.save(statef)
        print(f"UP runid={runid} pods={state.pods} flash_env={flash_env} addrs={addrs}")
    except Exception:
        from runpod_testbed.provision.down import teardown
        print("provision failed; tearing down")
        teardown(fleet, state)
        raise


if __name__ == "__main__":
    main()
