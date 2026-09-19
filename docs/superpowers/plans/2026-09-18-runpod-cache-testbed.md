# Runpod Cache Testbed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a repeatable Runpod testbed that deploys three peered xet-dc-cache pods + three serverless endpoints, drives a multi-model download workload, and harvests both pod-side and endpoint-side data into a report.

**Architecture:** New self-contained `runpod-testbed/` subsystem. Cache pods run the existing shim binary wrapped in a small self-configuring image (the shim is untouched), provisioned via the `runpod-python` SDK + raw GraphQL. The serverless side is **Runpod Flash** (`runpod-flash`): three `@Endpoint`-decorated download-timer functions on **CPU workers**, deployed with `flash deploy` and torn down with `flash undeploy`. Metrics are pulled to parquet and joined into a Markdown report + plots.

**Tech Stack:** Python 3.10+ (PEP 723 `uv` scripts, matching `tools/`/`study/`), `runpod` SDK (cache pods), `runpod-flash` (serverless), `huggingface_hub`, `pyarrow`/`pandas`, `matplotlib`, stdlib `urllib`/`tomllib`. Go shim reused as-is via `make build-linux`.

## Refinements since the spec (docs/superpowers/specs/2026-09-18-runpod-cache-testbed-design.md)

- **Serverless = Runpod Flash, not raw `create_endpoint`.** Flash (GA Sep 2026, MIT) makes CPU workers a native `cpu=` arg (the raw SDK only exposed GPU classes — the original blocker), needs **no worker Dockerfile** (deps become `dependencies=[...]`), and gives clean per-env teardown (`flash undeploy --all`). This removes the CPU-serverless uncertainty entirely.
- **Everything pins to `EU-RO-1`.** Flash CPU serverless is EU-RO-1-only today. Because workers reach cache pods over the chosen **public TCP** path (not private networking), co-location is only about latency realism — so cache pods pin to EU-RO-1 too and worker↔pod hops stay same-DC. A different region would force GPU workers (documented escape hatch, not the default).
- **Cache pods stay on the raw SDK.** Flash is for serverless functions, not long-running cache services; `create_pod` + the self-config entrypoint are unchanged.
- **Self-config runtime:** the cache pod needs an HTTP client at boot to self-discover addresses, so the testbed cache image is `FROM python:3.12-slim` (not distroless) + the linux `xetcache` binary + a stdlib-only `selfconfig.py` entrypoint.

## Verified Runpod API facts (from 2026 docs; the plan's code uses these)

- **Create CPU pod (SDK):** `runpod.create_pod(name=, image_name=, instance_id="cpu3c-2-4", container_disk_in_gb=, ports="8000/tcp", env={...}, ...)`. Cheapest flavor `cpu3c-1-2`. Runpod injects `RUNPOD_POD_ID` into every pod. **Confirm in Task 1** the create_pod kwarg for datacenter pinning (`data_center_id=` / `country_code=`) so cache pods land in EU-RO-1.
- **Read external TCP addr (GraphQL):** `query { pod(input:{podId:"ID"}){ runtime { ports { ip isIpPublic privatePort publicPort type } } } }`. `runtime` is `null` until running — poll. For TCP, `publicPort` ≠ `privatePort`.
- **Terminate pod (SDK):** `runpod.terminate_pod(pod_id)`.
- **Flash serverless (SDK + CLI):** `pip install runpod-flash`. Decorate: `@Endpoint(name=, cpu="cpu5c-4-8", datacenter=DataCenter.EU_RO_1, env={...}, workers=(0,N), idle_timeout=, dependencies=[...])` over `async def fn(payload: dict) -> dict`. Deploy: `flash deploy --env <env>`. Teardown: `flash undeploy --all` / `flash env delete <env>`. Flash provisions one real Serverless Endpoint per decorated function and writes `.flash/flash_manifest.json` mapping function→resource. Auth via `RUNPOD_API_KEY`.
- **Submit job (SDK, against Flash-provisioned endpoints):** `ep = runpod.Endpoint("ENDPOINT_ID"); job = ep.run({"input": {...}}); job.status(); job.output()` (polls). REST equivalent: `POST https://api.runpod.ai/v2/{id}/run` with `Authorization: Bearer <key>`, body `{"input": {...}}`; poll `GET /status/{job_id}`. **Confirm in Task 3/5** how to read the endpoint IDs out of `.flash/flash_manifest.json`.

## Global Constraints

- **Never modify `shim-go/`.** The cache binary is used as-is; the testbed only adds `runpod-testbed/`, a `.gitignore` entry, and (if needed) a `make build-linux`-consuming Dockerfile.
- **CPU-only everywhere**, pinned to **EU-RO-1** (cache pods via `create_pod`, Flash workers via `cpu=`/`datacenter=`). `up.py` enforces `max_pods`/`max_burst` budget ceilings from config and refuses to exceed them.
- **`PUBLIC_BASE` = the pod's discovered external `ip:publicPort`** (never the internal 8000, never localhost). Peer URLs likewise.
- **Fail-secure teardown:** every created resource is recorded to `data/state-<runid>.json` before the next is created; `down.py` is idempotent and tolerates already-gone resources; `up.py` tears down on error by default.
- **No secrets in the repo.** `RUNPOD_API_KEY` and the throwaway `HF_TOKEN` come from env or a git-ignored `config.toml`; `config.example.toml` carries only placeholders. `runpod-testbed/data/` is git-ignored.
- **Python style:** PEP 723 `uv` script headers (see `tools/*.py`), stdlib over deps where reasonable, pure functions separated from I/O so the core logic is unit-testable without touching Runpod.
- **Every module has a pure, unit-tested core.** Runpod/network calls live behind a thin seam a test can substitute.

## File Structure

- Create: `runpod-testbed/config.example.toml`
- Create: `runpod-testbed/config.py` — loader + validation + budget ceilings
- Create: `runpod-testbed/provision/fleet.py` — SDK + GraphQL client; pure port-parse helper
- Create: `runpod-testbed/provision/selfconfig.py` — cache-pod self-config entrypoint (pure `assemble_config` + poll loop)
- Create: `runpod-testbed/provision/cache.Dockerfile` — python-slim + `xetcache` + selfconfig
- Create: `runpod-testbed/provision/up.py` — provision cache pods, then `flash deploy` endpoints, write state
- Create: `runpod-testbed/provision/down.py` — idempotent teardown (pods via SDK, endpoints via `flash undeploy`)
- Create: `runpod-testbed/worker/flash_app.py` — Flash `@Endpoint` download-timer functions (pure `time_one`/`handle` core)
- Create: `runpod-testbed/drive/run.py` — job-matrix driver (+ `--dry-run`); reads endpoint IDs from `.flash/flash_manifest.json`
- Create: `runpod-testbed/harvest/scrape.py` — prometheus-text poller → parquet (pure parser)
- Create: `runpod-testbed/harvest/report.py` — join + aggregate + plots + md (pure aggregators)
- Create: `runpod-testbed/README.md` — end-to-end run guide, cost/token warnings
- Modify: `.gitignore` — add `runpod-testbed/data/` and `runpod-testbed/config.toml`
- Test: `runpod-testbed/tests/` — one `test_*.py` per module core

Each `uv` script is runnable standalone; tests use `pytest` via `uv run --with pytest`.

---

### Task 1: Config loader + fleet client foundation

**Files:**
- Create: `runpod-testbed/config.example.toml`
- Create: `runpod-testbed/config.py`
- Create: `runpod-testbed/provision/fleet.py`
- Test: `runpod-testbed/tests/test_config.py`, `runpod-testbed/tests/test_fleet.py`

**Interfaces:**
- Produces: `config.load(path) -> Config` (dataclass with `dc, registry, cache_image, models: list[str], overlap: dict[str,list[str]], burst: int, max_pods: int, max_burst: int, pod_instance_id: str, worker_cpu: str, worker_deps: list[str], container_disk_gb: int, scrape_interval_s: int`), raising `ValueError` on over-budget or missing fields. `dc` defaults to `"EU-RO-1"`.
- Produces: `fleet.parse_external_addr(ports: list[dict]) -> str | None` (returns `http://{ip}:{publicPort}` for the tcp/public entry with `privatePort==8000`, else `None`).
- Produces: `fleet.Fleet` class wrapping the SDK (pods only): `create_cache_pod(...)`, `get_pod_ports(pod_id)`, `terminate_pod(id)`. Endpoints are Flash's job (Tasks 3/4), not `Fleet`'s.

**Before writing code:** confirm the `runpod.create_pod` kwarg that pins a pod to a datacenter (`data_center_id=` vs `country_code=`) against the installed SDK version, and that a CPU pod can land in EU-RO-1. Record the finding as a comment at the top of `fleet.py`.

- [ ] **Step 1: Write the failing test for config budget validation**

```python
# runpod-testbed/tests/test_config.py
import pytest
from runpod_testbed.config import load_str

VALID = """
dc = "EU-RO-1"
registry = "docker.io/me"
cache_image = "me/xet-cache-testbed:latest"
worker_cpu = "cpu5c-4-8"
worker_deps = ["huggingface_hub", "hf_xet"]
models = ["org/x@main", "org/y@main", "org/z@main"]
burst = 4
max_pods = 3
max_burst = 8
pod_instance_id = "cpu3c-2-4"
container_disk_gb = 60
scrape_interval_s = 5
[overlap]
A = ["org/x@main", "org/y@main"]
B = ["org/y@main", "org/z@main"]
C = ["org/z@main", "org/x@main"]
"""

def test_load_ok():
    cfg = load_str(VALID)
    assert cfg.burst == 4
    assert cfg.overlap["A"] == ["org/x@main", "org/y@main"]

def test_rejects_burst_over_ceiling():
    bad = VALID.replace("burst = 4", "burst = 99")
    with pytest.raises(ValueError, match="burst"):
        load_str(bad)

def test_rejects_overlap_model_not_in_models():
    bad = VALID.replace('C = ["org/z@main", "org/x@main"]', 'C = ["org/z@main", "org/UNKNOWN@main"]')
    with pytest.raises(ValueError, match="UNKNOWN"):
        load_str(bad)
```

- [ ] **Step 2: Run it, verify it fails** — `cd runpod-testbed && uv run --with pytest pytest tests/test_config.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement `config.py`**

```python
# runpod-testbed/config.py
from __future__ import annotations
import tomllib
from dataclasses import dataclass

@dataclass(frozen=True)
class Config:
    dc: str
    registry: str
    cache_image: str
    worker_cpu: str
    worker_deps: list[str]
    models: list[str]
    overlap: dict[str, list[str]]
    burst: int
    max_pods: int
    max_burst: int
    pod_instance_id: str
    container_disk_gb: int
    scrape_interval_s: int

_REQUIRED = ("dc", "registry", "cache_image", "worker_cpu", "worker_deps",
             "models", "overlap", "burst", "max_pods", "max_burst",
             "pod_instance_id", "container_disk_gb", "scrape_interval_s")

def load_str(text: str) -> Config:
    raw = tomllib.loads(text)
    missing = [k for k in _REQUIRED if k not in raw]
    if missing:
        raise ValueError(f"config missing keys: {missing}")
    cfg = Config(**{k: raw[k] for k in _REQUIRED})
    if cfg.burst > cfg.max_burst:
        raise ValueError(f"burst {cfg.burst} exceeds max_burst {cfg.max_burst}")
    if len(cfg.overlap) > cfg.max_pods:
        raise ValueError(f"overlap groups {len(cfg.overlap)} exceed max_pods {cfg.max_pods}")
    known = set(cfg.models)
    for grp, ms in cfg.overlap.items():
        for m in ms:
            if m not in known:
                raise ValueError(f"overlap[{grp}] references unknown model {m}")
    return cfg

def load(path: str) -> Config:
    with open(path, "rb") as fh:
        return load_str(fh.read().decode())
```

- [ ] **Step 4: Run config tests, verify pass.**

- [ ] **Step 5: Write the failing test for `parse_external_addr`**

```python
# runpod-testbed/tests/test_fleet.py
from runpod_testbed.provision.fleet import parse_external_addr

def test_parse_picks_public_tcp_8000():
    ports = [
        {"ip": "1.2.3.4", "isIpPublic": True, "privatePort": 8000, "publicPort": 41234, "type": "tcp"},
        {"ip": "10.0.0.1", "isIpPublic": False, "privatePort": 8888, "publicPort": 8888, "type": "http"},
    ]
    assert parse_external_addr(ports) == "http://1.2.3.4:41234"

def test_parse_none_when_not_ready():
    assert parse_external_addr([]) is None
    assert parse_external_addr([{"ip": "1.2.3.4", "isIpPublic": False,
                                 "privatePort": 8000, "publicPort": 41234, "type": "tcp"}]) is None
```

- [ ] **Step 6: Run it, verify it fails.**

- [ ] **Step 7: Implement `fleet.py`** — the pure helper first, then the SDK wrapper (thin; no unit test for the network methods, they're integration-verified in Task 8's run).

```python
# runpod-testbed/provision/fleet.py
# Datacenter-pin finding (verified <DATE>): create_pod pins via <data_center_id|country_code>=... — fill in Task 1.
from __future__ import annotations
import json, os, urllib.request

def parse_external_addr(ports: list[dict]) -> str | None:
    for p in ports or []:
        if p.get("type") == "tcp" and p.get("isIpPublic") and p.get("privatePort") == 8000:
            return f"http://{p['ip']}:{p['publicPort']}"
    return None

_GQL = "https://api.runpod.io/graphql"

def _graphql(query: str, api_key: str) -> dict:
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(f"{_GQL}?api_key={api_key}", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

class Fleet:
    def __init__(self, api_key: str | None = None):
        import runpod
        self.api_key = api_key or os.environ["RUNPOD_API_KEY"]
        runpod.api_key = self.api_key
        self._runpod = runpod

    def get_pod_ports(self, pod_id: str) -> list[dict]:
        q = ('query { pod(input:{podId:"%s"}) { runtime { ports '
             '{ ip isIpPublic privatePort publicPort type } } } }' % pod_id)
        data = _graphql(q, self.api_key)
        rt = (data.get("data", {}).get("pod") or {}).get("runtime")
        return (rt or {}).get("ports") or []

    def create_cache_pod(self, name, image, instance_id, disk_gb, dc, env: dict) -> str:
        # dc kwarg name (data_center_id / country_code) confirmed in Task 1.
        pod = self._runpod.create_pod(
            name=name, image_name=image, instance_id=instance_id,
            container_disk_in_gb=disk_gb, ports="8000/tcp", env=env,
            data_center_id=dc)
        return pod["id"]

    def terminate_pod(self, pod_id: str) -> None:
        self._runpod.terminate_pod(pod_id)
```

- [ ] **Step 8: Write `config.example.toml`** with the schema above and placeholder values (`registry = "docker.io/CHANGEME"`, real-looking model ids as comments).

- [ ] **Step 9: Run all Task-1 tests, verify pass.**

- [ ] **Step 10: Commit** — `git add runpod-testbed/config* runpod-testbed/provision/fleet.py runpod-testbed/tests && git commit -m "feat(testbed): config loader + Runpod fleet client"`

---

### Task 2: Cache-pod self-config + image

**Files:**
- Create: `runpod-testbed/provision/selfconfig.py`
- Create: `runpod-testbed/provision/cache.Dockerfile`
- Test: `runpod-testbed/tests/test_selfconfig.py`

**Interfaces:**
- Consumes: `fleet.parse_external_addr` (Task 1).
- Produces: `selfconfig.assemble_env(own_id, peer_ids, addrs: dict[str,str], token, extra: dict) -> dict[str,str]` — returns the shim env (`PUBLIC_BASE`, `PEERS`, `SELF_URL`, `SHIM_AUTH_TOKEN`, plus `extra`) or raises `NotReady` if any id lacks an address.
- Produces: `selfconfig.resolve(fleet, own_id, peer_ids, timeout_s) -> dict[str,str]` (poll loop).

**Interface note (`entrypoint`):** the Docker `ENTRYPOINT` runs `python selfconfig.py` which resolves addresses, then `os.execvp("xetcache", ...)` with the assembled env. `RUNPOD_POD_ID` gives own id; `PEER_POD_IDS` (comma-sep, set by `up.py`) gives siblings.

- [ ] **Step 1: Write the failing test**

```python
# runpod-testbed/tests/test_selfconfig.py
import pytest
from runpod_testbed.provision.selfconfig import assemble_env, NotReady

def test_assemble_builds_public_base_and_peers():
    addrs = {"self": "http://1.1.1.1:41001",
             "b": "http://2.2.2.2:41002",
             "c": "http://3.3.3.3:41003"}
    env = assemble_env("self", ["b", "c"], addrs, token="secret",
                       extra={"XORB_CACHE_MAX_GIB": "50"})
    assert env["PUBLIC_BASE"] == "http://1.1.1.1:41001"
    assert env["SELF_URL"] == "http://1.1.1.1:41001"
    assert set(env["PEERS"].split(",")) == {"http://2.2.2.2:41002", "http://3.3.3.3:41003"}
    assert env["SHIM_AUTH_TOKEN"] == "secret"
    assert env["XORB_CACHE_MAX_GIB"] == "50"

def test_assemble_raises_when_peer_missing():
    with pytest.raises(NotReady):
        assemble_env("self", ["b"], {"self": "http://1.1.1.1:41001"}, token="s", extra={})
```

- [ ] **Step 2: Run it, verify it fails.**

- [ ] **Step 3: Implement `selfconfig.py`**

```python
# runpod-testbed/provision/selfconfig.py
"""Cache-pod entrypoint: self-discover own+peer external addrs, exec xetcache."""
from __future__ import annotations
import os, sys, time
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

def main() -> None:
    own = os.environ["RUNPOD_POD_ID"]
    peers = [p for p in os.environ.get("PEER_POD_IDS", "").split(",") if p]
    extra = {k: os.environ[k] for k in
             ("XORB_CACHE_MAX_GIB", "CACHE_DIR", "PORT") if k in os.environ}
    env = resolve(Fleet(), own, peers, timeout_s=300,
                  token=os.environ.get("SHIM_AUTH_TOKEN", ""), extra=extra)
    os.environ.update(env)
    print(f"[selfconfig] PUBLIC_BASE={env['PUBLIC_BASE']} PEERS={env['PEERS']}", flush=True)
    os.execvp("xetcache", ["xetcache"])

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Write `cache.Dockerfile`** (built from repo root; consumes `make build-linux` output)

```dockerfile
# runpod-testbed/provision/cache.Dockerfile
# Build the binary first: make build-linux  (-> shim-go/xetcache-linux-amd64)
# Then: docker build -f runpod-testbed/provision/cache.Dockerfile -t <img> .
FROM python:3.12-slim
COPY shim-go/xetcache-linux-amd64 /usr/local/bin/xetcache
COPY runpod-testbed/ /app/runpod-testbed/
ENV PYTHONPATH=/app CACHE_DIR=/cache PORT=8000
RUN pip install --no-cache-dir runpod
VOLUME ["/cache"]
EXPOSE 8000
ENTRYPOINT ["python", "-m", "runpod_testbed.provision.selfconfig"]
```

**Note:** ensure `runpod-testbed/` is importable as `runpod_testbed` — add empty `__init__.py` files (`runpod-testbed/__init__.py`, `provision/__init__.py`, etc.) and a symlink-free package name (rename dir import via `PYTHONPATH` + a top-level `runpod_testbed/` package, or set `pyproject`/`conftest` rootdir). Confirm the import path works in Step 6.

- [ ] **Step 6: Verify import + tests** — `cd runpod-testbed && uv run --with pytest pytest tests/test_selfconfig.py -v` → PASS.

- [ ] **Step 7: Commit** — `feat(testbed): self-configuring cache-pod entrypoint + image`

---

### Task 3: Flash worker app (download-timer endpoints)

**Files:**
- Create: `runpod-testbed/worker/timing.py` — pure, testable timing core
- Create: `runpod-testbed/worker/flash_app.py` — Flash `@Endpoint` functions (one per group)
- Test: `runpod-testbed/tests/test_timing.py`

**Interfaces:**
- Produces: `timing.time_one(download_fn, model: str) -> dict` — calls `download_fn(model) -> (bytes:int, first_byte_s:float)`, returns `{"model", "bytes", "wall_seconds", "first_byte_ms", "ok", "error"}`.
- Produces: `timing.run_download(payload: dict, download_fn) -> dict` — iterates `payload["models"]`, returns `{"worker_id", "results": [...]}`.
- Produces: `timing.hf_download(model: str) -> tuple[int, float]` — the real `snapshot_download` through the cache.

**Design note (env chicken-and-egg):** each `@Endpoint`'s `HF_ENDPOINT` = its cache pod's external addr, known only after pods are up. So `flash_app.py` reads the three addresses from process env at deploy time (`POD_ADDR_A/B/C`, exported by `up.py` before it shells `flash deploy`) — the decorator's `env={...}` captures them into the deployed endpoint.

- [ ] **Step 1: Write the failing test** (the pure core only — Flash decorators need the Runpod backend, so they are not unit-tested)

```python
# runpod-testbed/tests/test_timing.py
from runpod_testbed.worker.timing import time_one, run_download

def test_time_one_records_bytes_and_timing():
    out = time_one(lambda m: (1_000_000, 0.5), "org/x@main")
    assert out["ok"] is True
    assert out["bytes"] == 1_000_000
    assert out["first_byte_ms"] == 500
    assert out["wall_seconds"] >= 0

def test_time_one_captures_error():
    def boom(m):
        raise RuntimeError("dns fail")
    out = time_one(boom, "org/x@main")
    assert out["ok"] is False
    assert "dns fail" in out["error"]

def test_run_download_iterates_models():
    out = run_download({"models": ["a", "b"]}, lambda m: (10, 0.01))
    assert [r["model"] for r in out["results"]] == ["a", "b"]
```

- [ ] **Step 2: Run it, verify it fails.**

- [ ] **Step 3: Implement `timing.py`**

```python
# runpod-testbed/worker/timing.py
from __future__ import annotations
import os, time
from pathlib import Path

def time_one(download_fn, model: str) -> dict:
    start = time.monotonic()
    try:
        nbytes, first_byte_s = download_fn(model)
        return {"model": model, "bytes": int(nbytes),
                "first_byte_ms": round(first_byte_s * 1000),
                "wall_seconds": round(time.monotonic() - start, 3),
                "ok": True, "error": None}
    except Exception as e:  # errors are values — report, don't crash the worker
        return {"model": model, "bytes": 0, "first_byte_ms": None,
                "wall_seconds": round(time.monotonic() - start, 3),
                "ok": False, "error": str(e)}

def run_download(payload: dict, download_fn) -> dict:
    return {"worker_id": os.environ.get("RUNPOD_POD_ID", "unknown"),
            "results": [time_one(download_fn, m) for m in payload.get("models", [])]}

def hf_download(model: str):
    """Real download through the cache (HF_ENDPOINT set in the Flash endpoint env)."""
    from huggingface_hub import snapshot_download
    repo, _, rev = model.partition("@")
    t0 = time.monotonic()
    path = snapshot_download(repo_id=repo, revision=rev or None)
    first_byte_s = time.monotonic() - t0  # coarse: first file resolved
    total = sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())
    return (total, first_byte_s)
```

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Write `flash_app.py`** — three endpoints, one per group, each pinned to its pod's addr. Verify the exact `runpod_flash` import + `@Endpoint` signature against the installed `runpod-flash` (see the Flash facts above) before trusting this.

```python
# runpod-testbed/worker/flash_app.py
import os
from runpod_flash import Endpoint, DataCenter
from runpod_testbed.worker.timing import run_download, hf_download

_DEPS = os.environ.get("WORKER_DEPS", "huggingface_hub,hf_xet").split(",")
_CPU = os.environ.get("WORKER_CPU", "cpu5c-4-8")
_MAX = int(os.environ.get("WORKER_MAX", "4"))
_HF_TOKEN = os.environ.get("HF_TOKEN", "")

def _mk(name: str, pod_addr: str):
    @Endpoint(name=name, cpu=_CPU, datacenter=DataCenter.EU_RO_1,
              workers=(0, _MAX), idle_timeout=5, dependencies=_DEPS,
              env={"HF_ENDPOINT": pod_addr, "HF_TOKEN": _HF_TOKEN})
    async def _fn(payload: dict) -> dict:
        return run_download(payload, hf_download)
    return _fn

# up.py exports POD_ADDR_A/B/C before `flash deploy`; groups map A/B/C -> pods.
download_A = _mk("xet-dl-A", os.environ["POD_ADDR_A"])
download_B = _mk("xet-dl-B", os.environ["POD_ADDR_B"])
download_C = _mk("xet-dl-C", os.environ["POD_ADDR_C"])
```

- [ ] **Step 6: Verify Flash app imports** — `cd runpod-testbed && POD_ADDR_A=x POD_ADDR_B=y POD_ADDR_C=z uv run --with runpod-flash python -c "import runpod_testbed.worker.flash_app"` succeeds (no deploy). If the `@Endpoint` signature differs from the researched shape, correct it here and note the delta.

- [ ] **Step 7: Commit** — `feat(testbed): Flash download-timer endpoints + timing core`

---

### Task 4: Provisioning up/down (pods via SDK, endpoints via Flash)

**Files:**
- Create: `runpod-testbed/provision/up.py`
- Create: `runpod-testbed/provision/down.py`
- Test: `runpod-testbed/tests/test_provision.py`

**Interfaces:**
- Consumes: `Config` (Task 1), `Fleet` (Task 1), the Flash CLI (`flash deploy` / `flash undeploy`).
- Produces: `up.flash_deploy_env(addrs: dict[str,str], cfg, hf_token) -> dict` — the process env for the `flash deploy` subprocess: `POD_ADDR_A/B/C`, `HF_TOKEN`, `WORKER_CPU`, `WORKER_DEPS`, `WORKER_MAX`.
- Produces: `up.State` (dataclass: `runid, pods: list[str], flash_env: str`) with `save(path)`/`load(path)`.
- Produces: `down.teardown(fleet, state, flash_undeploy) -> list[str]` — runs `flash_undeploy(state.flash_env)` then terminates pods; returns ids/labels that errored (tolerated). `flash_undeploy` is a callable seam (shells `flash undeploy --all --env <env>`) so the pure logic is testable.

- [ ] **Step 1: Write the failing test**

```python
# runpod-testbed/tests/test_provision.py
from runpod_testbed.provision.up import flash_deploy_env, State
from runpod_testbed.provision.down import teardown

def _cfg():
    from runpod_testbed.config import Config
    return Config(dc="EU-RO-1", registry="r", cache_image="c",
                  worker_cpu="cpu5c-4-8", worker_deps=["huggingface_hub"],
                  models=["x"], overlap={"A": ["x"]}, burst=3, max_pods=3,
                  max_burst=8, pod_instance_id="cpu3c-2-4",
                  container_disk_gb=60, scrape_interval_s=5)

def test_flash_deploy_env_maps_addrs_and_worker_knobs():
    env = flash_deploy_env({"A": "http://1:41", "B": "http://2:42", "C": "http://3:43"},
                           _cfg(), "hf_throwaway")
    assert env["POD_ADDR_A"] == "http://1:41"
    assert env["POD_ADDR_C"] == "http://3:43"
    assert env["HF_TOKEN"] == "hf_throwaway"
    assert env["WORKER_CPU"] == "cpu5c-4-8"
    assert env["WORKER_DEPS"] == "huggingface_hub"
    assert env["WORKER_MAX"] == "3"

def test_state_roundtrip(tmp_path):
    p = tmp_path / "state.json"
    State("run1", ["pA", "pB"], "run1-env").save(str(p))
    got = State.load(str(p))
    assert got.pods == ["pA", "pB"] and got.flash_env == "run1-env"

class _FakeFleet:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)
    def terminate_pod(self, i):
        self.calls.append(("pod", i))
        if i in self.fail: raise RuntimeError("already gone")

def test_teardown_undeploys_then_terminates_and_tolerates_errors():
    f = _FakeFleet(fail={"pB"})
    seen = []
    errored = teardown(f, State("r", ["pA", "pB"], "r-env"),
                       flash_undeploy=lambda env: seen.append(env))
    assert seen == ["r-env"]                        # endpoints (Flash) torn down first
    assert f.calls == [("pod", "pA"), ("pod", "pB")]
    assert errored == ["pB"]

def test_teardown_tolerates_flash_undeploy_error():
    f = _FakeFleet()
    def boom(env): raise RuntimeError("flash cli missing")
    errored = teardown(f, State("r", ["pA"], "r-env"), flash_undeploy=boom)
    assert "r-env" in errored and f.calls == [("pod", "pA")]  # pods still torn down
```

- [ ] **Step 2: Run it, verify it fails.**

- [ ] **Step 3: Implement `up.py` (`flash_deploy_env`, `State`) + `down.py` (teardown core)**

```python
# runpod-testbed/provision/up.py
from __future__ import annotations
import json
from dataclasses import dataclass, asdict

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
        with open(path, "w") as fh: json.dump(asdict(self), fh, indent=2)
    @classmethod
    def load(cls, path: str) -> "State":
        with open(path) as fh: return cls(**json.load(fh))
```

```python
# runpod-testbed/provision/down.py
from __future__ import annotations
import subprocess
from runpod_testbed.provision.up import State

def cli_undeploy(env_name: str) -> None:
    subprocess.run(["flash", "undeploy", "--all", "--env", env_name], check=True)

def teardown(fleet, state: State, flash_undeploy=cli_undeploy) -> list[str]:
    errored = []
    try:
        flash_undeploy(state.flash_env)          # endpoints first (Flash)
    except Exception:
        errored.append(state.flash_env)
    for pid in state.pods:                        # then cache pods (SDK)
        try: fleet.terminate_pod(pid)
        except Exception: errored.append(pid)
    return errored

def main() -> None:
    import sys
    from runpod_testbed.provision.fleet import Fleet
    st = State.load(sys.argv[1])
    err = teardown(Fleet(), st)
    print(f"teardown done; errored (tolerated): {err}")

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Implement `up.main()`** — the real flow (integration; not unit-tested, exercised in Task 8). State is saved before each risky step; on any error it tears down.

```python
# append to runpod-testbed/provision/up.py
import os, sys, time, subprocess, urllib.request

def main() -> None:
    from runpod_testbed.config import load
    from runpod_testbed.provision.fleet import Fleet, parse_external_addr
    cfg = load(sys.argv[1] if len(sys.argv) > 1 else "config.toml")
    hf_token = os.environ["HF_TOKEN"]; auth = os.environ.get("SHIM_AUTH_TOKEN", "")
    runid = time.strftime("%Y%m%d-%H%M%S"); flash_env = f"xet-{runid}"
    state = State(runid, [], flash_env)
    statef = f"data/state-{runid}.json"; os.makedirs("data", exist_ok=True)
    fleet = Fleet()
    try:
        groups = list(cfg.overlap.keys())               # e.g. A,B,C
        if len(groups) > cfg.max_pods:
            raise ValueError("overlap groups exceed max_pods")
        # 1. create cache pods, pinned to EU-RO-1 (IDs exist now; addrs do not)
        gid = {}
        for g in groups:
            pid = fleet.create_cache_pod(
                name=f"xet-cache-{runid}-{g}", image=cfg.cache_image,
                instance_id=cfg.pod_instance_id, disk_gb=cfg.container_disk_gb,
                dc=cfg.dc, env={"XORB_CACHE_MAX_GIB": "0", "SHIM_AUTH_TOKEN": auth})
            state.pods.append(pid); gid[g] = pid; state.save(statef)
        # 2. selfconfig discovers siblings by name prefix xet-cache-{runid}-
        #    (see Task-2/Task-1 note (b); no PEER_POD_IDS ordering dependency)
        # 3. wait for external addrs + /healthz; map group -> external addr
        addrs = {}
        for g, pid in gid.items():
            addr = _wait_addr(fleet, pid, 300); _wait_healthz(addr, 300)
            addrs[g] = addr
        # 4. deploy the 3 Flash endpoints (env carries POD_ADDR_A/B/C + worker knobs)
        deploy_env = {**os.environ, **flash_deploy_env(addrs, cfg, hf_token),
                      "FLASH_ENV": flash_env}
        subprocess.run(["flash", "deploy", "--env", flash_env],
                       cwd="worker", env=deploy_env, check=True)
        state.save(statef)
        print(f"UP runid={runid} pods={state.pods} flash_env={flash_env} addrs={addrs}")
    except Exception:
        from runpod_testbed.provision.down import teardown
        print("provision failed; tearing down"); teardown(fleet, state)
        raise

def _wait_addr(fleet, pid, timeout_s):
    from runpod_testbed.provision.fleet import parse_external_addr
    end = time.time() + timeout_s
    while time.time() < end:
        a = parse_external_addr(fleet.get_pod_ports(pid))
        if a: return a
        time.sleep(3)
    raise TimeoutError(f"pod {pid} never exposed a public tcp addr")

def _wait_healthz(addr, timeout_s):
    end = time.time() + timeout_s
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{addr}/healthz", timeout=5) as r:
                if r.status == 200: return
        except Exception: pass
        time.sleep(3)
    raise TimeoutError(f"{addr}/healthz never green")
```

**Sibling-discovery note (resolve in Task 2/1):** the `flash deploy` cwd + how `flash_app.py` finds `runpod_testbed.worker.timing` must be confirmed (Flash artifact packaging). And `selfconfig` finds its peers by the `xet-cache-{runid}-` name prefix via a pods-list GraphQL query (Task-1 mechanism (b)) — no `PEER_POD_IDS` ordering dependency. If that query is unavailable, fall back to passing `PEER_POD_IDS` at create + an edit-pod mutation, and document which was used.

- [ ] **Step 6: Run all Task-4 tests, verify pass.**

- [ ] **Step 7: Commit** — `feat(testbed): pod provisioning + Flash deploy/undeploy with fail-secure teardown`

---

### Task 5: Workload driver

**Files:**
- Create: `runpod-testbed/drive/run.py`
- Test: `runpod-testbed/tests/test_drive.py`

**Interfaces:**
- Consumes: `Config` (Task 1), the Flash manifest `worker/.flash/flash_manifest.json`, the `runpod` SDK `Endpoint` client.
- Produces: `run.expand_jobs(overlap: dict, burst: int) -> list[dict]` — COLD phase (one job per (group, model)) then WARM-BURST phase (`burst` jobs per (group, model)); each job `{"group", "model", "phase", "replica"}`.
- Produces: `run.endpoint_ids(manifest: dict) -> dict[str,str]` — maps group (`A`/`B`/`C`, from the `xet-dl-<G>` function name) → Runpod endpoint id, read from the Flash manifest.
- Produces: `run.record(job: dict, result: dict, submit_ts: float, path: str)` — append one JSON line with submit/return timestamps.

- [ ] **Step 1: Write the failing test**

```python
# runpod-testbed/tests/test_drive.py
from runpod_testbed.drive.run import expand_jobs, endpoint_ids

def test_expand_cold_then_warm():
    overlap = {"A": ["x", "y"], "B": ["y", "z"]}
    jobs = expand_jobs(overlap, burst=3)
    cold = [j for j in jobs if j["phase"] == "cold"]
    warm = [j for j in jobs if j["phase"] == "warm"]
    assert len(cold) == 4                      # 2 groups * 2 models
    assert len(warm) == 4 * 3                   # each (group,model) * burst
    # all cold jobs precede all warm jobs
    assert jobs.index(cold[-1]) < jobs.index(warm[0])
    assert {j["group"] for j in jobs} == {"A", "B"}

def test_endpoint_ids_maps_group_to_id():
    # shape per Task-3/5 verification of flash_manifest.json
    manifest = {"endpoints": [
        {"function": "xet-dl-A", "endpoint_id": "ep-a"},
        {"function": "xet-dl-B", "endpoint_id": "ep-b"},
    ]}
    assert endpoint_ids(manifest) == {"A": "ep-a", "B": "ep-b"}
```

- [ ] **Step 2: Run it, verify it fails.**

- [ ] **Step 3: Implement `run.py`** (confirm the actual `flash_manifest.json` key names in Task 3/5 and adjust `endpoint_ids` to match)

```python
# runpod-testbed/drive/run.py
from __future__ import annotations
import json, time

def expand_jobs(overlap: dict, burst: int) -> list:
    cold, warm = [], []
    for g, models in overlap.items():
        for m in models:
            cold.append({"group": g, "model": m, "phase": "cold", "replica": 0})
            for r in range(burst):
                warm.append({"group": g, "model": m, "phase": "warm", "replica": r})
    return cold + warm

def endpoint_ids(manifest: dict) -> dict:
    out = {}
    for e in manifest.get("endpoints", []):
        fn = e["function"]                      # "xet-dl-A"
        out[fn.rsplit("-", 1)[-1]] = e["endpoint_id"]
    return out

def record(job: dict, result: dict, submit_ts: float, path: str) -> None:
    row = {**job, "submit_ts": submit_ts, "return_ts": time.time(), "result": result}
    with open(path, "a") as fh:
        fh.write(json.dumps(row) + "\n")
```

Then `main()` (integration): load config; read `worker/.flash/flash_manifest.json` → `endpoint_ids`; for each job build `{"input": {"models": [job["model"]]}}` and submit via `runpod.Endpoint(eid).run(...)` — COLD serially (await each), then WARM-BURST concurrently (thread pool sized to `burst`); record each to `data/jobs-<runid>.jsonl`. Add `--dry-run <cache_url>`: skip Runpod entirely and run the download locally against one pod to validate wiring + the harvester before any spend:

```python
# in main(): if argv has --dry-run <url>:
#   import os; os.environ["HF_ENDPOINT"] = url
#   from runpod_testbed.worker.timing import hf_download
#   for m in cfg.models: print(m, hf_download(m))
#   return
```

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit** — `feat(testbed): workload driver + dry-run mode`

---

### Task 6: Metrics harvester

**Files:**
- Create: `runpod-testbed/harvest/scrape.py`
- Create: `runpod-testbed/tests/fixtures/metrics_sample.txt` (captured `/metrics/prometheus`)
- Test: `runpod-testbed/tests/test_scrape.py`

**Interfaces:**
- Produces: `scrape.parse_prometheus(text: str) -> list[dict]` — each `{"name", "labels": dict, "value": float}`, skipping `#` comment lines.

- [ ] **Step 1: Create the fixture** — capture real shim output shape (from `shim-go/prometheus.go`). Include at least: `xet_hits_total`, `xet_wan_bytes_total`, `xet_peer_bytes_total`, `xet_effective_hit_rate`, a labeled `xet_peer_peer_throughput_bytes_per_ms{peer="http://b:8000"}`, and a histogram line `xet_xorb_latency_ms_bucket{source="cdn",le="5"}`.

```
# runpod-testbed/tests/fixtures/metrics_sample.txt
# HELP xet_hits_total Xorb range requests served from the local cache.
# TYPE xet_hits_total counter
xet_hits_total 12
# HELP xet_wan_bytes_total Bytes fetched from upstream (WAN).
# TYPE xet_wan_bytes_total counter
xet_wan_bytes_total 1048576
# HELP xet_peer_bytes_total Bytes pulled from peers.
# TYPE xet_peer_bytes_total counter
xet_peer_bytes_total 524288
# HELP xet_effective_hit_rate share served without a CDN fetch.
# TYPE xet_effective_hit_rate gauge
xet_effective_hit_rate 0.75
# HELP xet_peer_peer_throughput_bytes_per_ms Per-peer throughput EWMA (bytes/ms).
# TYPE xet_peer_peer_throughput_bytes_per_ms gauge
xet_peer_peer_throughput_bytes_per_ms{peer="http://b:8000"} 3.5
# HELP xet_xorb_latency_ms Served-range latency by source, ms.
# TYPE xet_xorb_latency_ms histogram
xet_xorb_latency_ms_bucket{source="cdn",le="5"} 3
```

- [ ] **Step 2: Write the failing test**

```python
# runpod-testbed/tests/test_scrape.py
from pathlib import Path
from runpod_testbed.harvest.scrape import parse_prometheus

def test_parse_plain_labeled_and_histogram():
    text = Path(__file__).parent.joinpath("fixtures/metrics_sample.txt").read_text()
    rows = parse_prometheus(text)
    by = {(r["name"], tuple(sorted(r["labels"].items()))): r["value"] for r in rows}
    assert by[("xet_hits_total", ())] == 12.0
    assert by[("xet_peer_bytes_total", ())] == 524288.0
    assert by[("xet_effective_hit_rate", ())] == 0.75
    assert by[("xet_peer_peer_throughput_bytes_per_ms",
               (("peer", "http://b:8000"),))] == 3.5
    assert by[("xet_xorb_latency_ms_bucket",
               (("le", "5"), ("source", "cdn")))] == 3.0
    assert all(not r["name"].startswith("#") for r in rows)
```

- [ ] **Step 3: Implement `scrape.py`**

```python
# runpod-testbed/harvest/scrape.py
from __future__ import annotations
import re, time, urllib.request

_LINE = re.compile(r'^(?P<name>[a-zA-Z_:][\w:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<val>[^\s]+)\s*$')
_LBL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')

def parse_prometheus(text: str) -> list:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        labels = {k: v for k, v in _LBL.findall(m.group("labels") or "")}
        try:
            val = float(m.group("val"))
        except ValueError:
            continue
        rows.append({"name": m.group("name"), "labels": labels, "value": val})
    return rows

def scrape_once(pod_id: str, addr: str) -> list:
    with urllib.request.urlopen(f"{addr}/metrics/prometheus", timeout=10) as r:
        rows = parse_prometheus(r.read().decode())
    ts = time.time()
    for row in rows:
        row["pod"] = pod_id; row["ts"] = ts
    return rows

def main() -> None:  # integration: loop scrape all pods -> parquet
    import sys, pyarrow as pa, pyarrow.parquet as pq
    from runpod_testbed.provision.up import State
    from runpod_testbed.provision.fleet import Fleet, parse_external_addr
    st = State.load(sys.argv[1]); interval = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    fleet = Fleet(); addrs = {p: parse_external_addr(fleet.get_pod_ports(p)) for p in st.pods}
    out = f"data/pod-metrics-{st.runid}.parquet"; buf = []
    try:
        while True:
            for pid, a in addrs.items():
                if a: buf.extend(scrape_once(pid, a))
            time.sleep(interval)
    except KeyboardInterrupt:
        pq.write_table(pa.Table.from_pylist(buf), out)
        print(f"wrote {len(buf)} rows -> {out}")
```

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit** — `feat(testbed): prometheus metrics harvester`

---

### Task 7: Report

**Files:**
- Create: `runpod-testbed/harvest/report.py`
- Test: `runpod-testbed/tests/test_report.py`

**Interfaces:**
- Consumes: jobs `.jsonl` (Task 5), pod-metrics parquet rows (Task 6).
- Produces: `report.latency_by_phase(jobs: list[dict]) -> dict[str, dict]` — per phase (`cold`/`warm`): `{"n", "median_s", "p95_s", "total_bytes"}` over `ok` jobs.
- Produces: `report.peering_payoff(metric_rows: list[dict]) -> dict` — fleet totals `{"peer_bytes", "wan_bytes", "peer_fraction", "hedge_win_ratio"}` from the final (max-ts) sample per pod.

- [ ] **Step 1: Write the failing test**

```python
# runpod-testbed/tests/test_report.py
from runpod_testbed.harvest.report import latency_by_phase, peering_payoff

JOBS = [
    {"phase": "cold", "result": {"results": [{"ok": True, "wall_seconds": 10.0, "bytes": 100}]}},
    {"phase": "warm", "result": {"results": [{"ok": True, "wall_seconds": 1.0, "bytes": 100}]}},
    {"phase": "warm", "result": {"results": [{"ok": True, "wall_seconds": 2.0, "bytes": 100}]}},
    {"phase": "warm", "result": {"results": [{"ok": False, "wall_seconds": 0.0, "bytes": 0}]}},
]

def test_latency_by_phase_ignores_failures():
    out = latency_by_phase(JOBS)
    assert out["cold"]["n"] == 1 and out["cold"]["median_s"] == 10.0
    assert out["warm"]["n"] == 2 and out["warm"]["median_s"] == 1.5

def test_peering_payoff_uses_final_sample():
    rows = [
        {"pod": "A", "ts": 1, "name": "xet_peer_bytes_total", "labels": {}, "value": 5},
        {"pod": "A", "ts": 2, "name": "xet_peer_bytes_total", "labels": {}, "value": 30},
        {"pod": "A", "ts": 2, "name": "xet_wan_bytes_total", "labels": {}, "value": 70},
    ]
    out = peering_payoff(rows)
    assert out["peer_bytes"] == 30 and out["wan_bytes"] == 70
    assert abs(out["peer_fraction"] - 0.3) < 1e-9
```

- [ ] **Step 2: Run it, verify it fails.**

- [ ] **Step 3: Implement `report.py`** (pure aggregators + a `main()` that loads both files, computes, writes `data/report-<runid>.md` and matplotlib plots)

```python
# runpod-testbed/harvest/report.py
from __future__ import annotations
import statistics as st

def _ok_results(jobs):
    for j in jobs:
        for r in j.get("result", {}).get("results", []):
            if r.get("ok"):
                yield j["phase"], r

def latency_by_phase(jobs: list) -> dict:
    buckets: dict = {}
    for phase, r in _ok_results(jobs):
        buckets.setdefault(phase, []).append(r)
    out = {}
    for phase, rs in buckets.items():
        secs = sorted(x["wall_seconds"] for x in rs)
        out[phase] = {"n": len(secs), "median_s": st.median(secs),
                      "p95_s": secs[max(0, round(0.95 * len(secs)) - 1)],
                      "total_bytes": sum(x["bytes"] for x in rs)}
    return out

def _final_by_pod(rows, name):
    latest = {}
    for r in rows:
        if r["name"] != name or r["labels"]:
            continue
        cur = latest.get(r["pod"])
        if cur is None or r["ts"] > cur[0]:
            latest[r["pod"]] = (r["ts"], r["value"])
    return sum(v for _, v in latest.values())

def peering_payoff(metric_rows: list) -> dict:
    peer = _final_by_pod(metric_rows, "xet_peer_bytes_total")
    wan = _final_by_pod(metric_rows, "xet_wan_bytes_total")
    fired = _final_by_pod(metric_rows, "xet_peer_hedge_fired_total")
    won = _final_by_pod(metric_rows, "xet_peer_hedge_peer_won_total")
    total = peer + wan
    return {"peer_bytes": peer, "wan_bytes": wan,
            "peer_fraction": (peer / total) if total else 0.0,
            "hedge_win_ratio": (won / fired) if fired else 0.0}
```

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit** — `feat(testbed): report aggregation + plots`

---

### Task 8: README, packaging, end-to-end validation

**Files:**
- Create: `runpod-testbed/README.md`
- Create: package `__init__.py` files + `runpod-testbed/tests/conftest.py` (import rootdir)
- Modify: `.gitignore`
- Modify: `Makefile` (add `build-linux` if absent — confirm; the spec references it)

- [ ] **Step 1: Confirm `make build-linux` exists** (`grep build-linux Makefile`). If missing, add: `build-linux:` → `cd shim-go && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -o xetcache-linux-amd64 .`

- [ ] **Step 2: Add `__init__.py`** so `runpod_testbed` imports as a package; add `conftest.py` setting rootdir/`sys.path` so `uv run --with pytest pytest` resolves `runpod_testbed.*`. Verify: `cd runpod-testbed && uv run --with pytest pytest -v` → all Tasks 1-7 tests PASS together.

- [ ] **Step 3: Update `.gitignore`** — add `runpod-testbed/data/`, `runpod-testbed/config.toml`, and `runpod-testbed/worker/.flash/`.

- [ ] **Step 4: Write `README.md`** — the full run recipe: (1) `make build-linux`; (2) `docker build -f runpod-testbed/provision/cache.Dockerfile` the cache image, push to `registry` (the worker side needs **no** image — Flash packages it); (3) `pip install runpod-flash` + `flash login` (or `RUNPOD_API_KEY`); (4) copy `config.example.toml`→`config.toml`, fill in; (5) `export RUNPOD_API_KEY HF_TOKEN SHIM_AUTH_TOKEN`; (6) `uv run provision/up.py config.toml` (creates 3 EU-RO-1 CPU pods, then `flash deploy`s 3 CPU endpoints); (7) `uv run drive/run.py --dry-run <cache_url>` to validate wiring; (8) `uv run harvest/scrape.py data/state-<runid>.json &`; (9) `uv run drive/run.py config.toml`; (10) `uv run harvest/report.py data/state-<runid>.json`; (11) `uv run provision/down.py data/state-<runid>.json` (`flash undeploy` + terminate pods). Include the **cost warning** (per-run $ estimate; teardown is mandatory) and the **plaintext-token exposure** warning (public TCP forwards HF tokens — use a throwaway/scoped token; the shim is a trusted-LAN component being deliberately exposed for staging). Note the **EU-RO-1 pin** (CPU serverless is EU-RO-1-only; a different region would force GPU workers) and the **Flash 500 MB artifact limit** (fine for the download-only handler).

- [ ] **Step 5: End-to-end validation (integration, real Runpod)** — run `up.py` → `--dry-run` → `down.py` against a real account with `max_pods=3`, `burst=2`, tiny models. Confirm: 3 EU-RO-1 pods reach `/healthz`, `flash deploy` provisions 3 CPU endpoints, dry-run downloads succeed through a pod, `state.json` written, `down.py` removes everything (`flash undeploy` + pods) and is safe to re-run. Record the outcome (and the resolved datacenter-pin kwarg from Task 1, the `flash_manifest.json` shape from Task 3/5, and the sibling-discovery mechanism from Task 2) in the README's "validated run" note.

- [ ] **Step 6: Commit** — `docs(testbed): run guide, packaging, .gitignore`

---

## Self-Review

- **Spec coverage:** networking (public TCP, external addr → `PUBLIC_BASE`) ✅ Tasks 2/4; 3 peered pods ✅ Tasks 2/4; multi-model overlap ✅ Task 5; 3 endpoints ✅ Tasks 3/4; download-timer handler (Flash) ✅ Task 3; scripts orchestration ✅ Tasks 1/4; pull-to-parquet + report ✅ Tasks 6/7; fail-secure teardown + budget ✅ Tasks 1/4; token safety + cost warnings ✅ Task 8; dry-run + unit tests ✅ Tasks 5/6/7/8.
- **Placeholder scan:** the residual verifications are all explicitly scoped to concrete tasks with fallbacks — datacenter-pin kwarg (Task 1), Flash `@Endpoint` signature (Task 3), `flash_manifest.json` shape (Task 3/5), sibling discovery by name-prefix (Task 2). None are vague "TBD"s.
- **Type consistency:** `State(runid, pods, flash_env)`, `Config`, `Fleet`, `parse_external_addr`, `assemble_env`, `flash_deploy_env`, `expand_jobs`, `endpoint_ids`, `parse_prometheus`, `latency_by_phase`, `peering_payoff` names are used identically across the tasks that consume them. (Task 3 timing core is `timing.run_download`, not the old `handler.handle`.)

## Global risks the executor must not lose

1. **Every run costs money and leaves live pods + Flash endpoints.** `down.py` is mandatory (`flash undeploy` + terminate pods); `up.py` tears down on error. Never skip Task 4's teardown semantics.
2. **EU-RO-1 is a hard pin for CPU serverless.** If a run needs another region, that forces GPU workers (a config + Task-3 change), not a silent CPU deploy elsewhere.
3. **The shim is deliberately exposed on a public port with plaintext token forwarding** — throwaway HF token only; documented in Task 8.
4. **Flash specifics are verified-but-confirm:** the `@Endpoint` signature (Task 3), manifest shape (Task 3/5), and `flash deploy` cwd/packaging (Task 4). Each has a named verification step before its code is trusted.
