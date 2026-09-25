"""The Go xet-cache shim as a Mechanism: 3 peered cache pods + Flash endpoints
with HF_ENDPOINT=<pod addr>. Behavior-preserving port of provision/up.py's
main() and provision/down.py's teardown; has_metrics() gates harvest/scrape."""
from __future__ import annotations
import os
import time
import urllib.request

from runpod_testbed.mechanisms.base import (
    BASELINE_LABEL, ProvisionState, WorkerSpec, state_path,
)
from runpod_testbed.provision.down import ENDPOINTS, cli_undeploy, teardown
from runpod_testbed.provision.flash import deploy_env, flash_deploy, manifest_endpoint_ids
from runpod_testbed.provision.up import State, flash_deploy_env

POD_WAIT_S = 300
POLL_S = 3
HEALTHZ_TIMEOUT_S = 5

_NO_METRICS = ("_No pod metrics captured for this run "
               "(`make scrape` was not running); section omitted._")


def per_pod_stats(metrics_rows: list) -> dict:
    from runpod_testbed.harvest.report import _final_by_pod
    out = {}
    for pod in sorted({r["pod"] for r in metrics_rows}):
        pod_rows = [r for r in metrics_rows if r["pod"] == pod]
        out[pod] = {"effective_hit_rate": _final_by_pod(pod_rows, "xet_effective_hit_rate"),
                    "wan_bytes_saved": _final_by_pod(pod_rows, "xet_wan_bytes_saved")}
    return out


def _peering_lines(payoff: dict, format_bytes, format_percent) -> list[str]:
    """Plain-English peering paragraph. Zero peer+WAN bytes means peering was
    never exercised this run (not a meaningless "0%")."""
    if not payoff["peer_bytes"] and not payoff["wan_bytes"]:
        return ["_Peering not exercised this run._", ""]
    lines = [f"Of all cache-miss bytes, **{format_percent(payoff['peer_fraction'])} "
             f"({format_bytes(payoff['peer_bytes'])}) were served peer-to-peer** over the backbone; "
             f"only **{format_percent(1 - payoff['peer_fraction'])} "
             f"({format_bytes(payoff['wan_bytes'])})** fell back to the internet."]
    if payoff["hedge_win_ratio"]:
        lines[0] += (f" When a peer lagged, the hedge raced the CDN and the peer still won "
                     f"**{format_percent(payoff['hedge_win_ratio'])}** of the time.")
    lines += ["", "_(Higher peer % = less internet egress = the point of the system.)_", ""]
    return lines


def _wait_addr(fleet, pid: str, timeout_s: int) -> str:
    from runpod_testbed.provision.fleet import parse_external_addr
    end = time.time() + timeout_s
    while time.time() < end:
        a = parse_external_addr(fleet.get_pod_ports(pid))
        if a:
            return a
        time.sleep(POLL_S)
    raise TimeoutError(
        f"pod {pid} never exposed a public tcp addr (usual cause: the image "
        f"failed to pull or the shim never started — check the pod's status/logs "
        f"in the Runpod console and confirm cache_image is pushed and reachable)")


def _wait_healthz(addr: str, timeout_s: int) -> None:
    end = time.time() + timeout_s
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{addr}/healthz", timeout=HEALTHZ_TIMEOUT_S) as r:
                if r.status == 200:
                    return
        except Exception:
            pass
        time.sleep(POLL_S)
    raise TimeoutError(f"{addr}/healthz never green")


def _pod_env(runid: str, fleet_size: int, environ) -> dict:
    # env carries everything selfconfig needs to discover siblings by name
    # prefix and query its own addr.
    return {"XORB_CACHE_MAX_GIB": "0",
            "SHIM_AUTH_TOKEN": environ.get("SHIM_AUTH_TOKEN", ""),
            "RUNPOD_API_KEY": environ["RUNPOD_API_KEY"],
            "FLEET_PREFIX": f"xet-cache-{runid}-",
            "FLEET_SIZE": str(fleet_size)}


class ShimMechanism:
    name = "shim"

    def has_metrics(self) -> bool:
        return True

    def worker_spec(self, cfg) -> WorkerSpec:
        return WorkerSpec(handler="shim")   # POD_ADDR_* is only known after pods exist

    def jobs(self, cfg) -> list[dict]:
        from runpod_testbed.drive.run import expand_jobs
        return [{**j, "mechanism": self.name, "endpoint": j["group"]}
                for j in expand_jobs(cfg.overlap, cfg.burst)]

    def provision(self, cfg, runid: str, *, fleet=None, deploy=flash_deploy,
                  manifest_ids=manifest_endpoint_ids, wait_healthz=_wait_healthz,
                  environ=os.environ) -> ProvisionState:
        if fleet is None:
            from runpod_testbed.provision.fleet import Fleet
            fleet = Fleet()
        groups = list(cfg.overlap.keys())  # A,B,C (config enforces)
        if len(groups) > cfg.max_pods:
            raise ValueError("overlap groups exceed max_pods")
        state = ProvisionState(mechanism=self.name, runid=runid)
        os.makedirs("data", exist_ok=True)
        pod_env = _pod_env(runid, len(groups), environ)
        for g in groups:  # 1. create cache pods; save after each so teardown sees partial fleets
            state.pods[g] = fleet.create_cache_pod(
                name=f"xet-cache-{runid}-{g}", image=cfg.cache_image,
                instance_id=cfg.pod_instance_id, disk_gb=cfg.container_disk_gb,
                dc=cfg.dc, env=pod_env)
            state.save(state_path(runid))
        for g, pid in state.pods.items():  # 2. wait for external addrs + /healthz
            addr = _wait_addr(fleet, pid, POD_WAIT_S)
            wait_healthz(addr, POD_WAIT_S)
            state.metrics_urls[g] = addr
        # 3. deploy the Flash endpoints (A/B/C through their pods + the baseline)
        spec = WorkerSpec(handler="shim", env=flash_deploy_env(state.metrics_urls, cfg, environ["HF_TOKEN"]))
        deploy(deploy_env(spec, cfg, environ["HF_TOKEN"], runid))
        state.endpoints = manifest_ids()
        state.save(state_path(runid))
        with open("data/last-runid", "w") as fh:  # demo targets read this instead of a RUNID arg
            fh.write(runid)
        return state

    def teardown(self, state: ProvisionState, *, fleet=None, flash_undeploy=cli_undeploy) -> None:
        if fleet is None:
            from runpod_testbed.provision.fleet import Fleet
            fleet = Fleet()
        legacy = State(state.runid, list(state.pods.values()), f"xet-{state.runid}")
        errored = teardown(fleet, legacy, flash_undeploy=lambda env: flash_undeploy(env, names=ENDPOINTS))
        print(f"shim teardown done; errored (tolerated): {errored}")

    def report_sections(self, jobs: list, metrics_rows: list, state) -> list[str]:
        from runpod_testbed.harvest.report import format_bytes, format_percent, peering_payoff, pod_label
        if not metrics_rows:
            return ["## Per-pod cache effectiveness", "", _NO_METRICS, "",
                    "## Where the bytes came from (peering)", "", _NO_METRICS, ""]
        lines = ["## Per-pod cache effectiveness", "",
                 "| pod | hit rate | internet traffic avoided |", "|---|---|---|"]
        for pod, d in per_pod_stats(metrics_rows).items():
            lines.append(f"| {pod_label(pod, state)} | {format_percent(d['effective_hit_rate'])} | "
                         f"{format_bytes(d['wan_bytes_saved'])} |")
        lines += ["", "## Where the bytes came from (peering)", ""]
        lines += _peering_lines(peering_payoff(metrics_rows), format_bytes, format_percent)
        return lines
