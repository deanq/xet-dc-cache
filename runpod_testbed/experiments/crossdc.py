"""Cross-DC peering experiment: how does datacenter distance change the stats?

Provisions TWO peered cache pods, one in each of the two datacenters given on
the command line, warms the second pod with a model, then drives a COLD pull
through the first pod. On that miss the first pod peer-fetches from the second,
so the first pod's metrics show the peer-vs-CDN split the adaptive hedge chose
for that inter-pod distance. Run it once same-DC and once cross-DC and compare.

    python -m runpod_testbed.experiments.crossdc EU-RO-1 EU-RO-1     # same-DC baseline
    python -m runpod_testbed.experiments.crossdc EU-RO-1 US-KS-2     # transatlantic

Model defaults to gpt2's model.safetensors (~548 MB, Xet-backed); override with
CROSSDC_REPO / CROSSDC_PATH. Needs RUNPOD_API_KEY / HF_TOKEN / SHIM_AUTH_TOKEN
in the environment (the Makefile sources runpod_testbed/.env).

CAVEAT: Runpod pods peer over their PUBLIC addresses, so "cross-DC" here is the
public internet between regions, not a private backbone — the pessimistic case.
The point is the DIRECTION: as distance grows, the hedge shifts peer_bytes ->
wan_bytes. Always tears the pods down, even on failure.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from runpod_testbed.provision.fleet import Fleet, parse_external_addr

REPO = os.environ.get("CROSSDC_REPO", "openai-community/gpt2")
FILE = os.environ.get("CROSSDC_PATH", "model.safetensors")
REV = os.environ.get("CROSSDC_REV", "main")
INSTANCE = os.environ.get("CROSSDC_INSTANCE", "cpu3c-2-4")
IMAGE = os.environ.get("CROSSDC_IMAGE", "docker.io/deanq/xet-cache-testbed:latest")


def _worker(endpoint: str, out: str) -> None:
    """Subprocess body: one hf_hub_download through `endpoint`, print sha256."""
    with tempfile.TemporaryDirectory() as hf_home:
        os.environ["HF_HOME"] = hf_home
        os.environ["HF_ENDPOINT"] = endpoint
        from huggingface_hub import hf_hub_download

        path = hf_hub_download(repo_id=REPO, filename=FILE, revision=REV)
        shutil.copy(path, out)
        print(hashlib.sha256(Path(path).read_bytes()).hexdigest())


def download(endpoint: str, out: Path) -> tuple[str, float]:
    """Run a download in a clean subprocess; return (sha256, wall_seconds)."""
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "runpod_testbed.experiments.crossdc",
         "--worker", endpoint, str(out)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"download via {endpoint} failed:\n{proc.stderr}")
    return proc.stdout.strip().splitlines()[-1], time.monotonic() - t0


def metrics(addr: str) -> dict:
    import json
    with urllib.request.urlopen(f"{addr}/metrics", timeout=15) as r:
        return json.load(r)


def _wait_ready(fleet: Fleet, pid: str, timeout_s: int = 360) -> str:
    end = time.time() + timeout_s
    while time.time() < end:
        addr = parse_external_addr(fleet.get_pod_ports(pid))
        if addr:
            try:
                with urllib.request.urlopen(f"{addr}/healthz", timeout=5) as r:
                    if r.status == 200:
                        return addr
            except Exception:
                pass
        time.sleep(4)
    raise TimeoutError(f"pod {pid} never became healthy")


def _delta(before: dict, after: dict, key: str) -> int:
    return int(after.get(key, 0)) - int(before.get(key, 0))


def run_pair(dc_a: str, dc_b: str) -> dict:
    """Provision A@dc_a + B@dc_b (peered), warm B, measure a cold pull through A."""
    fleet = Fleet()
    runid = time.strftime("%H%M%S")
    prefix = f"crossdc-{runid}-"
    pod_env = {
        "XORB_CACHE_MAX_GIB": "0",
        "SHIM_AUTH_TOKEN": os.environ.get("SHIM_AUTH_TOKEN", ""),
        "RUNPOD_API_KEY": os.environ["RUNPOD_API_KEY"],
        "FLEET_PREFIX": prefix,
        "FLEET_SIZE": "2",
    }
    # Optional peer tuning passthrough, so a run can widen the discovery/fetch
    # timeouts to show cross-DC peering is a tunable threshold, not a hard limit.
    for knob in ("PEER_PROBE_TIMEOUT_MS", "PEER_FETCH_TIMEOUT_MS", "PEER_HEDGE_MAX_MS"):
        if os.environ.get(knob):
            pod_env[knob] = os.environ[knob]
    tmp = Path(tempfile.mkdtemp(prefix="crossdc-"))
    pods: list[str] = []
    try:
        print(f"provisioning A@{dc_a} + B@{dc_b} ...", flush=True)
        a = fleet.create_cache_pod(name=f"{prefix}a", image=IMAGE,
                                   instance_id=INSTANCE, disk_gb=20, dc=dc_a, env=pod_env)
        pods.append(a)
        b = fleet.create_cache_pod(name=f"{prefix}b", image=IMAGE,
                                   instance_id=INSTANCE, disk_gb=20, dc=dc_b, env=pod_env)
        pods.append(b)
        addr_a = _wait_ready(fleet, a)
        addr_b = _wait_ready(fleet, b)
        print(f"  A {addr_a}  |  B {addr_b}", flush=True)

        # Warm B (WAN fill), then a cold pull through A -> A peer-fetches from B.
        print("warming B (WAN) ...", flush=True)
        _sha_b, _ = download(addr_b, tmp / "warm.bin")
        print("measuring cold pull through A (peer-fetches from B) ...", flush=True)
        before = metrics(addr_a)
        sha_a, dt = download(addr_a, tmp / "measure.bin")
        after = metrics(addr_a)

        peer = _delta(before, after, "peer_bytes")
        wan = _delta(before, after, "wan_bytes")
        served = _delta(before, after, "served_bytes")
        fired = _delta(before, after, "peer_hedge_fired_total")
        peer_won = _delta(before, after, "peer_hedge_peer_won_total")
        probe_to = _delta(before, after, "peer_probe_timeouts_total")
        peer_miss = _delta(before, after, "peer_misses")
        total = peer + wan
        return {
            "pair": f"{dc_a} -> {dc_b}", "bytes_ok": sha_a == _sha_b,
            "peer_bytes": peer, "wan_bytes": wan, "served_bytes": served,
            "peer_fraction": (peer / total) if total else 0.0,
            "hedge_fired": fired, "hedge_peer_won": peer_won,
            "peer_probe_timeouts": probe_to, "peer_misses": peer_miss,
            "seconds": round(dt, 1),
            "mb_s": round(served / max(dt, 1e-6) / 1e6) if served else 0,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        for pid in pods:
            try:
                fleet.terminate_pod(pid)
            except Exception as e:
                print(f"WARN: failed to terminate {pid}: {e}", flush=True)
        print(f"torn down {len(pods)} pods", flush=True)


def _fmt(r: dict) -> str:
    return (f"  {r['pair']:<22} peer={r['peer_bytes']:>12,}  wan={r['wan_bytes']:>12,}"
            f"  peer_frac={r['peer_fraction']:.2f}  probe_timeouts={r['peer_probe_timeouts']}"
            f"  hedge_won={r['hedge_peer_won']}/{r['hedge_fired']}"
            f"  {r['seconds']}s ({r['mb_s']} MB/s)  {'ok' if r['bytes_ok'] else 'BYTES DIFFER'}")


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "--worker":
        _worker(sys.argv[2], sys.argv[3])
        return
    if len(sys.argv) != 3:
        print("usage: python -m runpod_testbed.experiments.crossdc <dcA> <dcB>",
              file=sys.stderr)
        sys.exit(2)
    r = run_pair(sys.argv[1], sys.argv[2])
    print(f"\n== cross-DC result ({REPO}::{FILE}) ==")
    print(_fmt(r))


if __name__ == "__main__":
    main()
