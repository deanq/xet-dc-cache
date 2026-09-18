"""Cache-pod entrypoint: self-discover own+peer external addrs, exec xetcache.

Sibling discovery uses name-prefix listing (Fleet.list_pods_by_prefix), not a
PEER_POD_IDS env var -- this removes the pod-creation ordering dependency (a
pod doesn't need its siblings' ids handed to it at creation time; it can find
them by convention once all pods in the fleet exist).
"""
from __future__ import annotations

import os
import time

from runpod_testbed.provision.fleet import Fleet, parse_external_addr


class NotReady(Exception):
    pass


def assemble_env(own_id, peer_ids, addrs: dict, token: str, extra: dict) -> dict:
    ids = [own_id, *peer_ids]
    for i in ids:
        if not addrs.get(i):
            raise NotReady(f"no external addr yet for {i}")
    env = dict(extra)
    env["PUBLIC_BASE"] = addrs[own_id]
    env["SELF_URL"] = addrs[own_id]
    env["PEERS"] = ",".join(addrs[i] for i in peer_ids)
    if token:
        env["SHIM_AUTH_TOKEN"] = token
    return env


def resolve(fleet: Fleet, own_id, peer_ids, timeout_s: int, token, extra) -> dict:
    deadline = time.time() + timeout_s
    while True:
        addrs = {}
        for pid in [own_id, *peer_ids]:
            addrs[pid] = parse_external_addr(fleet.get_pod_ports(pid))
        try:
            return assemble_env(own_id, peer_ids, addrs, token, extra)
        except NotReady:
            if time.time() > deadline:
                raise
            time.sleep(3)


def discover_ids(list_result: list[dict], own_id: str) -> list[str]:
    """Pure: given Fleet.list_pods_by_prefix's result, return sibling ids."""
    return [pod["id"] for pod in list_result if pod["id"] != own_id]


def main() -> None:
    own = os.environ["RUNPOD_POD_ID"]
    prefix = os.environ["FLEET_PREFIX"]
    token = os.environ.get("SHIM_AUTH_TOKEN", "")
    extra = {
        k: os.environ[k]
        for k in ("XORB_CACHE_MAX_GIB", "CACHE_DIR", "PORT")
        if k in os.environ
    }

    fleet = Fleet()

    expected = int(os.environ.get("FLEET_SIZE", "0")) or None
    deadline = time.time() + 300
    while True:
        pods = fleet.list_pods_by_prefix(prefix)
        if any(p["id"] == own_id for p in pods) and (
            expected is None or len(pods) >= expected
        ):
            break
        if time.time() > deadline:
            raise NotReady(f"fleet prefix {prefix!r} never reached expected size")
        time.sleep(3)

    peers = discover_ids(pods, own)
    env = resolve(fleet, own, peers, timeout_s=300, token=token, extra=extra)
    os.environ.update(env)
    print(f"[selfconfig] PUBLIC_BASE={env['PUBLIC_BASE']} PEERS={env['PEERS']}", flush=True)
    os.execvp("xetcache", ["xetcache"])


if __name__ == "__main__":
    main()
