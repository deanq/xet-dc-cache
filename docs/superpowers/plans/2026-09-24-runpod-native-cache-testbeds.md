# Runpod-native cache testbeds (Model Store + VolumeCache) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `runpod_testbed/` with a `Mechanism` abstraction so the existing shim testbed, a new VolumeCache testbed and a new Model Store testbed all run the same CPU download-timing exercise against a shared naive-HF baseline and emit one comparable timing schema.

**Architecture:** A `Mechanism` protocol (`mechanisms/base.py`) captures only what differs per cache mechanism — provisioning, worker build, teardown, job plan, report extras. Everything else is shared: `drive/run.py` submits baseline + mechanism jobs and persists a shared timing row per job; `worker/timing.py` produces the handler-side breakdown; `harvest/report.py` renders a shared timing core and appends `mechanism.report_sections()`. The shim's current provision/worker/report code is moved behind the protocol unchanged in behavior (its existing tests must stay green, unmodified).

**Tech Stack:** Python 3.11+ (stdlib `tomllib`, `dataclasses`, `typing.Protocol`), `uv run --with ...` for deps, pytest, `runpod` (runpod-python: `runpod.Endpoint`, `runpod.serverless.VolumeCache`, `runpod.api.graphql.run_graphql_query`), `runpod-flash` (`runpod_flash.Endpoint`, `runpod_flash.core.resources.network_volume.NetworkVolume`, `flash deploy` / `flash undeploy` CLI), Runpod REST API (`https://rest.runpod.io/v1/networkvolumes`), `pyarrow` + `matplotlib` (report only).

**Spec:** `docs/superpowers/specs/2026-09-24-runpod-native-cache-testbeds-design.md`

## Global Constraints

- CPU download-timing only (no GPU load). Every Flash endpoint is a CPU endpoint (`cpu=WORKER_CPU`); no model is loaded into VRAM.
- Shared timing schema, exactly as the spec's "Shared timing schema" section — every job row carries:
  `mechanism` (`"shim" | "volumecache" | "modelstore" | "baseline"`), `phase` (`"baseline" | "populate" | "warm"`), `model` (`"org/name@rev"`), `wall_seconds` (float, driver-observed submit → ready), `bytes` (int), `breakdown` (`{"download_s": float|None, "hydrate_s": float|None, "local_read_s": float|None}`), `worker_cold` (bool), `ok` (bool).
- Headline speedup = `baseline_wall / mechanism_warm_wall` (medians over OK rows).
- All unit tests are no-spend: mock the Runpod / Flash / runpod-python SDKs (`sys.modules` fakes, injected callables). No test touches the network or the `flash` CLI.
- Teardown safety preserved: `down` is mandatory, `demo-run` tears down on `EXIT` via `trap`, and `provision.down` best-effort-removes endpoints, pods and any network volumes recorded in `ProvisionState`.
- `SHIM_AUTH_TOKEN` is shim-only. `RUNPOD_API_KEY` + `HF_TOKEN` always.
- Report output path: `data/report-<mechanism>-<runid>.md` (plots `data/report-<mechanism>-<runid>-*.png`).
- VolumeCache import is `from runpod.serverless import VolumeCache` with signature `VolumeCache(dirs, namespace=RUNPOD_ENDPOINT_ID, volume_path="/runpod-volume", best_effort=True, max_workers=...)` (verified against runpod 1.12.0: `(self, dirs, *, namespace=None, volume_path='/runpod-volume', best_effort=True, max_workers=None)`).
- Existing shim tests (`runpod_testbed/tests/test_*.py` as of commit `54944c2`) stay green **unchanged** through the whole plan. New behavior gets new tests; never edit an existing assertion.
- Project conventions: functions < 50 lines, early returns, errors-as-values in worker/driver paths (report, don't crash), no magic numbers (module constants), conventional commits `type(scope): subject`.
- All commands run from the repo root `/Users/deanq/Desktop/xet-dc-cache`. Test command: `uv run --with pytest --with pyarrow pytest runpod_testbed/tests -q` (pyarrow is needed by `test_scrape.py`; the two parquet tests fail with `ModuleNotFoundError` without it — pre-existing, not a regression).

---

## File structure

Created:

| Path | Responsibility |
|---|---|
| `runpod_testbed/mechanisms/__init__.py` | Registry: `MECHANISMS: dict[str, Mechanism]`, `get_mechanism(name)`. |
| `runpod_testbed/mechanisms/base.py` | `WorkerSpec`, `ProvisionState` (JSON save/load), `Mechanism` protocol, shared label constants. |
| `runpod_testbed/mechanisms/baseline.py` | Baseline control: `baseline_jobs(models)`, `BASELINE_WORKER_SPEC`. Not a full `Mechanism`. |
| `runpod_testbed/mechanisms/shim.py` | `ShimMechanism`: today's `up.main` body, `flash_deploy_env`, `down.teardown`, peering/hit-rate report blocks. |
| `runpod_testbed/mechanisms/volumecache.py` | `VolumeCacheMechanism` (Phase 2). |
| `runpod_testbed/mechanisms/modelstore.py` | `ModelStoreMechanism` (Phase 3). |
| `runpod_testbed/provision/flash.py` | `flash deploy` subprocess wrapper + manifest → endpoint-id map (shared by every mechanism). |
| `runpod_testbed/provision/volumes.py` | Runpod REST network-volume list/delete helpers (Phase 2). |
| `runpod_testbed/worker/plan.py` | Pure `plan_endpoints(...)`: which Flash endpoints `flash_app.py` builds for a mechanism. |
| `runpod_testbed/tests/test_mechanisms_base.py`, `test_mechanism_shim.py`, `test_baseline.py`, `test_flash.py`, `test_worker_plan.py`, `test_mechanism_volumecache.py`, `test_volumes.py`, `test_mechanism_modelstore.py` | New no-spend tests. |

Modified:

| Path | Change |
|---|---|
| `runpod_testbed/config.py` | `mechanism` key, `[shim]`/`[volumecache]`/`[modelstore]` subtables, back-compat with top-level shim keys. |
| `runpod_testbed/worker/timing.py` | Breakdown in `time_one`, `make_timing_row` (shared schema), `volumecache_download`, `modelstore_local_read`. |
| `runpod_testbed/worker/flash_app.py` | Builds endpoints from `plan_endpoints`; always includes the baseline endpoint. |
| `runpod_testbed/provision/up.py` | Generic main: `mechanism.provision`, teardown-on-failure. Keeps `State`, `flash_deploy_env`. |
| `runpod_testbed/provision/down.py` | `cli_undeploy(env, names=ENDPOINTS)`, `teardown_all(mech, state, api_key)`, generic main. Keeps `teardown`. |
| `runpod_testbed/drive/run.py` | Jobs = `baseline_jobs + mechanism.jobs`; endpoint ids from `ProvisionState`; timing row per job. |
| `runpod_testbed/harvest/report.py` | Shared timing core over the schema + `mechanism.report_sections` hook + new output path. |
| `runpod_testbed/harvest/scrape.py` | Loads `ProvisionState`; no-op exit when `has_metrics()` is False. |
| `runpod_testbed/Makefile`, `config.example.toml`, `README.md` | `MECHANISM` parametrization, subtables, docs, cost note. |

Existing tests import these names and they must keep working: `config.load_str`, `config.Config(...)` positional/keyword construction as in `tests/test_provision.py::_cfg`, `provision.up.flash_deploy_env`, `provision.up.State`, `provision.down.teardown/cli_undeploy/ENDPOINTS`, `drive.run.expand_jobs/endpoint_ids/_submit_and_wait/start_jobs_file/record`, `harvest.report.latency_by_phase/peering_payoff/headline/coldstart`, `worker.timing.time_one/run_download`.

---

## Phase 0 — Spike: provisioning automation probe (throwaway; no production code)

**This phase is investigative.** Its deliverable is a findings note appended to the spec (and mirrored into this plan's "Spike findings" block below). Its result may adjust Task 16 (VolumeCache provisioning) and Task 19 (Model Store provisioning); those tasks are written against the fallbacks the spec allows and each states exactly what to change if the spike finds an API.

What is already known from reading the installed SDKs (2026-09-24, `runpod-flash` current, `runpod` 1.12.0) — the spike verifies these live:

- `runpod_flash.Endpoint(..., volume=NetworkVolume(name=..., size=<GB 10..4096>, datacenter=DataCenter.EU_RO_1))` exists. `NetworkVolume.deploy()` is idempotent by `(name, dataCenterId)`, calls REST `POST /v1/networkvolumes`, and lists via `GET /v1/networkvolumes`. Flash's REST client has **no** delete-volume method, and `flash undeploy` has no volume handling — teardown must delete via REST directly.
- `runpod.create_endpoint(name, template_id, ..., network_volume_id=...)` (runpod-python GraphQL `saveEndpoint`) also attaches a volume, but needs a template we do not have — Flash is the path.
- Neither `runpod-flash` nor `runpod-python` contains any `cachedModel` / `modelStore` field or method. Cached-model declaration is unknown → GraphQL introspection + REST OpenAPI probe.

### Task 0.1: Probe network-volume attach via Flash (spike)

**Files:**
- Create (scratch, NOT committed): `/private/tmp/claude-501/-Users-deanq-Desktop-xet-dc-cache/ef90f04a-9299-49b5-8a12-e8287772c1f5/scratchpad/spike_volume/flash_app.py`

**Interfaces:**
- Consumes: `runpod_flash.Endpoint`, `runpod_flash.core.resources.network_volume.NetworkVolume`, `runpod_flash.DataCenter`.
- Produces: findings (a) below. **Spends money** (one CPU endpoint + a 10 GB volume for a few minutes) — operator runs it, tears down immediately.

- [ ] **Step 1: Write the spike app**

```python
# scratchpad/spike_volume/flash_app.py — throwaway
import os
from runpod_flash import Endpoint, DataCenter
from runpod_flash.core.resources.network_volume import NetworkVolume

_VOL = NetworkVolume(name="xet-spike-vol", size=10, datacenter=DataCenter.EU_RO_1)

async def spike_probe(**payload) -> dict:
    import os, time
    from runpod.serverless import VolumeCache
    t0 = time.monotonic()
    vc = VolumeCache(dirs=["/tmp/spike-cache"], best_effort=True)
    out = {
        "volume_mounted": os.path.ismount("/runpod-volume"),
        "volume_listing": sorted(os.listdir("/runpod-volume"))[:20] if os.path.isdir("/runpod-volume") else None,
        "volumecache_available": bool(vc.available),
        "endpoint_id_env": os.environ.get("RUNPOD_ENDPOINT_ID"),
    }
    os.makedirs("/tmp/spike-cache", exist_ok=True)
    open("/tmp/spike-cache/marker.txt", "w").write("hi")
    vc.hydrate()
    vc.sync(background=False)
    out["mirror_root_exists"] = os.path.isdir(f"/runpod-volume/.cache/{os.environ.get('RUNPOD_ENDPOINT_ID')}")
    out["elapsed_s"] = round(time.monotonic() - t0, 3)
    return out

spike_probe = Endpoint(name="xet-spike-vol", cpu=os.environ.get("WORKER_CPU", "cpu5c-4-8"),
                       datacenter=DataCenter.EU_RO_1, workers=(0, 1), idle_timeout=30,
                       volume=_VOL, env={})(spike_probe)
```

- [ ] **Step 2: Deploy, invoke once, capture the volume id, undeploy**

Run (from the scratch dir, with `runpod_testbed/.env` sourced):
```bash
set -a; . runpod_testbed/.env; set +a
cd /private/tmp/claude-501/-Users-deanq-Desktop-xet-dc-cache/ef90f04a-9299-49b5-8a12-e8287772c1f5/scratchpad/spike_volume
flash deploy --env spike
cat .flash/flash_manifest.json
EID=$(python -c "import json;print(json.load(open('.flash/flash_manifest.json'))['resources']['xet-spike-vol']['endpoint_id'])")
uv run --with runpod python -c "import runpod,os;runpod.api_key=os.environ['RUNPOD_API_KEY'];print(runpod.Endpoint('$EID').run({'input':{}}).output(timeout=600))"
curl -s -H "Authorization: Bearer $RUNPOD_API_KEY" https://rest.runpod.io/v1/networkvolumes | python -m json.tool
flash undeploy xet-spike-vol --force
```
Expected: handler output shows `volume_mounted: true`, `volumecache_available: true`, `mirror_root_exists: true`; the REST listing shows a volume named `xet-spike-vol` with an `id` and `dataCenterId: "EU-RO-1"`.

- [ ] **Step 3: Verify volume delete via REST and record whether `flash undeploy` left the volume**

Run:
```bash
VID=$(curl -s -H "Authorization: Bearer $RUNPOD_API_KEY" https://rest.runpod.io/v1/networkvolumes | python -c "import json,sys;print([v['id'] for v in json.load(sys.stdin) if v['name']=='xet-spike-vol'][0])")
curl -s -o /dev/null -w "%{http_code}\n" -X DELETE -H "Authorization: Bearer $RUNPOD_API_KEY" https://rest.runpod.io/v1/networkvolumes/$VID
curl -s -H "Authorization: Bearer $RUNPOD_API_KEY" https://rest.runpod.io/v1/networkvolumes
```
Expected: DELETE returns `200`/`204`; the volume is gone from the listing. If the listing shape is `{"networkVolumes": [...]}` instead of a bare list, note it (Task 14 handles both).

- [ ] **Step 4: Record finding (a)**

Append to the spec under a new `## Spike findings (Phase 0)` heading, and to this plan's "Spike findings" block:
`(a) volume attach: <automatable via Flash Endpoint(volume=NetworkVolume) | not> ; mount path <...> ; volume listing shape <list|{"networkVolumes":[]}> ; DELETE /v1/networkvolumes/{id} -> <status> ; flash undeploy removes volume: <yes|no>`.

### Task 0.2: Probe cached-model declaration (spike)

**Files:**
- Create (scratch, NOT committed): `.../scratchpad/spike_modelstore/probe.py`

**Interfaces:**
- Consumes: `runpod.api.graphql.run_graphql_query`, Runpod REST API.
- Produces: finding (b). No spend (read-only introspection) unless an endpoint create is attempted.

- [ ] **Step 1: GraphQL introspection for cached-model fields**

```python
# scratchpad/spike_modelstore/probe.py — throwaway
import json, os, urllib.request
from runpod.api.graphql import run_graphql_query

KEY = os.environ["RUNPOD_API_KEY"]

def introspect(type_name: str) -> list[str]:
    q = '{ __type(name: "%s") { inputFields { name type { name kind ofType { name } } } fields { name type { name kind ofType { name } } } } }' % type_name
    data = run_graphql_query(q, KEY)["data"]["__type"]
    if data is None:
        return []
    fields = (data.get("inputFields") or []) + (data.get("fields") or [])
    return [f["name"] for f in fields]

for t in ("EndpointInput", "Endpoint", "PodTemplateInput", "PodTemplate", "Mutation"):
    names = introspect(t)
    hits = [n for n in names if any(k in n.lower() for k in ("model", "cache", "hugging"))]
    print(t, "->", hits or "(no model/cache fields)", f"[{len(names)} fields total]")

# REST: OpenAPI surface of /v1/endpoints
req = urllib.request.Request("https://rest.runpod.io/v1/openapi.json",
                             headers={"Authorization": f"Bearer {KEY}"})
try:
    spec = json.load(urllib.request.urlopen(req, timeout=20))
    text = json.dumps(spec)
    for k in ("cachedModel", "cached_models", "modelStore", "huggingface", "models"):
        print("openapi contains", k, ":", k in text)
except Exception as e:
    print("openapi fetch failed:", e)
```

- [ ] **Step 2: Run the probe**

Run: `set -a; . runpod_testbed/.env; set +a; uv run --with runpod python /private/tmp/claude-501/-Users-deanq-Desktop-xet-dc-cache/ef90f04a-9299-49b5-8a12-e8287772c1f5/scratchpad/spike_modelstore/probe.py`
Expected: a line per type listing any `*model*`/`*cache*` field names. Either (i) a field such as `cachedModels`/`models` appears on `EndpointInput` / the REST endpoint schema → automatable, or (ii) nothing appears → console-only.

- [ ] **Step 3: If (i), confirm with a dry mutation against a throwaway endpoint; if (ii), confirm the console flow**

If (i): create a CPU endpoint via `flash deploy` (reuse Task 0.1's scratch app without `volume=`), then send the discovered mutation/PATCH with `{"model": "openai-community/gpt2"}`-shaped input and `GET` the endpoint back to confirm the field persisted; then `flash undeploy xet-spike-vol --force`.
If (ii): in the Runpod console, open Serverless → any endpoint → Edit → locate the "Cached model"/"Model" field; note the exact label, whether it accepts `org/name` + revision, and whether it requires a network volume. Also note where the platform stages the files (open a worker shell if possible: `ls /runpod-volume/huggingface-cache/hub`).

- [ ] **Step 4: Record finding (b)**

Append to the spec's `## Spike findings (Phase 0)` and this plan's block:
`(b) cached model: <automatable via GraphQL field X / REST PATCH Y | console-only> ; per-endpoint limit <1|n> ; staging path confirmed <path> ; needs network volume: <yes|no>`.

### Task 0.3: Record spike findings into the spec and this plan

**Files:**
- Modify: `docs/superpowers/specs/2026-09-24-runpod-native-cache-testbeds-design.md` (append section)
- Modify: `docs/superpowers/plans/2026-09-24-runpod-native-cache-testbeds.md` (fill the block below)

- [ ] **Step 1: Fill the findings block**

Replace the two placeholder bullets in the block below with the recorded findings (a) and (b), and state which of Task 16 Step 3 / Task 19 Step 3 conditional branches applies.

> ### Spike findings (Phase 0) — CONFIRMED live 2026-09-25
> - **(a) volume attach: AUTOMATABLE via Flash `Endpoint(volume=NetworkVolume(...))`.** Verified: a volumecache run provisioned + attached a 10 GB volume (`xet-vc-<runid>`) and tore it down cleanly. The Runpod REST API (`rest.runpod.io/v1/networkvolumes`) works for list/`DELETE` **but requires a `User-Agent` header** — without one Cloudflare returns `403` (error 1010). `DELETE /v1/networkvolumes/{id}` → 200/204; GraphQL `deleteNetworkVolume(input:{id})` also works. Listing returns a bare JSON list.
> - **(b) cached model: automatable via an UNDOCUMENTED GraphQL field.** Absent from all public surfaces (REST OpenAPI `Runpod API 0.1.0`/23 paths, live endpoint object, `runpod-python`, `runpod-flash`; introspection disabled) — but the console's JS uses a `modelReferences` field on the `saveEndpoint` mutation, verified live with a plain API key. `saveEndpoint` is an UPSERT: read the endpoint's full config via `myself { endpoints }` (no top-level `endpoint(id)` query), resend all of it + `modelReferences`, and re-carry `modelReferences` on every later save. Format `["org/name[:rev]"]` lowercased (API normalizes to a HF URL → compare loosely); the endpoint must not also have a network volume; staging surfaces as `delayTime`, not handler time. Implemented in `ModelStoreMechanism.declare_cached_models` (`provision/modelstore_api.py`), with the manual console step as a fallback and the reuse path retained. *(Reverse-engineered; unofficial — may change.)*
> - **(c) VolumeCache on the worker:** the Flash base image ships a `runpod` predating `runpod.serverless.VolumeCache`, and `WORKER_DEPS` does not override base packages. Fix shipped: `flash_app` force-upgrades `runpod>=1.12.0` at first invocation for the volumecache downloader and purges the pre-imported `runpod.serverless` from `sys.modules` before the lazy `VolumeCache` import (see `plan.upgrade_pkgs` / `flash_app._upgrade_once`).
> - **Consequence (resolved):** Task 16's default Flash `volume=` branch is correct (keep it; the REST helper now sends a User-Agent). Task 19 now **automates** the cached-model declaration via the `saveEndpoint`+`modelReferences` GraphQL path (Task 19 Step 3 branch (ii), realized in `provision/modelstore_api.py`), with the manual console step as the fallback and the `[modelstore.endpoints]` reuse path retained.

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-09-24-runpod-native-cache-testbeds-design.md docs/superpowers/plans/2026-09-24-runpod-native-cache-testbeds.md
git commit -m "docs(spike): record provisioning-automation findings"
```

---

## Phase 1 — Mechanism abstraction + shim port + baseline + shared schema

### Task 1: `mechanisms/base.py` — WorkerSpec, ProvisionState, Mechanism protocol

**Files:**
- Create: `runpod_testbed/mechanisms/__init__.py` (empty for now; registry added in Task 8)
- Create: `runpod_testbed/mechanisms/base.py`
- Test: `runpod_testbed/tests/test_mechanisms_base.py`

**Interfaces:**
- Produces:
  - `WorkerSpec(handler: str, env: dict[str,str], deps: list[str], network_volume_gb: int|None)`
  - `ProvisionState(mechanism: str, runid: str, endpoints: dict[str,str], pods: dict[str,str], volumes: dict[str,str], metrics_urls: dict[str,str])` with `save(path) -> None`, `load(path) -> ProvisionState`, `to_json() -> str`, `from_json(text) -> ProvisionState`, `state_path(runid) -> str` (module function, returns `data/state-<runid>.json`).
  - `Mechanism` protocol: `name: str`, `provision(config, runid) -> ProvisionState`, `worker_spec(config) -> WorkerSpec`, `teardown(state) -> None`, `report_sections(jobs, metrics_rows) -> list[str]`, `has_metrics() -> bool`, `jobs(config) -> list[dict]`.
  - Constants: `BASELINE_LABEL = "baseline"`, `ENDPOINT_PREFIX = "xet-dl"`, `endpoint_name(label) -> f"xet-dl-{label}"`.
  - Job dict shape (consumed by `drive/run.py`): `{"mechanism": str, "endpoint": <label>, "model": str, "phase": str, "replica": int}` (shim jobs additionally keep `"group"`).

Note on the spec: the spec's protocol has no job-plan hook, but its per-mechanism mechanics define three different driver sequences (overlap matrix / populate→warm / warm-only per model). `jobs(config)` is that hook; `runid` is added to `ProvisionState` so `scrape`/`report` can derive paths from the state object alone.

- [ ] **Step 1: Write the failing test**

```python
# runpod_testbed/tests/test_mechanisms_base.py
import json

from runpod_testbed.mechanisms.base import (
    BASELINE_LABEL, ProvisionState, WorkerSpec, endpoint_name, state_path,
)


def test_worker_spec_defaults_to_no_volume():
    spec = WorkerSpec(handler="shim")
    assert spec.env == {} and spec.deps == [] and spec.network_volume_gb is None


def test_provision_state_roundtrips_through_json(tmp_path):
    st = ProvisionState(mechanism="volumecache", runid="r1",
                        endpoints={"volumecache": "ep-1", BASELINE_LABEL: "ep-b"},
                        volumes={"vc": "vol-9"})
    p = tmp_path / "state.json"
    st.save(str(p))
    back = ProvisionState.load(str(p))
    assert back == st
    assert json.loads(p.read_text())["pods"] == {}          # defaults serialize


def test_provision_state_from_json_text():
    text = json.dumps({"mechanism": "shim", "runid": "r", "endpoints": {"A": "e"},
                       "pods": {"A": "p"}, "volumes": {}, "metrics_urls": {"A": "http://x"}})
    st = ProvisionState.from_json(text)
    assert st.pods == {"A": "p"} and st.metrics_urls["A"] == "http://x"


def test_endpoint_name_and_state_path():
    assert endpoint_name("A") == "xet-dl-A"
    assert endpoint_name(BASELINE_LABEL) == "xet-dl-baseline"
    assert state_path("20260924-101500") == "data/state-20260924-101500.json"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_mechanisms_base.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'runpod_testbed.mechanisms'`

- [ ] **Step 3: Write minimal implementation**

```python
# runpod_testbed/mechanisms/__init__.py
# (empty — the registry is added in Task 8)
```

```python
# runpod_testbed/mechanisms/base.py
"""Mechanism protocol + shared dataclasses (spec: "The Mechanism protocol")."""
from __future__ import annotations
import json
from dataclasses import asdict, dataclass, field
from typing import Protocol

BASELINE_LABEL = "baseline"
ENDPOINT_PREFIX = "xet-dl"


def endpoint_name(label: str) -> str:
    """Flash endpoint name for a driver label ("A" -> "xet-dl-A")."""
    return f"{ENDPOINT_PREFIX}-{label}"


def state_path(runid: str) -> str:
    return f"data/state-{runid}.json"


@dataclass
class WorkerSpec:
    """How to build the Flash worker for this mechanism."""
    handler: str                                   # downloader key in worker/plan.py
    env: dict[str, str] = field(default_factory=dict)   # deploy-time env for `flash deploy`
    deps: list[str] = field(default_factory=list)
    network_volume_gb: int | None = None           # None = no volume attached


@dataclass
class ProvisionState:
    """Everything teardown needs; serialized to data/state-<runid>.json."""
    mechanism: str
    runid: str
    endpoints: dict[str, str] = field(default_factory=dict)     # label -> endpoint id
    pods: dict[str, str] = field(default_factory=dict)          # label -> pod id (shim only)
    volumes: dict[str, str] = field(default_factory=dict)       # label -> network volume id
    metrics_urls: dict[str, str] = field(default_factory=dict)  # label -> scrape base URL

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "ProvisionState":
        return cls(**json.loads(text))

    def save(self, path: str) -> None:
        with open(path, "w") as fh:
            fh.write(self.to_json())

    @classmethod
    def load(cls, path: str) -> "ProvisionState":
        with open(path) as fh:
            return cls.from_json(fh.read())


class Mechanism(Protocol):
    name: str

    def provision(self, config, runid: str) -> ProvisionState: ...
    def worker_spec(self, config) -> WorkerSpec: ...
    def teardown(self, state: ProvisionState) -> None: ...
    def report_sections(self, jobs: list, metrics_rows: list) -> list[str]: ...
    def has_metrics(self) -> bool: ...
    def jobs(self, config) -> list[dict]: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_mechanisms_base.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add runpod_testbed/mechanisms/__init__.py runpod_testbed/mechanisms/base.py runpod_testbed/tests/test_mechanisms_base.py
git commit -m "feat(testbed): add Mechanism protocol + ProvisionState"
```

### Task 2: `config.py` — `mechanism` key and per-mechanism subtables (back-compat)

**Files:**
- Modify: `runpod_testbed/config.py`
- Modify: `runpod_testbed/config.example.toml`
- Test: `runpod_testbed/tests/test_config.py` (append new tests only)

**Interfaces:**
- Produces: `Config` gains `mechanism: str = "shim"`, `volume_gb: int = 0`, `modelstore_endpoints: dict[str, str] = {}`. Shim-only keys (`registry`, `cache_image`, `overlap`, `max_pods`, `pod_instance_id`) are read from `[shim]` (with `[shim.overlap]`) **or** top-level (legacy), and are only required/validated when `mechanism == "shim"`. `load_str(text, mechanism_override: str | None = None)`, `load(path, mechanism_override=None)`. `SUPPORTED_MECHANISMS = ("shim", "volumecache", "modelstore")`.
- Consumers: `provision/up.py`, `drive/run.py` (Task 9/10) pass `mechanism_override=os.environ.get("MECHANISM")` so `make up MECHANISM=...` wins over the toml key.

- [ ] **Step 1: Write the failing tests (append to `tests/test_config.py`)**

```python
VALID_VOLUMECACHE = """
mechanism = "volumecache"
dc = "EU-RO-1"
worker_cpu = "cpu5c-4-8"
worker_deps = ["huggingface_hub", "hf_xet"]
models = ["org/x@main", "org/y@main"]
burst = 4
max_burst = 8
container_disk_gb = 20
scrape_interval_s = 5
[volumecache]
volume_gb = 50
"""

VALID_SHIM_SUBTABLE = """
mechanism = "shim"
dc = "EU-RO-1"
worker_cpu = "cpu5c-4-8"
worker_deps = ["huggingface_hub"]
models = ["org/x@main", "org/y@main", "org/z@main"]
burst = 2
max_burst = 8
container_disk_gb = 20
scrape_interval_s = 5
[shim]
registry = "docker.io/me"
cache_image = "me/xet-cache-testbed:latest"
max_pods = 3
pod_instance_id = "cpu3c-2-4"
[shim.overlap]
A = ["org/x@main", "org/y@main"]
B = ["org/y@main", "org/z@main"]
C = ["org/z@main", "org/x@main"]
"""


def test_legacy_toplevel_config_defaults_to_shim():
    cfg = load_str(VALID)
    assert cfg.mechanism == "shim"
    assert cfg.volume_gb == 0 and cfg.modelstore_endpoints == {}


def test_shim_keys_read_from_shim_subtable():
    cfg = load_str(VALID_SHIM_SUBTABLE)
    assert cfg.registry == "docker.io/me" and cfg.max_pods == 3
    assert cfg.overlap["B"] == ["org/y@main", "org/z@main"]


def test_volumecache_config_needs_no_shim_keys():
    cfg = load_str(VALID_VOLUMECACHE)
    assert cfg.mechanism == "volumecache"
    assert cfg.volume_gb == 50
    assert cfg.registry == "" and cfg.overlap == {}


def test_volumecache_requires_volume_gb():
    bad = VALID_VOLUMECACHE.replace("volume_gb = 50", "")
    with pytest.raises(ValueError, match="volume_gb"):
        load_str(bad)


def test_modelstore_config_reads_optional_endpoint_map():
    text = VALID_VOLUMECACHE.replace('mechanism = "volumecache"', 'mechanism = "modelstore"') \
        .replace("[volumecache]\nvolume_gb = 50", '[modelstore]\n[modelstore.endpoints]\n"org/x@main" = "ep-x"')
    cfg = load_str(text)
    assert cfg.modelstore_endpoints == {"org/x@main": "ep-x"}


def test_shim_mechanism_still_requires_shim_keys():
    bad = VALID_VOLUMECACHE.replace('mechanism = "volumecache"', 'mechanism = "shim"')
    with pytest.raises(ValueError, match="registry"):
        load_str(bad)


def test_unknown_mechanism_rejected():
    with pytest.raises(ValueError, match="mechanism"):
        load_str(VALID_VOLUMECACHE.replace('"volumecache"', '"turbo"'))


def test_mechanism_override_wins_over_toml():
    cfg = load_str(VALID_SHIM_SUBTABLE.replace("[shim]", "[volumecache]\nvolume_gb = 20\n[shim]"),
                   mechanism_override="volumecache")
    assert cfg.mechanism == "volumecache" and cfg.volume_gb == 20
```

- [ ] **Step 2: Run tests to verify the new ones fail and the old ones still pass**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_config.py -q`
Expected: 7 old PASS; new FAIL with `AttributeError: 'Config' object has no attribute 'mechanism'` / `ValueError: config missing keys`.

- [ ] **Step 3: Rewrite `config.py`**

```python
# runpod_testbed/config.py
from __future__ import annotations
import tomllib
from dataclasses import dataclass, field

SUPPORTED_MECHANISMS = ("shim", "volumecache", "modelstore")


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
    job_timeout_s: int
    mechanism: str = "shim"
    volume_gb: int = 0                                   # [volumecache]
    modelstore_endpoints: dict[str, str] = field(default_factory=dict)  # [modelstore.endpoints]


_SHARED = ("dc", "worker_cpu", "worker_deps", "models", "burst", "max_burst",
           "container_disk_gb", "scrape_interval_s", "job_timeout_s")
_SHIM = ("registry", "cache_image", "overlap", "max_pods", "pod_instance_id")
_SHIM_ABSENT = {"registry": "", "cache_image": "", "overlap": {}, "max_pods": 0,
                "pod_instance_id": ""}

# job_timeout_s: how long drive waits for a single job's result. runpod's
# Job.output(timeout=0) does NOT wait — it returns None immediately — so drive
# must pass a real ceiling that covers a cold worker's boot + dep install +
# download. Optional (defaulted) so pre-existing configs keep working.
_DEFAULTS = {"dc": "EU-RO-1", "job_timeout_s": 600}


def _shim_section(raw: dict) -> dict:
    """[shim] subtable, falling back to legacy top-level keys."""
    shim = dict(raw.get("shim", {}))
    for key in _SHIM:
        if key not in shim and key in raw:
            shim[key] = raw[key]
    return shim


def _check_placeholders(cfg: Config) -> None:
    pairs = [("registry", cfg.registry), ("cache_image", cfg.cache_image)] if cfg.mechanism == "shim" else []
    placeholders = [f"{n}={v}" for n, v in pairs if "CHANGEME" in v]
    placeholders += [f"models[{i}]={m}" for i, m in enumerate(cfg.models) if "CHANGEME" in m]
    if placeholders:
        raise ValueError(
            "config still has example placeholders — edit config.toml before "
            "provisioning (pods would pull a nonexistent image and time out): "
            + ", ".join(placeholders))


def _check_shim(cfg: Config) -> None:
    if len(cfg.overlap) > cfg.max_pods:
        raise ValueError(f"overlap groups {len(cfg.overlap)} exceed max_pods {cfg.max_pods}")
    if set(cfg.overlap) != {"A", "B", "C"}:
        raise ValueError(f"overlap groups must be exactly A,B,C (got {sorted(cfg.overlap)})")
    known = set(cfg.models)
    for grp, ms in cfg.overlap.items():
        for m in ms:
            if m not in known:
                raise ValueError(f"overlap[{grp}] references unknown model {m}")


def _mechanism_fields(raw: dict, mechanism: str) -> dict:
    """Shim keys (required for shim, defaulted otherwise) + per-mechanism subtable."""
    if mechanism not in SUPPORTED_MECHANISMS:
        raise ValueError(f"unknown mechanism {mechanism!r}; expected one of {SUPPORTED_MECHANISMS}")
    shim = _shim_section(raw)
    if mechanism == "shim":
        missing = [k for k in _SHIM if k not in shim]
        if missing:
            raise ValueError(f"config missing [shim] keys: {missing}")
        fields = {k: shim[k] for k in _SHIM}
    else:
        fields = dict(_SHIM_ABSENT)
    vc = raw.get("volumecache", {})
    if mechanism == "volumecache" and "volume_gb" not in vc:
        raise ValueError("config missing [volumecache] volume_gb")
    fields["volume_gb"] = int(vc.get("volume_gb", 0))
    fields["modelstore_endpoints"] = dict(raw.get("modelstore", {}).get("endpoints", {}))
    return fields


def load_str(text: str, mechanism_override: str | None = None) -> Config:
    raw = tomllib.loads(text)
    for key, default in _DEFAULTS.items():
        raw.setdefault(key, default)
    missing = [k for k in _SHARED if k not in raw]
    if missing:
        raise ValueError(f"config missing keys: {missing}")
    mechanism = mechanism_override or raw.get("mechanism", "shim")
    cfg = Config(**{k: raw[k] for k in _SHARED}, mechanism=mechanism,
                 **_mechanism_fields(raw, mechanism))
    _check_placeholders(cfg)
    if cfg.burst > cfg.max_burst:
        raise ValueError(f"burst {cfg.burst} exceeds max_burst {cfg.max_burst}")
    if cfg.mechanism == "shim":
        _check_shim(cfg)
    return cfg


def load(path: str, mechanism_override: str | None = None) -> Config:
    with open(path, "rb") as fh:
        return load_str(fh.read().decode(), mechanism_override)
```

- [ ] **Step 4: Run all config + provision tests**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_config.py runpod_testbed/tests/test_provision.py -q`
Expected: all PASS (the `Config(...)` construction in `test_provision._cfg` still works because new fields have defaults).

- [ ] **Step 5: Update `config.example.toml` to the new layout**

Replace the file contents with:

```toml
# Runpod cache-testbed configuration (example). Copy to config.toml and fill
# in real values -- do not commit config.toml (it may carry account-specific
# registry paths).

mechanism = "shim"                          # shim | volumecache | modelstore  (make ... MECHANISM=x overrides)

dc = "EU-RO-1"                              # Runpod datacenter id to pin pods/endpoints to
worker_cpu = "cpu5c-4-8"                    # Flash worker instance flavor (CPU only — no GPU load)
# Pin newer than Flash's base image (which ships huggingface_hub 1.6.0 + hf_xet
# 1.3.2): hf_xet 1.3.2 fetches Xet xorbs DIRECTLY from the CAS, ignoring the
# shim's rewritten reconstruction URLs (bypasses the cache); >=1.6.0 honors them.
worker_deps = ["huggingface_hub>=1.32.0", "hf_xet>=1.6.0"]
container_disk_gb = 20   # CPU pods cap container disk at 20 GB; larger caches need a network volume
scrape_interval_s = 5
job_timeout_s = 600      # how long drive waits for one job (cold worker: boot + dep install + download)

# Models under test (org/name@revision). Replace with real HF repo ids, e.g.:
#   "meta-llama/Llama-3.1-8B@main"
models = ["org/CHANGEME-a@main", "org/CHANGEME-b@main", "org/CHANGEME-c@main"]

burst = 4
max_burst = 8

# ---- shim (only read when mechanism = "shim") -------------------------------
[shim]
registry = "docker.io/CHANGEME"             # your container registry
cache_image = "CHANGEME/xet-cache-testbed:latest"  # image running the cache shim
max_pods = 3
pod_instance_id = "cpu3c-2-4"

# Overlap groups: each key is a pod label, value is the subset of `models`
# that pod should warm. Must be exactly A,B,C; every model must be in `models`.
[shim.overlap]
A = ["org/CHANGEME-a@main", "org/CHANGEME-b@main"]
B = ["org/CHANGEME-b@main", "org/CHANGEME-c@main"]
C = ["org/CHANGEME-c@main", "org/CHANGEME-a@main"]

# ---- volumecache (only read when mechanism = "volumecache") ----------------
[volumecache]
volume_gb = 50           # network volume attached at /runpod-volume (10..4096)

# ---- modelstore (only read when mechanism = "modelstore") ------------------
# One endpoint per model (platform limit: one cached model per endpoint).
# Leave `endpoints` empty to let `make up` deploy them; or map model -> an
# endpoint id you pre-created in the console with the model already cached.
[modelstore]
[modelstore.endpoints]
# "org/CHANGEME-a@main" = "endpoint-id"
```

- [ ] **Step 6: Commit**

```bash
git add runpod_testbed/config.py runpod_testbed/config.example.toml runpod_testbed/tests/test_config.py
git commit -m "feat(testbed): mechanism key + per-mechanism config subtables"
```

### Task 3: `worker/timing.py` — breakdown in `time_one` + shared `make_timing_row`

**Files:**
- Modify: `runpod_testbed/worker/timing.py`
- Test: `runpod_testbed/tests/test_timing.py` (append)

**Interfaces:**
- Produces:
  - `EMPTY_BREAKDOWN = {"download_s": None, "hydrate_s": None, "local_read_s": None}`
  - `download_fn(model)` may return `(nbytes, first_byte_s)` **or** `(nbytes, first_byte_s, breakdown: dict)`; `time_one` output gains `"breakdown"` (always all three keys).
  - `hf_download(model)` now returns a 3-tuple with `{"download_s": <wall of snapshot_download>}`.
  - `SCHEMA_PHASES = ("baseline", "populate", "warm")`; `schema_phase(job_phase) -> str` maps the driver's legacy `"cold"` to `"populate"`.
  - `make_timing_row(mechanism: str, job: dict, result: dict, wall_seconds: float) -> dict` — the spec's shared schema row; `result` is the worker's `run_download(...)` dict (or a driver-side failure dict without `results`).
- Consumed by: `drive/run.py::record` (Task 10), `harvest/report.py` (Task 11).

- [ ] **Step 1: Write the failing tests (append to `tests/test_timing.py`)**

```python
from runpod_testbed.worker.timing import EMPTY_BREAKDOWN, make_timing_row, schema_phase


def test_time_one_defaults_breakdown_for_two_tuple_downloaders():
    out = time_one(lambda m: (10, 0.01), "org/x@main")
    assert out["breakdown"] == EMPTY_BREAKDOWN


def test_time_one_merges_reported_breakdown():
    out = time_one(lambda m: (10, 0.01, {"hydrate_s": 0.4, "download_s": 0.1}), "org/x@main")
    assert out["breakdown"] == {"download_s": 0.1, "hydrate_s": 0.4, "local_read_s": None}


def test_time_one_error_still_has_breakdown():
    def boom(m):
        raise RuntimeError("x")
    assert time_one(boom, "m")["breakdown"] == EMPTY_BREAKDOWN


def test_schema_phase_maps_legacy_cold_to_populate():
    assert schema_phase("cold") == "populate"
    assert schema_phase("warm") == "warm" and schema_phase("baseline") == "baseline"


def test_make_timing_row_matches_shared_schema():
    job = {"mechanism": "volumecache", "endpoint": "volumecache", "model": "org/x@main",
           "phase": "warm", "replica": 2}
    result = {"worker_id": "w", "cold_first_invocation": True, "dep_upgrade_ms": 900,
              "results": [{"model": "org/x@main", "bytes": 1234, "first_byte_ms": 5,
                           "wall_seconds": 1.2, "ok": True, "error": None,
                           "breakdown": {"download_s": 0.1, "hydrate_s": 1.0, "local_read_s": None}}]}
    row = make_timing_row("volumecache", job, result, wall_seconds=7.5)
    assert row == {
        "mechanism": "volumecache", "phase": "warm", "model": "org/x@main",
        "wall_seconds": 7.5, "bytes": 1234,
        "breakdown": {"download_s": 0.1, "hydrate_s": 1.0, "local_read_s": None},
        "worker_cold": True, "ok": True,
    }


def test_make_timing_row_for_driver_side_failure():
    job = {"mechanism": "shim", "endpoint": "A", "model": "org/x@main", "phase": "cold", "replica": 0}
    row = make_timing_row("shim", job, {"ok": False, "error": "TimeoutError: Job timed out."}, 600.0)
    assert row["phase"] == "populate" and row["ok"] is False
    assert row["bytes"] == 0 and row["worker_cold"] is False
    assert row["breakdown"] == EMPTY_BREAKDOWN
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_timing.py -q`
Expected: 5 old PASS; new FAIL with `ImportError: cannot import name 'EMPTY_BREAKDOWN'`.

- [ ] **Step 3: Implement**

Replace `runpod_testbed/worker/timing.py` with:

```python
from __future__ import annotations
import os, time
from pathlib import Path

EMPTY_BREAKDOWN = {"download_s": None, "hydrate_s": None, "local_read_s": None}
SCHEMA_PHASES = ("baseline", "populate", "warm")
_PHASE_ALIAS = {"cold": "populate"}   # shim driver still says "cold" (tests pin it)


def _unpack(ret) -> tuple[int, float, dict]:
    """download_fn returns (bytes, first_byte_s) or (bytes, first_byte_s, breakdown)."""
    if len(ret) == 2:
        nbytes, first_byte_s = ret
        return nbytes, first_byte_s, dict(EMPTY_BREAKDOWN)
    nbytes, first_byte_s, breakdown = ret
    return nbytes, first_byte_s, {**EMPTY_BREAKDOWN, **breakdown}


def time_one(download_fn, model: str) -> dict:
    start = time.monotonic()
    try:
        nbytes, first_byte_s, breakdown = _unpack(download_fn(model))
        return {"model": model, "bytes": int(nbytes),
                "first_byte_ms": round(first_byte_s * 1000),
                "wall_seconds": round(time.monotonic() - start, 3),
                "breakdown": breakdown, "ok": True, "error": None}
    except Exception as e:  # errors are values — report, don't crash the worker
        return {"model": model, "bytes": 0, "first_byte_ms": None,
                "wall_seconds": round(time.monotonic() - start, 3),
                "breakdown": dict(EMPTY_BREAKDOWN), "ok": False, "error": str(e)}


def run_download(payload: dict, download_fn, *,
                 cold_first_invocation: bool = False, dep_upgrade_ms: int = 0) -> dict:
    return {"worker_id": os.environ.get("RUNPOD_POD_ID", "unknown"),
            "cold_first_invocation": cold_first_invocation,
            "dep_upgrade_ms": dep_upgrade_ms,
            "results": [time_one(download_fn, m) for m in payload.get("models", [])]}


def hf_download(model: str):
    """Real download (through the shim when HF_ENDPOINT is set; straight from HF otherwise)."""
    from huggingface_hub import snapshot_download
    repo, _, rev = model.partition("@")
    t0 = time.monotonic()
    path = snapshot_download(repo_id=repo, revision=rev or None)
    download_s = time.monotonic() - t0
    total = sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())
    return (total, download_s, {"download_s": round(download_s, 3)})  # first-byte stays coarse


def schema_phase(job_phase: str) -> str:
    return _PHASE_ALIAS.get(job_phase, job_phase)


def make_timing_row(mechanism: str, job: dict, result: dict, wall_seconds: float) -> dict:
    """The spec's shared timing schema — one row per driver job."""
    per_model = (result.get("results") or [{}])[0]
    return {
        "mechanism": mechanism,
        "phase": schema_phase(job["phase"]),
        "model": job["model"],
        "wall_seconds": float(wall_seconds),
        "bytes": int(per_model.get("bytes", 0)),
        "breakdown": {**EMPTY_BREAKDOWN, **per_model.get("breakdown", {})},
        "worker_cold": bool(result.get("cold_first_invocation", False)),
        "ok": bool(per_model.get("ok", False)),
    }
```

- [ ] **Step 4: Run tests**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_timing.py -q`
Expected: `11 passed`

- [ ] **Step 5: Commit**

```bash
git add runpod_testbed/worker/timing.py runpod_testbed/tests/test_timing.py
git commit -m "feat(testbed): shared timing schema row + handler breakdown"
```

### Task 4: `mechanisms/baseline.py` — the shared naive-HF control

**Files:**
- Create: `runpod_testbed/mechanisms/baseline.py`
- Test: `runpod_testbed/tests/test_baseline.py`

**Interfaces:**
- Consumes: `BASELINE_LABEL`, `WorkerSpec` from `mechanisms/base.py`.
- Produces:
  - `BASELINE_MECHANISM = "baseline"` (the `mechanism` value in timing rows)
  - `BASELINE_WORKER_SPEC = WorkerSpec(handler="baseline")` — plain `hf_download`, no `HF_ENDPOINT`, no volume.
  - `baseline_jobs(models: list[str]) -> list[dict]` — one `phase="baseline"` job per model on endpoint label `"baseline"`, `replica=0`, `mechanism="baseline"`.
- Consumed by: `drive/run.py` (Task 10), `worker/plan.py` (Task 6).

- [ ] **Step 1: Write the failing test**

```python
# runpod_testbed/tests/test_baseline.py
from runpod_testbed.mechanisms.base import BASELINE_LABEL
from runpod_testbed.mechanisms.baseline import (
    BASELINE_MECHANISM, BASELINE_WORKER_SPEC, baseline_jobs,
)


def test_baseline_jobs_one_per_model_on_control_endpoint():
    jobs = baseline_jobs(["org/a@main", "org/b@main"])
    assert jobs == [
        {"mechanism": BASELINE_MECHANISM, "endpoint": BASELINE_LABEL,
         "model": "org/a@main", "phase": "baseline", "replica": 0},
        {"mechanism": BASELINE_MECHANISM, "endpoint": BASELINE_LABEL,
         "model": "org/b@main", "phase": "baseline", "replica": 0},
    ]


def test_baseline_worker_spec_is_plain_hf():
    assert BASELINE_WORKER_SPEC.handler == "baseline"
    assert "HF_ENDPOINT" not in BASELINE_WORKER_SPEC.env
    assert BASELINE_WORKER_SPEC.network_volume_gb is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_baseline.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'runpod_testbed.mechanisms.baseline'`

- [ ] **Step 3: Implement**

```python
# runpod_testbed/mechanisms/baseline.py
"""The naive-HF control: one plain Flash CPU endpoint, hf_hub_download straight
from HF (no HF_ENDPOINT, no VolumeCache, no cached model). Provisioned for every
run by the shared core (worker/plan.py always emits it); the driver measures a
`baseline` phase on it for the same models the mechanism run uses.
"""
from __future__ import annotations
from runpod_testbed.mechanisms.base import BASELINE_LABEL, WorkerSpec

BASELINE_MECHANISM = "baseline"
BASELINE_WORKER_SPEC = WorkerSpec(handler="baseline")


def baseline_jobs(models: list[str]) -> list[dict]:
    return [{"mechanism": BASELINE_MECHANISM, "endpoint": BASELINE_LABEL,
             "model": m, "phase": "baseline", "replica": 0} for m in models]
```

- [ ] **Step 4: Run test**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_baseline.py -q`
Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add runpod_testbed/mechanisms/baseline.py runpod_testbed/tests/test_baseline.py
git commit -m "feat(testbed): shared naive-HF baseline control"
```

### Task 5: `provision/flash.py` — shared `flash deploy` wrapper + manifest → endpoint ids

**Files:**
- Create: `runpod_testbed/provision/flash.py`
- Test: `runpod_testbed/tests/test_flash.py`

**Interfaces:**
- Consumes: `WorkerSpec`, `Config`.
- Produces:
  - `WORKER_DIR = "runpod_testbed/worker"`, `MANIFEST_PATH = "runpod_testbed/worker/.flash/flash_manifest.json"`
  - `deploy_env(spec: WorkerSpec, cfg, hf_token: str, runid: str) -> dict[str,str]` — `MECHANISM`, `MODELS` (comma-joined), `HF_TOKEN`, `WORKER_CPU`, `WORKER_DEPS`, `WORKER_MAX`, `FLASH_ENV=xet-<runid>`, plus `spec.env`. Sets `VOLUME_NAME=xet-vc-<runid>` / `VOLUME_GB` when `spec.network_volume_gb` is not None.
  - `flash_deploy(env: dict, run=subprocess.run) -> None` — `["flash","deploy","--env", env["FLASH_ENV"]]`, `cwd=WORKER_DIR`, `check=True`, `env={**os.environ, **env}`.
  - `manifest_endpoint_ids(path=MANIFEST_PATH) -> dict[str,str]` — reuses `drive.run.endpoint_ids` (label = trailing `-` token: `xet-dl-A -> "A"`, `xet-dl-baseline -> "baseline"`, `xet-dl-m0 -> "m0"`). Labels therefore must not contain `-`.
- Consumed by: every mechanism's `provision` (Tasks 7, 16, 19).

- [ ] **Step 1: Write the failing test**

```python
# runpod_testbed/tests/test_flash.py
import json

from runpod_testbed.mechanisms.base import WorkerSpec
from runpod_testbed.provision.flash import (
    MANIFEST_PATH, WORKER_DIR, deploy_env, flash_deploy, manifest_endpoint_ids,
)
from runpod_testbed.tests.test_provision import _cfg


def test_deploy_env_carries_mechanism_models_and_worker_knobs():
    cfg = _cfg()
    spec = WorkerSpec(handler="shim", env={"POD_ADDR_A": "http://1:41"})
    env = deploy_env(spec, cfg, hf_token="hf_x", runid="r1")
    assert env["MECHANISM"] == "shim" and env["MODELS"] == "x"
    assert env["POD_ADDR_A"] == "http://1:41" and env["HF_TOKEN"] == "hf_x"
    assert env["WORKER_CPU"] == "cpu5c-4-8" and env["WORKER_DEPS"] == "huggingface_hub"
    assert env["WORKER_MAX"] == "3" and env["FLASH_ENV"] == "xet-r1"
    assert "VOLUME_NAME" not in env


def test_deploy_env_adds_volume_knobs_when_spec_wants_a_volume():
    env = deploy_env(WorkerSpec(handler="volumecache", network_volume_gb=50), _cfg(), "t", "r2")
    assert env["VOLUME_NAME"] == "xet-vc-r2" and env["VOLUME_GB"] == "50"


def test_flash_deploy_runs_cli_from_worker_dir(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    calls = []
    flash_deploy({"FLASH_ENV": "xet-r1", "MECHANISM": "shim"},
                 run=lambda argv, **kw: calls.append((argv, kw)))
    argv, kw = calls[0]
    assert argv == ["flash", "deploy", "--env", "xet-r1"]
    assert kw["cwd"] == WORKER_DIR and kw["check"] is True
    assert kw["env"]["MECHANISM"] == "shim" and kw["env"]["PATH"] == "/usr/bin"


def test_manifest_endpoint_ids_reads_flash_manifest(tmp_path):
    p = tmp_path / "flash_manifest.json"
    p.write_text(json.dumps({"resources": {
        "xet-dl-A": {"endpoint_id": "ep-a"},
        "xet-dl-baseline": {"endpoint_id": "ep-base"},
        "xet-dl-m0": {"endpoint_id": "ep-m0"},
    }}))
    assert manifest_endpoint_ids(str(p)) == {"A": "ep-a", "baseline": "ep-base", "m0": "ep-m0"}
    assert MANIFEST_PATH == "runpod_testbed/worker/.flash/flash_manifest.json"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_flash.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'runpod_testbed.provision.flash'`

- [ ] **Step 3: Implement**

```python
# runpod_testbed/provision/flash.py
"""Shared `flash deploy` plumbing. Flash tracks deployed endpoints in
worker/.flash, so deploy AND undeploy must run from WORKER_DIR."""
from __future__ import annotations
import json
import os
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation only — a runtime import here would cycle:
    # flash -> mechanisms (package __init__ imports shim) -> shim -> flash (half-initialized)
    from runpod_testbed.mechanisms.base import WorkerSpec

WORKER_DIR = "runpod_testbed/worker"
MANIFEST_PATH = f"{WORKER_DIR}/.flash/flash_manifest.json"


def deploy_env(spec: WorkerSpec, cfg, hf_token: str, runid: str) -> dict[str, str]:
    env = {
        "MECHANISM": cfg.mechanism,
        "MODELS": ",".join(cfg.models),
        "HF_TOKEN": hf_token,
        "WORKER_CPU": cfg.worker_cpu,
        "WORKER_DEPS": ",".join(spec.deps or cfg.worker_deps),
        "WORKER_MAX": str(cfg.burst),
        "FLASH_ENV": f"xet-{runid}",
        **spec.env,
    }
    if spec.network_volume_gb is not None:
        env["VOLUME_NAME"] = f"xet-vc-{runid}"
        env["VOLUME_GB"] = str(spec.network_volume_gb)
    return env


def flash_deploy(env: dict[str, str], run=subprocess.run) -> None:
    run(["flash", "deploy", "--env", env["FLASH_ENV"]],
        cwd=WORKER_DIR, env={**os.environ, **env}, check=True)


def manifest_endpoint_ids(path: str = MANIFEST_PATH) -> dict[str, str]:
    from runpod_testbed.drive.run import endpoint_ids
    with open(path) as fh:
        return endpoint_ids(json.load(fh))
```

- [ ] **Step 4: Run test**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_flash.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add runpod_testbed/provision/flash.py runpod_testbed/tests/test_flash.py
git commit -m "feat(testbed): shared flash deploy wrapper + manifest ids"
```

### Task 6: `worker/plan.py` + `flash_app.py` dispatch on `MECHANISM` (always deploys the baseline)

**Files:**
- Create: `runpod_testbed/worker/plan.py`
- Modify: `runpod_testbed/worker/flash_app.py`
- Test: `runpod_testbed/tests/test_worker_plan.py`

**Interfaces:**
- Consumes: env baked by `provision/flash.py::deploy_env` (`MECHANISM`, `MODELS`, `POD_ADDR_*`, `VOLUME_NAME`, `VOLUME_GB`).
- Produces:
  - `EndpointPlan(name: str, binding: str, downloader: str, env: dict[str,str], volume_gb: int|None)` dataclass.
  - `plan_endpoints(mechanism: str, models: list[str], env: Mapping[str,str]) -> list[EndpointPlan]` — always the baseline (`xet-dl-baseline` / `xet_dl_baseline` / `"baseline"`), plus: shim → `xet-dl-A/B/C` with `HF_ENDPOINT=POD_ADDR_*` (downloader `"shim"`); volumecache → `xet-dl-volumecache` with `HF_HOME` + `volume_gb` (downloader `"volumecache"`); modelstore → `xet-dl-m{i}` per model (label `m{i}`, no `-` so the manifest label parse works) with `MODEL=<model>` (downloader `"modelstore"`). An empty `models` list under modelstore yields only the baseline (used by the reuse path in Task 19).
  - `HF_HOME_DEFAULT = "/root/.cache/huggingface"`, `SHIM_LABELS = ("A", "B", "C")`.
  - `flash_app.py` module attribute per `binding` (Flash's scanner uses `dir(module)` + `isinstance(obj, Endpoint)` at import; the generated handler does `getattr(flash_app, func_name)`).
  - `flash_app._DOWNLOADERS: dict[str, callable]` — `"shim"`/`"baseline"` → `timing.hf_download`; `"volumecache"`/`"modelstore"` are filled in by Tasks 15/18 (until then they map to `hf_download` so the module imports cleanly).

Why `flash_app.py` reads env at import: the deployed worker re-imports the module to find its handler, and the env it sees is only the endpoint's `env=`; so every endpoint's runtime `env` carries `MECHANISM` (and `MODELS`) so the same bindings exist at deploy and at run time (the existing `POD_ADDR_*`-defaults-to-"" trick, generalized).

- [ ] **Step 1: Write the failing test**

```python
# runpod_testbed/tests/test_worker_plan.py
import pytest

from runpod_testbed.worker.plan import HF_HOME_DEFAULT, EndpointPlan, plan_endpoints

MODELS = ["org/a@main", "org/b@main"]


def _by_name(plans):
    return {p.name: p for p in plans}


def test_every_mechanism_gets_the_baseline_endpoint():
    for mech in ("shim", "volumecache", "modelstore"):
        base = _by_name(plan_endpoints(mech, MODELS, {}))["xet-dl-baseline"]
        assert base.binding == "xet_dl_baseline" and base.downloader == "baseline"
        assert "HF_ENDPOINT" not in base.env and base.volume_gb is None
        assert base.env["MECHANISM"] == mech


def test_shim_plan_matches_todays_endpoints():
    env = {"POD_ADDR_A": "http://1:41", "POD_ADDR_B": "http://2:42", "POD_ADDR_C": "http://3:43"}
    plans = _by_name(plan_endpoints("shim", MODELS, env))
    assert set(plans) == {"xet-dl-A", "xet-dl-B", "xet-dl-C", "xet-dl-baseline"}
    assert plans["xet-dl-A"] == EndpointPlan(
        name="xet-dl-A", binding="xet_dl_A", downloader="shim",
        env={"HF_ENDPOINT": "http://1:41", "MECHANISM": "shim", "MODELS": "org/a@main,org/b@main"},
        volume_gb=None)


def test_shim_plan_tolerates_missing_pod_addrs_at_worker_runtime():
    # Worker re-import: POD_ADDR_* absent -> HF_ENDPOINT "" (never a KeyError).
    plans = _by_name(plan_endpoints("shim", MODELS, {}))
    assert plans["xet-dl-B"].env["HF_ENDPOINT"] == ""


def test_volumecache_plan_single_endpoint_with_volume():
    plans = _by_name(plan_endpoints("volumecache", MODELS, {"VOLUME_GB": "50", "VOLUME_NAME": "xet-vc-r"}))
    vc = plans["xet-dl-volumecache"]
    assert vc.binding == "xet_dl_volumecache" and vc.downloader == "volumecache"
    assert vc.volume_gb == 50 and vc.env["HF_HOME"] == HF_HOME_DEFAULT
    assert vc.env["VOLUME_NAME"] == "xet-vc-r"


def test_modelstore_plan_one_endpoint_per_model():
    plans = _by_name(plan_endpoints("modelstore", MODELS, {}))
    assert plans["xet-dl-m0"].env["MODEL"] == "org/a@main"
    assert plans["xet-dl-m1"].env["MODEL"] == "org/b@main"
    assert plans["xet-dl-m1"].binding == "xet_dl_m1"
    assert plans["xet-dl-m0"].downloader == "modelstore" and plans["xet-dl-m0"].volume_gb is None


def test_modelstore_plan_with_no_models_is_baseline_only():
    assert [p.name for p in plan_endpoints("modelstore", [], {})] == ["xet-dl-baseline"]


def test_unknown_mechanism_is_an_error():
    with pytest.raises(ValueError, match="mechanism"):
        plan_endpoints("turbo", MODELS, {})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_worker_plan.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'runpod_testbed.worker.plan'`

- [ ] **Step 3: Implement `worker/plan.py`**

```python
# runpod_testbed/worker/plan.py
"""Pure plan of which Flash endpoints flash_app.py builds for a mechanism.

Kept SDK-free so it is unit-testable; flash_app.py turns each EndpointPlan into
a runpod_flash.Endpoint. Flash packages worker/ as the deploy root, so inside
the worker this module is imported as top-level `plan`, not
`runpod_testbed.worker.plan`.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Mapping

HF_HOME_DEFAULT = "/root/.cache/huggingface"
SHIM_LABELS = ("A", "B", "C")
ENDPOINT_PREFIX = "xet-dl"


@dataclass
class EndpointPlan:
    name: str            # Flash endpoint name, e.g. xet-dl-A
    binding: str         # module attribute + handler __name__, e.g. xet_dl_A
    downloader: str      # key into flash_app._DOWNLOADERS
    env: dict[str, str] = field(default_factory=dict)   # runtime env baked into the endpoint
    volume_gb: int | None = None


def _plan(label: str, downloader: str, common: dict, extra: dict, volume_gb=None) -> EndpointPlan:
    name = f"{ENDPOINT_PREFIX}-{label}"
    return EndpointPlan(name=name, binding=name.replace("-", "_"), downloader=downloader,
                        env={**extra, **common}, volume_gb=volume_gb)


def plan_endpoints(mechanism: str, models: list[str], env: Mapping[str, str]) -> list[EndpointPlan]:
    common = {"MECHANISM": mechanism, "MODELS": ",".join(models)}
    plans = [_plan("baseline", "baseline", common, {})]
    if mechanism == "shim":
        plans += [_plan(g, "shim", common, {"HF_ENDPOINT": env.get(f"POD_ADDR_{g}", "")})
                  for g in SHIM_LABELS]
        return plans
    if mechanism == "volumecache":
        gb = int(env.get("VOLUME_GB", "0")) or None
        extra = {"HF_HOME": HF_HOME_DEFAULT, "VOLUME_NAME": env.get("VOLUME_NAME", "")}
        return plans + [_plan("volumecache", "volumecache", common, extra, volume_gb=gb)]
    if mechanism == "modelstore":
        return plans + [_plan(f"m{i}", "modelstore", common, {"MODEL": m})
                        for i, m in enumerate(models)]
    raise ValueError(f"unknown mechanism {mechanism!r}")
```

- [ ] **Step 4: Run test**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_worker_plan.py -q`
Expected: `7 passed`

- [ ] **Step 5: Rewrite `worker/flash_app.py` to build endpoints from the plan**

Replace the whole file with:

```python
# Verified against the installed `runpod-flash` package (Endpoint signature
# inspected live via `inspect.signature`): cpu/datacenter/workers/idle_timeout/
# dependencies/env/volume all exist; DataCenter.EU_RO_1 exists as spelled.
import os
from runpod_flash import Endpoint, DataCenter
from runpod_flash.core.resources.network_volume import NetworkVolume
# Flash packages this worker/ dir as the deploy root, so timing.py / plan.py are
# top-level siblings here — NOT importable as runpod_testbed.worker.*.
from timing import run_download, hf_download
from plan import plan_endpoints

# Pin >= the versions that honor HF_ENDPOINT for Xet xorb fetches. Flash's base
# image ships huggingface_hub 1.6.0 + hf_xet 1.3.2, and hf_xet 1.3.2 pulls xorbs
# straight from the CAS (bypassing the shim); >=1.6.0 uses the rewritten URLs.
_DEPS = os.environ.get("WORKER_DEPS", "huggingface_hub>=1.32.0,hf_xet>=1.6.0").split(",")
_CPU = os.environ.get("WORKER_CPU", "cpu5c-4-8")
_MAX = int(os.environ.get("WORKER_MAX", "4"))
_HF_TOKEN = os.environ.get("HF_TOKEN", "")
_IDLE_TIMEOUT_S = 30
# MECHANISM/MODELS are present at deploy time (provision/flash.py::deploy_env)
# AND at worker runtime (baked into every endpoint's env by plan_endpoints), so
# the same bindings exist in both imports. Default "shim" keeps a bare import safe.
_MECHANISM = os.environ.get("MECHANISM", "shim")
_MODELS = [m for m in os.environ.get("MODELS", "").split(",") if m]
_UPGRADED: list = []  # once-flag for the runtime hf_xet upgrade workaround

# Tasks 15 / 18 replace the last two with volumecache_download / modelstore_local_read.
_DOWNLOADERS = {"shim": hf_download, "baseline": hf_download,
                "volumecache": hf_download, "modelstore": hf_download}


def _upgrade_hf_once() -> tuple[bool, int]:
    # Flash's base image ships hf_xet 1.3.2, which fetches Xet xorbs DIRECTLY
    # from the CAS and bypasses the shim. WORKER_DEPS does not upgrade the base's
    # pre-installed version, so force-upgrade once at first invocation, BEFORE
    # huggingface_hub is first imported (timing.hf_download imports it lazily).
    # Applied to every mechanism (baseline included) so hf versions are equal.
    import subprocess, sys, time
    if _UPGRADED:
        return False, 0
    t0 = time.monotonic()
    subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade",
                    "huggingface_hub>=1.32.0", "hf_xet>=1.6.0"],
                   check=False, capture_output=True)
    _UPGRADED.append(True)
    return True, round((time.monotonic() - t0) * 1000)


def _mk(plan):
    # The generated deployed handler resolves `flash_app.<func_name>` and CALLS
    # it as `func(**job_input)` — the job's `input` dict is splatted as kwargs.
    # So the module-level binding MUST be named exactly the handler __name__.
    download_fn = _DOWNLOADERS[plan.downloader]

    async def handler(**payload) -> dict:
        cold, dep_upgrade_ms = _upgrade_hf_once()
        return run_download(payload, download_fn,
                            cold_first_invocation=cold, dep_upgrade_ms=dep_upgrade_ms)

    handler.__name__ = handler.__qualname__ = plan.binding
    volume = None
    if plan.volume_gb is not None:
        volume = NetworkVolume(name=plan.env["VOLUME_NAME"], size=plan.volume_gb,
                               datacenter=DataCenter.EU_RO_1)
    return Endpoint(name=plan.name, cpu=_CPU, datacenter=DataCenter.EU_RO_1,
                    workers=(0, _MAX), idle_timeout=_IDLE_TIMEOUT_S, dependencies=_DEPS,
                    volume=volume, env={**plan.env, "HF_TOKEN": _HF_TOKEN})(handler)


# One module attribute per endpoint (xet_dl_A, xet_dl_baseline, xet_dl_ms_0, ...).
# Flash's scanner finds Endpoint instances via dir(module), so dynamic bindings work.
for _plan in plan_endpoints(_MECHANISM, _MODELS, os.environ):
    globals()[_plan.binding] = _mk(_plan)
```

- [ ] **Step 6: Import-smoke the worker module the way Flash does (no deploy)**

Run: `cd runpod_testbed/worker && MECHANISM=volumecache MODELS=org/a@main VOLUME_NAME=xet-vc-t VOLUME_GB=10 uv run --with runpod-flash python -c "import flash_app; print(sorted(n for n in dir(flash_app) if n.startswith('xet_dl_')))"; cd -`
Expected: `['xet_dl_baseline', 'xet_dl_volumecache']`. Repeat with `MECHANISM=shim` → `['xet_dl_A', 'xet_dl_B', 'xet_dl_C', 'xet_dl_baseline']`, and `MECHANISM=modelstore MODELS=org/a@main,org/b@main` → `['xet_dl_baseline', 'xet_dl_m0', 'xet_dl_m1']`.

- [ ] **Step 7: Commit**

```bash
git add runpod_testbed/worker/plan.py runpod_testbed/worker/flash_app.py runpod_testbed/tests/test_worker_plan.py
git commit -m "feat(testbed): worker builds endpoints from mechanism plan"
```

### Task 7: `mechanisms/shim.py` — port today's provision/teardown/jobs behind the protocol

**Files:**
- Create: `runpod_testbed/mechanisms/shim.py`
- Modify: `runpod_testbed/provision/up.py` (delete `_wait_addr`/`_wait_healthz`/`main` body — they move; keep `flash_deploy_env` and `State` untouched. `main` is rewritten in Task 9; for now leave it calling the old code path so the file still imports)
- Modify: `runpod_testbed/provision/down.py` (`cli_undeploy(env_name, names=ENDPOINTS)`)
- Test: `runpod_testbed/tests/test_mechanism_shim.py`

**Interfaces:**
- Consumes: `flash_deploy_env`, `State` (`provision/up.py`), `teardown`, `cli_undeploy`, `ENDPOINTS` (`provision/down.py`), `Fleet`, `parse_external_addr` (`provision/fleet.py`), `expand_jobs` (`drive/run.py`), `deploy_env`, `flash_deploy`, `manifest_endpoint_ids` (`provision/flash.py`), `ProvisionState`, `WorkerSpec`, `state_path`, `endpoint_name`.
- Produces: `ShimMechanism` with `name = "shim"`, `has_metrics() -> True`, `worker_spec(cfg) -> WorkerSpec(handler="shim")`, `jobs(cfg) -> list[dict]` (today's `expand_jobs` rows + `mechanism="shim"`, `endpoint=<group>`), `provision(cfg, runid, *, fleet=None, deploy=flash_deploy, manifest_ids=manifest_endpoint_ids, wait_healthz=_wait_healthz, environ=os.environ) -> ProvisionState`, `teardown(state, *, fleet=None, flash_undeploy=cli_undeploy) -> None`, `report_sections` (Task 8). Module constants `POD_WAIT_S = 300`, `POLL_S = 3`.
- `ProvisionState` layout for shim: `pods={"A": pid,...}`, `metrics_urls={"A": "http://ip:port",...}`, `endpoints={"A": eid, "B":..., "C":..., "baseline": eid}`.

- [ ] **Step 1: Write the failing tests**

```python
# runpod_testbed/tests/test_mechanism_shim.py
from runpod_testbed.mechanisms.base import BASELINE_LABEL, ProvisionState, WorkerSpec
from runpod_testbed.mechanisms.shim import ShimMechanism
from runpod_testbed.provision.down import ENDPOINTS
from runpod_testbed.tests.test_provision import _cfg

_PORTS = [{"ip": "1.2.3.4", "isIpPublic": True, "privatePort": 8000, "publicPort": 41001, "type": "tcp"}]


class _FakeFleet:
    def __init__(self):
        self.created, self.terminated = [], []

    def create_cache_pod(self, name, image, instance_id, disk_gb, dc, env):
        self.created.append({"name": name, "image": image, "dc": dc, "env": env})
        return f"pod-{name[-1]}"

    def get_pod_ports(self, pid):
        return _PORTS

    def terminate_pod(self, pid):
        self.terminated.append(pid)


def _cfg3():
    from dataclasses import replace
    return replace(_cfg(), models=["x", "y", "z"],
                   overlap={"A": ["x", "y"], "B": ["y", "z"], "C": ["z", "x"]})


def test_shim_identity_and_worker_spec():
    m = ShimMechanism()
    assert m.name == "shim" and m.has_metrics() is True
    assert m.worker_spec(_cfg3()) == WorkerSpec(handler="shim")


def test_shim_jobs_are_todays_expand_jobs_with_labels():
    jobs = ShimMechanism().jobs(_cfg3())
    assert len(jobs) == 6 + 6 * 3                       # 6 cold + 6 (group,model) * burst 3
    assert all(j["mechanism"] == "shim" and j["endpoint"] == j["group"] for j in jobs)
    assert jobs[0]["phase"] == "cold" and jobs[-1]["phase"] == "warm"


def test_shim_provision_creates_pods_deploys_flash_and_records_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                        # state lands in ./data
    fleet, deploys = _FakeFleet(), []
    environ = {"HF_TOKEN": "hf_t", "SHIM_AUTH_TOKEN": "s3", "RUNPOD_API_KEY": "rk"}
    st = ShimMechanism().provision(
        _cfg3(), "r1", fleet=fleet, deploy=lambda env: deploys.append(env),
        manifest_ids=lambda: {"A": "ep-a", "B": "ep-b", "C": "ep-c", BASELINE_LABEL: "ep-base"},
        wait_healthz=lambda addr, timeout_s: None, environ=environ)
    assert [c["name"] for c in fleet.created] == ["xet-cache-r1-A", "xet-cache-r1-B", "xet-cache-r1-C"]
    assert fleet.created[0]["env"]["FLEET_PREFIX"] == "xet-cache-r1-"
    assert fleet.created[0]["env"]["FLEET_SIZE"] == "3" and fleet.created[0]["env"]["SHIM_AUTH_TOKEN"] == "s3"
    assert st.pods == {"A": "pod-A", "B": "pod-B", "C": "pod-C"}
    assert st.metrics_urls["B"] == "http://1.2.3.4:41001"
    assert st.endpoints[BASELINE_LABEL] == "ep-base" and st.endpoints["A"] == "ep-a"
    env = deploys[0]
    assert env["POD_ADDR_C"] == "http://1.2.3.4:41001" and env["MECHANISM"] == "shim"
    assert env["FLASH_ENV"] == "xet-r1" and env["HF_TOKEN"] == "hf_t"
    assert ProvisionState.load("data/state-r1.json") == st  # saved incrementally
    assert open("data/last-runid").read() == "r1"


def test_shim_teardown_undeploys_its_endpoints_then_terminates_pods():
    fleet, undeployed = _FakeFleet(), []
    st = ProvisionState(mechanism="shim", runid="r1", pods={"A": "pA", "B": "pB"})
    ShimMechanism().teardown(st, fleet=fleet,
                             flash_undeploy=lambda env, names=ENDPOINTS: undeployed.append(tuple(names)))
    assert undeployed == [ENDPOINTS]
    assert fleet.terminated == ["pA", "pB"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_mechanism_shim.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'runpod_testbed.mechanisms.shim'`

- [ ] **Step 3: Generalize `cli_undeploy` in `provision/down.py`**

Change the function (keep everything else in the file as-is for now):

```python
def cli_undeploy(env_name: str, names: tuple[str, ...] = ENDPOINTS) -> None:
    # `flash undeploy` deletes by endpoint name (there is no --env); --all would
    # nuke unrelated endpoints in the account. Tolerate not-found per endpoint
    # (teardown-on-failure often runs before deploy created them). env_name is
    # unused by the CLI but kept for the teardown(flash_undeploy=...) contract.
    for name in names:
        subprocess.run(["flash", "undeploy", name, "--force"],
                       cwd=_WORKER_DIR, check=False)
```

- [ ] **Step 4: Implement `mechanisms/shim.py`**

```python
# runpod_testbed/mechanisms/shim.py
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

    def report_sections(self, jobs: list, metrics_rows: list) -> list[str]:
        return []   # filled in by Task 8
```

- [ ] **Step 5: Trim `provision/up.py`**

Delete `_wait_addr`, `_wait_healthz`, and the body of `main()` from `up.py`; keep `flash_deploy_env` and `State` byte-identical. Replace `main` temporarily with:

```python
def main() -> None:
    raise SystemExit("provision.up.main is rewired in Task 9")  # placeholder removed in Task 9
```

(The imports `subprocess`, `time`, `urllib.request` become unused — remove them; keep `json`, `os`, `sys`, `asdict`, `dataclass`.)

- [ ] **Step 6: Run the shim tests + the untouched legacy tests**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_mechanism_shim.py runpod_testbed/tests/test_provision.py runpod_testbed/tests/test_drive.py -q`
Expected: all PASS (`test_provision.py` still exercises `flash_deploy_env`, `State`, `teardown`, `cli_undeploy("ignored-env")` with the default names).

- [ ] **Step 7: Commit**

```bash
git add runpod_testbed/mechanisms/shim.py runpod_testbed/provision/up.py runpod_testbed/provision/down.py runpod_testbed/tests/test_mechanism_shim.py
git commit -m "refactor(testbed): port shim provisioning behind Mechanism"
```

### Task 8: Shim `report_sections` (moved from `report.main`) + registry + protocol-conformance test

**Files:**
- Modify: `runpod_testbed/mechanisms/shim.py` (`report_sections`)
- Modify: `runpod_testbed/mechanisms/__init__.py` (registry)
- Test: `runpod_testbed/tests/test_mechanism_shim.py` (append), `runpod_testbed/tests/test_mechanisms_registry.py` (new)

**Interfaces:**
- Consumes: `harvest.report.peering_payoff`, `harvest.report._final_by_pod` (unchanged).
- Produces:
  - `ShimMechanism.report_sections(jobs, metrics_rows) -> list[str]` — markdown lines for `## Per-pod hit rate / WAN bytes saved` and `## Peering payoff`, verbatim text of today's `report.main` blocks (including the "_No pod metrics captured..._" fallback).
  - `shim.per_pod_stats(metrics_rows) -> dict[pod, {"effective_hit_rate", "wan_bytes_saved"}]` (also used by `report.main` for the hit-rate plot).
  - `runpod_testbed.mechanisms.MECHANISMS: dict[str, Mechanism]`, `get_mechanism(name) -> Mechanism` (raises `ValueError` listing known names).
- Consumed by: `harvest/report.py` (Task 11), `provision/up.py`/`down.py` (Task 9), `drive/run.py` (Task 10). Phases 2/3 register their mechanisms here.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mechanism_shim.py`:

```python
_ROWS = [
    {"pod": "A", "ts": 1, "name": "xet_effective_hit_rate", "labels": {}, "value": 0.5},
    {"pod": "A", "ts": 2, "name": "xet_effective_hit_rate", "labels": {}, "value": 1.0},
    {"pod": "A", "ts": 2, "name": "xet_wan_bytes_saved", "labels": {}, "value": 4096},
    {"pod": "A", "ts": 2, "name": "xet_peer_bytes_total", "labels": {}, "value": 30},
    {"pod": "A", "ts": 2, "name": "xet_wan_bytes_total", "labels": {}, "value": 70},
]


def test_shim_report_sections_render_hit_rate_and_peering_tables():
    text = "\n".join(ShimMechanism().report_sections([], _ROWS))
    assert "## Per-pod hit rate / WAN bytes saved" in text
    assert "| A | 1.0000 | 4096 |" in text
    assert "## Peering payoff" in text and "- peer_fraction: 0.3000" in text


def test_shim_report_sections_without_metrics_say_so():
    text = "\n".join(ShimMechanism().report_sections([], []))
    assert text.count("No pod metrics captured for this run") == 2
```

New file:

```python
# runpod_testbed/tests/test_mechanisms_registry.py
"""Protocol-conformance: every registered mechanism implements Mechanism and
its ProvisionState round-trips through JSON. Phases 2/3 add config texts here."""
import pytest

from runpod_testbed.config import load_str
from runpod_testbed.mechanisms import MECHANISMS, get_mechanism
from runpod_testbed.mechanisms.base import ProvisionState, WorkerSpec
from runpod_testbed.tests.test_config import VALID

_CONFIG_TEXT = {
    "shim": VALID,
}
_METHODS = ("provision", "worker_spec", "teardown", "report_sections", "has_metrics", "jobs")


@pytest.mark.parametrize("name", sorted(MECHANISMS))
def test_registered_mechanism_conforms_to_protocol(name):
    m = get_mechanism(name)
    assert m.name == name
    for meth in _METHODS:
        assert callable(getattr(m, meth)), meth
    assert isinstance(m.has_metrics(), bool)
    assert m.report_sections([], []) == [] or all(isinstance(s, str) for s in m.report_sections([], []))


@pytest.mark.parametrize("name", sorted(MECHANISMS))
def test_registered_mechanism_worker_spec_and_jobs(name):
    cfg = load_str(_CONFIG_TEXT[name])
    m = get_mechanism(name)
    assert isinstance(m.worker_spec(cfg), WorkerSpec)
    jobs = m.jobs(cfg)
    assert jobs, "a mechanism must plan at least one job"
    for j in jobs:
        assert {"mechanism", "endpoint", "model", "phase", "replica"} <= set(j)
        assert j["mechanism"] == name and j["phase"] in ("cold", "populate", "warm")


@pytest.mark.parametrize("name", sorted(MECHANISMS))
def test_provision_state_roundtrips_for_each_mechanism(name, tmp_path):
    st = ProvisionState(mechanism=name, runid="r", endpoints={"baseline": "e0", "x": "e1"},
                        pods={"A": "p"}, volumes={"vc": "v"}, metrics_urls={"A": "http://a"})
    p = tmp_path / "s.json"
    st.save(str(p))
    assert ProvisionState.load(str(p)) == st


def test_get_mechanism_rejects_unknown():
    with pytest.raises(ValueError, match="shim"):
        get_mechanism("turbo")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_mechanism_shim.py runpod_testbed/tests/test_mechanisms_registry.py -q`
Expected: FAIL — `ImportError: cannot import name 'MECHANISMS'`; report_sections assertions fail (returns `[]`).

- [ ] **Step 3: Implement `report_sections` + `per_pod_stats` in `mechanisms/shim.py`**

Replace the stub method and add the helper (module level, above the class):

```python
def per_pod_stats(metrics_rows: list) -> dict:
    from runpod_testbed.harvest.report import _final_by_pod
    out = {}
    for pod in sorted({r["pod"] for r in metrics_rows}):
        pod_rows = [r for r in metrics_rows if r["pod"] == pod]
        out[pod] = {"effective_hit_rate": _final_by_pod(pod_rows, "xet_effective_hit_rate"),
                    "wan_bytes_saved": _final_by_pod(pod_rows, "xet_wan_bytes_saved")}
    return out


_NO_METRICS = ("_No pod metrics captured for this run "
               "(`make scrape` was not running); section omitted._")
```

```python
    def report_sections(self, jobs: list, metrics_rows: list) -> list[str]:
        from runpod_testbed.harvest.report import peering_payoff
        lines = ["## Per-pod hit rate / WAN bytes saved", ""]
        if not metrics_rows:
            lines += [_NO_METRICS, "", "## Peering payoff", "", _NO_METRICS, ""]
            return lines
        lines += ["| pod | effective_hit_rate | wan_bytes_saved |", "|---|---|---|"]
        for pod, d in per_pod_stats(metrics_rows).items():
            lines.append(f"| {pod} | {d['effective_hit_rate']:.4f} | {d['wan_bytes_saved']} |")
        payoff = peering_payoff(metrics_rows)
        lines += ["", "## Peering payoff", "",
                  f"- peer_bytes: {payoff['peer_bytes']}",
                  f"- wan_bytes: {payoff['wan_bytes']}",
                  f"- peer_fraction: {payoff['peer_fraction']:.4f}",
                  f"- hedge_win_ratio: {payoff['hedge_win_ratio']:.4f}", ""]
        return lines
```

- [ ] **Step 4: Implement the registry in `mechanisms/__init__.py`**

```python
# runpod_testbed/mechanisms/__init__.py
"""Registry of cache mechanisms under test. Phases 2/3 add entries."""
from __future__ import annotations
from runpod_testbed.mechanisms.shim import ShimMechanism

MECHANISMS = {
    "shim": ShimMechanism(),
}


def get_mechanism(name: str):
    if name not in MECHANISMS:
        raise ValueError(f"unknown mechanism {name!r}; known: {sorted(MECHANISMS)}")
    return MECHANISMS[name]
```

- [ ] **Step 5: Run tests**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_mechanism_shim.py runpod_testbed/tests/test_mechanisms_registry.py -q`
Expected: all PASS (`test_mechanisms_registry`: 3 parametrized × 1 mechanism + 1 = 4).

- [ ] **Step 6: Commit**

```bash
git add runpod_testbed/mechanisms/__init__.py runpod_testbed/mechanisms/shim.py runpod_testbed/tests/test_mechanism_shim.py runpod_testbed/tests/test_mechanisms_registry.py
git commit -m "feat(testbed): mechanism registry + shim report sections"
```

### Task 9: Generic `up` / `down` / `scrape` mains dispatching through the registry

**Files:**
- Modify: `runpod_testbed/provision/up.py` (`main`)
- Modify: `runpod_testbed/provision/down.py` (`teardown_all`, `main`)
- Modify: `runpod_testbed/harvest/scrape.py` (`main`)
- Test: `runpod_testbed/tests/test_provision_dispatch.py`

**Interfaces:**
- Consumes: `get_mechanism`, `ProvisionState`, `state_path`, `endpoint_name`, `BASELINE_LABEL`, `config.load(path, mechanism_override=...)`.
- Produces:
  - `down.teardown_all(mech, state: ProvisionState, *, flash_undeploy=None, delete_volume=None) -> list[str]` — order: `mech.teardown(state)` → `flash undeploy xet-dl-baseline` → each `state.volumes` id via `delete_volume(vid)`; every step best-effort, failures returned as strings. `None` defaults resolve at call time to the module attributes `cli_undeploy` / `_default_volume_delete`. `_default_volume_delete` starts as `_no_volume_delete(vid)`, which raises `RuntimeError("... not wired (Task 17)")` so an unwired volume shows up in `errored` rather than vanishing silently; Task 17 rebinds it to `provision.volumes.delete_network_volume`.
  - `up.main(argv=None, *, environ=os.environ, get_mech=None, load_config=None, now=time.strftime)` — `None` resolves to `mechanisms.get_mechanism` / `config.load` at call time; provisions via the mechanism; on any exception tears down whatever the partial state file records, then re-raises.
  - `scrape.main` reads `ProvisionState`, exits early when `has_metrics()` is False, scrapes `state.metrics_urls` (no `Fleet` needed).

- [ ] **Step 1: Write the failing tests**

```python
# runpod_testbed/tests/test_provision_dispatch.py
import types

import pytest

from runpod_testbed.mechanisms.base import BASELINE_LABEL, ProvisionState, state_path
from runpod_testbed.provision import up
from runpod_testbed.provision.down import teardown_all


class _FakeMech:
    name = "fake"

    def __init__(self, fail_provision_after_save=False, fail_teardown=False):
        self.fail_provision_after_save = fail_provision_after_save
        self.fail_teardown = fail_teardown
        self.torn_down = []

    def provision(self, cfg, runid):
        st = ProvisionState(mechanism=self.name, runid=runid, endpoints={"x": "e1"})
        st.save(state_path(runid))
        if self.fail_provision_after_save:
            raise RuntimeError("flash deploy exploded")
        return st

    def teardown(self, state):
        self.torn_down.append(state.runid)
        if self.fail_teardown:
            raise RuntimeError("pods gone already")

    def has_metrics(self):
        return False


def test_teardown_all_runs_mechanism_then_baseline_then_volumes_and_tolerates_errors():
    mech, undeployed, deleted = _FakeMech(fail_teardown=True), [], []
    st = ProvisionState(mechanism="fake", runid="r", volumes={"vc": "vol-1", "vc2": "vol-2"})

    def _delete(vid):
        deleted.append(vid)
        if vid == "vol-2":
            raise RuntimeError("409 in use")

    errored = teardown_all(mech, st, flash_undeploy=lambda env, names: undeployed.append(tuple(names)),
                           delete_volume=_delete)
    assert mech.torn_down == ["r"]
    assert undeployed == [("xet-dl-baseline",)]
    assert deleted == ["vol-1", "vol-2"]
    assert any("pods gone already" in e for e in errored) and any("vol-2" in e for e in errored)


def test_teardown_all_reports_unwired_volume_deletion_instead_of_silently_skipping():
    st = ProvisionState(mechanism="fake", runid="r", volumes={"vc": "vol-1"})
    errored = teardown_all(_FakeMech(), st, flash_undeploy=lambda env, names: None)
    assert any("vol-1" in e and "not wired" in e for e in errored)


def _fake_cfg(path, mechanism_override=None):
    return types.SimpleNamespace(mechanism=mechanism_override or "fake")


def test_up_main_provisions_and_prints_runid(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    mech = _FakeMech()
    up.main(["cfg.toml"], environ={"MECHANISM": "fake"},
            get_mech=lambda name: mech, load_config=_fake_cfg,
            now=lambda fmt: "20260924-000000")
    assert ProvisionState.load("data/state-20260924-000000.json").endpoints == {"x": "e1"}
    assert "UP runid=20260924-000000 mechanism=fake" in capsys.readouterr().out


def test_up_main_tears_down_partial_state_on_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mech = _FakeMech(fail_provision_after_save=True)
    undeployed = []
    monkeypatch.setattr("runpod_testbed.provision.down.cli_undeploy",
                        lambda env, names: undeployed.append(tuple(names)))
    with pytest.raises(RuntimeError, match="exploded"):
        up.main(["cfg.toml"], environ={}, get_mech=lambda name: mech,
                load_config=_fake_cfg, now=lambda fmt: "r9")
    assert mech.torn_down == ["r9"]                 # partial state was torn down
    assert undeployed == [("xet-dl-baseline",)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_provision_dispatch.py -q`
Expected: FAIL — `ImportError: cannot import name 'teardown_all'`

- [ ] **Step 3: Implement `teardown_all` + generic `main` in `provision/down.py`**

Append below `teardown(...)` (keep `teardown`, `cli_undeploy`, `ENDPOINTS`, `_WORKER_DIR`):

```python
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
```

Replace `main()`:

```python
def main() -> None:
    import sys
    from runpod_testbed.mechanisms import get_mechanism
    from runpod_testbed.mechanisms.base import ProvisionState
    st = ProvisionState.load(sys.argv[1])
    err = teardown_all(get_mechanism(st.mechanism), st)
    print(f"teardown done; errored (tolerated): {err}")
```

- [ ] **Step 4: Implement generic `main` in `provision/up.py`**

```python
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
```

(Add `import time` back to `up.py`'s imports.)

- [ ] **Step 5: Rewire `harvest/scrape.py::main` onto `ProvisionState`**

Replace the first lines of `main()` up to and including `out = ...` with:

```python
def main() -> None:  # integration: loop scrape all pods -> parquet
    import sys, signal
    from runpod_testbed import config
    from runpod_testbed.mechanisms import get_mechanism
    from runpod_testbed.mechanisms.base import ProvisionState

    st = ProvisionState.load(sys.argv[1])
    mech = get_mechanism(st.mechanism)
    if not mech.has_metrics():
        print(f"scrape: mechanism {mech.name!r} exposes no metrics endpoint; nothing to do", flush=True)
        return
    # Config is the source of truth for the scrape interval (scrape_interval_s);
    # sys.argv[2] remains as an optional one-off override.
    cfg = config.load("runpod_testbed/config.toml")
    interval = int(sys.argv[2]) if len(sys.argv) > 2 else cfg.scrape_interval_s
    addrs = dict(st.metrics_urls)        # label -> http://ip:port (recorded by provision)
    out = f"data/pod-metrics-{st.runid}.parquet"
```

The rest of the loop is unchanged (`for pid, a in addrs.items(): ...` now iterates labels A/B/C — the `pod` column in the parquet becomes the label, which is what `report_sections` groups on).

- [ ] **Step 6: Run the dispatch tests + full suite**

Run: `uv run --with pytest --with pyarrow pytest runpod_testbed/tests -q`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add runpod_testbed/provision/up.py runpod_testbed/provision/down.py runpod_testbed/harvest/scrape.py runpod_testbed/tests/test_provision_dispatch.py
git commit -m "refactor(testbed): up/down/scrape dispatch through mechanism registry"
```

### Task 10: `drive/run.py` — baseline + mechanism jobs, endpoint ids from state, timing row per job

**Files:**
- Modify: `runpod_testbed/drive/run.py`
- Test: `runpod_testbed/tests/test_drive.py` (append)

**Interfaces:**
- Consumes: `baseline_jobs`, `get_mechanism`, `ProvisionState`, `state_path`, `make_timing_row`.
- Produces:
  - `record(job, result, submit_ts, path)` — unchanged signature; when `"mechanism" in job` the persisted row gains `"timing": make_timing_row(job["mechanism"], job, result, return_ts - submit_ts)`.
  - `SEQUENTIAL_PHASES = ("baseline", "cold", "populate")`; `split_jobs(jobs) -> tuple[list, list]` (sequential, burst).
  - `resolve_endpoints(jobs, endpoints: dict) -> dict[label, id]` — raises `ValueError` naming missing labels **before** any spend.
  - `main` reads endpoint ids from `ProvisionState` (no more manifest read in drive).
- `expand_jobs`, `endpoint_ids`, `_submit_and_wait`, `start_jobs_file` unchanged.

- [ ] **Step 1: Write the failing tests (append to `tests/test_drive.py`)**

```python
from runpod_testbed.drive.run import SEQUENTIAL_PHASES, resolve_endpoints, split_jobs


def test_record_adds_shared_timing_row_when_job_carries_mechanism(tmp_path):
    jobs_path = str(tmp_path / "jobs.jsonl")
    job = {"mechanism": "baseline", "endpoint": "baseline", "model": "org/m@main",
           "phase": "baseline", "replica": 0}
    result = {"cold_first_invocation": False, "dep_upgrade_ms": 0,
              "results": [{"ok": True, "bytes": 42, "wall_seconds": 1.0, "model": "org/m@main",
                           "first_byte_ms": 1, "error": None,
                           "breakdown": {"download_s": 0.9, "hydrate_s": None, "local_read_s": None}}]}
    record(job, result, submit_ts=100.0, path=jobs_path)
    row = json.loads(open(jobs_path).read())
    t = row["timing"]
    assert set(t) == {"mechanism", "phase", "model", "wall_seconds", "bytes", "breakdown", "worker_cold", "ok"}
    assert t["mechanism"] == "baseline" and t["phase"] == "baseline" and t["bytes"] == 42
    assert t["wall_seconds"] == row["return_ts"] - 100.0      # driver-observed, not handler wall
    assert t["breakdown"]["download_s"] == 0.9 and t["ok"] is True


def test_record_without_mechanism_stays_legacy(tmp_path):
    jobs_path = str(tmp_path / "jobs.jsonl")
    record({"group": "A", "model": "m", "phase": "cold", "replica": 0}, {"ok": True}, 1.0, jobs_path)
    assert "timing" not in json.loads(open(jobs_path).read())


def test_split_jobs_sequential_first_then_burst():
    jobs = [{"phase": "warm"}, {"phase": "baseline"}, {"phase": "cold"}, {"phase": "populate"}]
    seq, burst = split_jobs(jobs)
    assert [j["phase"] for j in seq] == ["baseline", "cold", "populate"]
    assert burst == [{"phase": "warm"}] and SEQUENTIAL_PHASES == ("baseline", "cold", "populate")


def test_resolve_endpoints_fails_before_spend_on_missing_label():
    jobs = [{"endpoint": "baseline"}, {"endpoint": "volumecache"}]
    assert resolve_endpoints(jobs, {"baseline": "e0", "volumecache": "e1", "extra": "e2"}) == \
        {"baseline": "e0", "volumecache": "e1"}
    with pytest.raises(ValueError, match="volumecache"):
        resolve_endpoints(jobs, {"baseline": "e0"})
```

Add `import pytest` at the top of `tests/test_drive.py`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_drive.py -q`
Expected: FAIL — `ImportError: cannot import name 'SEQUENTIAL_PHASES'`

- [ ] **Step 3: Implement**

In `drive/run.py`, replace `record` and `main`, and add the two helpers:

```python
SEQUENTIAL_PHASES = ("baseline", "cold", "populate")


def record(job: dict, result: dict, submit_ts: float, path: str) -> None:
    return_ts = time.time()
    row = {**job, "submit_ts": submit_ts, "return_ts": return_ts, "result": result}
    if "mechanism" in job:  # shared timing schema (spec) — driver-observed wall
        from runpod_testbed.worker.timing import make_timing_row
        row["timing"] = make_timing_row(job["mechanism"], job, result, return_ts - submit_ts)
    with open(path, "a") as fh:
        fh.write(json.dumps(row) + "\n")


def split_jobs(jobs: list) -> tuple[list, list]:
    """Sequential phases (baseline / populate) first, then the warm burst."""
    seq = [j for j in jobs if j["phase"] in SEQUENTIAL_PHASES]
    burst = [j for j in jobs if j["phase"] not in SEQUENTIAL_PHASES]
    return seq, burst


def resolve_endpoints(jobs: list, endpoints: dict) -> dict:
    wanted = sorted({j["endpoint"] for j in jobs})
    missing = [w for w in wanted if w not in endpoints]
    if missing:
        raise ValueError(f"no endpoint id recorded for labels {missing}; state has {sorted(endpoints)}")
    return {w: endpoints[w] for w in wanted}


def main(argv: list | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv

    parser = argparse.ArgumentParser()
    parser.add_argument("config", nargs="?", default="config.toml")
    parser.add_argument("runid")
    parser.add_argument("--dry-run", metavar="CACHE_URL", default=None)
    args = parser.parse_args(argv)

    from runpod_testbed.config import load

    cfg = load(args.config, mechanism_override=os.environ.get("MECHANISM"))

    if args.dry_run:
        os.environ["HF_ENDPOINT"] = args.dry_run
        from runpod_testbed.worker.timing import hf_download

        for m in cfg.models:
            print(m, hf_download(m))
        return

    from runpod_testbed.mechanisms import get_mechanism
    from runpod_testbed.mechanisms.base import ProvisionState, state_path
    from runpod_testbed.mechanisms.baseline import baseline_jobs

    state = ProvisionState.load(state_path(args.runid))  # validates runid before spend
    mech = get_mechanism(state.mechanism)
    jobs = baseline_jobs(cfg.models) + mech.jobs(cfg)
    eids = resolve_endpoints(jobs, state.endpoints)
    sequential, burst = split_jobs(jobs)

    os.makedirs("data", exist_ok=True)
    jobs_path = f"data/jobs-{args.runid}.jsonl"
    start_jobs_file(jobs_path)  # fresh file so a re-run doesn't double-count

    for job in sequential:
        _submit_and_wait(eids[job["endpoint"]], job, jobs_path, cfg.job_timeout_s)

    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.burst) as pool:
        futures = [pool.submit(_submit_and_wait, eids[job["endpoint"]], job,
                               jobs_path, cfg.job_timeout_s)
                   for job in burst]
        for f in concurrent.futures.as_completed(futures):
            f.result()
```

Update the module header comment: replace "submits jobs to the 3 Flash serverless endpoints" with "submits baseline jobs to the control endpoint, then the mechanism's job plan (`mechanism.jobs`) to the endpoints recorded in `ProvisionState`". Keep the `flash_manifest.json` shape note (it documents `endpoint_ids`, still used by `provision/flash.py`).

- [ ] **Step 4: Run tests**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_drive.py -q`
Expected: `11 passed`

- [ ] **Step 5: Commit**

```bash
git add runpod_testbed/drive/run.py runpod_testbed/tests/test_drive.py
git commit -m "feat(testbed): drive baseline + mechanism jobs with shared timing rows"
```

### Task 11: `harvest/report.py` — shared timing core, mixed-mechanism fixture, `report_sections` hook, new output path

**Files:**
- Modify: `runpod_testbed/harvest/report.py`
- Test: `runpod_testbed/tests/test_report.py` (append)

**Interfaces:**
- Consumes: `get_mechanism`, `ProvisionState`, `state_path`, `SCHEMA_PHASES`, `shim.per_pod_stats`.
- Produces:
  - `SCHEMA_KEYS = frozenset({"mechanism","phase","model","wall_seconds","bytes","breakdown","worker_cold","ok"})`
  - `timing_rows(jobs) -> list[dict]` — `j["timing"]` for rows that have it.
  - `latency_by_schema_phase(rows) -> dict[phase, {"n","median_s","p95_s","total_bytes"}]` over OK rows.
  - `headline_vs_baseline(rows) -> {"n_ok","total_bytes","baseline_median_s","warm_median_s","speedup"}` with `speedup = baseline_median_s / warm_median_s` (None if either absent or warm 0).
  - `render_timing_core(runid, mechanism, jobs) -> list[str]` — `# Runpod cache testbed report — <mechanism> <runid>`, `## Headline` (baseline→warm speedup; legacy cold→warm line when present), `## Cold-start vs steady-state` (existing `coldstart`), `## Latency by phase` over schema phases.
  - `report_path(mechanism, runid) -> f"data/report-{mechanism}-{runid}.md"`, `plot_path(mechanism, runid, kind) -> f"data/report-{mechanism}-{runid}-{kind}.png"`.
- Existing `latency_by_phase`, `headline`, `coldstart`, `_final_by_pod`, `peering_payoff` stay unchanged.

- [ ] **Step 1: Write the failing tests (append to `tests/test_report.py`)**

```python
from runpod_testbed.harvest.report import (
    SCHEMA_KEYS, headline_vs_baseline, latency_by_schema_phase, render_timing_core,
    report_path, timing_rows,
)


def _t(mechanism, phase, wall, nbytes=100, ok=True, cold=False, **bd):
    return {"phase": phase, "result": {},
            "timing": {"mechanism": mechanism, "phase": phase, "model": "org/x@main",
                       "wall_seconds": wall, "bytes": nbytes,
                       "breakdown": {"download_s": None, "hydrate_s": None, "local_read_s": None, **bd},
                       "worker_cold": cold, "ok": ok}}

# Mixed-mechanism fixture: locks the schema shared by shim, volumecache, modelstore + baseline.
MIXED = [
    _t("baseline", "baseline", 30.0, download_s=29.0, cold=True),
    _t("baseline", "baseline", 32.0, download_s=31.0),
    _t("shim", "populate", 28.0, download_s=27.0, cold=True),
    _t("shim", "warm", 4.0, download_s=3.5),
    _t("volumecache", "populate", 31.0, download_s=30.0, hydrate_s=0.1),
    _t("volumecache", "warm", 2.0, hydrate_s=1.5, download_s=0.1),
    _t("modelstore", "warm", 6.0, local_read_s=0.2, cold=True),
    _t("modelstore", "warm", 0.0, ok=False, nbytes=0),
    {"phase": "cold", "result": {"results": [{"ok": True, "wall_seconds": 9.0, "bytes": 1}]}},  # legacy row, no timing
]


def test_every_timing_row_has_exactly_the_schema_keys():
    rows = timing_rows(MIXED)
    assert len(rows) == 8
    for r in rows:
        assert set(r) == SCHEMA_KEYS
        assert set(r["breakdown"]) == {"download_s", "hydrate_s", "local_read_s"}
        assert r["mechanism"] in ("shim", "volumecache", "modelstore", "baseline")
        assert r["phase"] in ("baseline", "populate", "warm")


def test_latency_by_schema_phase_ignores_failures_and_buckets_all_three_phases():
    lat = latency_by_schema_phase(timing_rows(MIXED))
    assert set(lat) == {"baseline", "populate", "warm"}
    assert lat["baseline"]["median_s"] == 31.0 and lat["baseline"]["n"] == 2
    assert lat["warm"]["n"] == 3                       # the failed modelstore row excluded
    assert lat["warm"]["total_bytes"] == 300


def test_headline_speedup_is_baseline_over_warm():
    h = headline_vs_baseline(timing_rows(MIXED))
    assert h["baseline_median_s"] == 31.0 and h["warm_median_s"] == 4.0
    assert h["speedup"] == 31.0 / 4.0
    assert h["n_ok"] == 7


def test_headline_speedup_none_without_baseline():
    assert headline_vs_baseline(timing_rows([_t("shim", "warm", 1.0)]))["speedup"] is None


def test_render_timing_core_mentions_mechanism_and_phases():
    text = "\n".join(render_timing_core("r1", "volumecache", MIXED))
    assert text.startswith("# Runpod cache testbed report — volumecache r1")
    assert "Baseline→warm speedup: 7.8× faster" in text
    assert "| baseline |" in text and "| populate |" in text and "| warm |" in text
    assert "## Cold-start vs steady-state" in text


def test_report_path_is_per_mechanism():
    assert report_path("modelstore", "r1") == "data/report-modelstore-r1.md"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_report.py -q`
Expected: FAIL — `ImportError: cannot import name 'SCHEMA_KEYS'`

- [ ] **Step 3: Implement the timing core (insert after `peering_payoff`, before `main`)**

```python
SCHEMA_KEYS = frozenset({"mechanism", "phase", "model", "wall_seconds", "bytes",
                         "breakdown", "worker_cold", "ok"})
SCHEMA_PHASES = ("baseline", "populate", "warm")


def report_path(mechanism: str, runid: str) -> str:
    return f"data/report-{mechanism}-{runid}.md"


def plot_path(mechanism: str, runid: str, kind: str) -> str:
    return f"data/report-{mechanism}-{runid}-{kind}.png"


def timing_rows(jobs: list) -> list:
    return [j["timing"] for j in jobs if "timing" in j]


def _stats(secs: list, total_bytes: int) -> dict:
    secs = sorted(secs)
    return {"n": len(secs), "median_s": st.median(secs),
            "p95_s": secs[max(0, round(0.95 * len(secs)) - 1)], "total_bytes": total_bytes}


def latency_by_schema_phase(rows: list) -> dict:
    buckets: dict = {}
    for r in rows:
        if r["ok"]:
            buckets.setdefault(r["phase"], []).append(r)
    return {p: _stats([r["wall_seconds"] for r in rs], sum(r["bytes"] for r in rs))
            for p, rs in buckets.items()}


def headline_vs_baseline(rows: list) -> dict:
    """The comparison money-stat: baseline_wall / mechanism_warm_wall (medians)."""
    lat = latency_by_schema_phase(rows)
    base, warm = lat.get("baseline"), lat.get("warm")
    speedup = None
    if base and warm and warm["median_s"] > 0:
        speedup = base["median_s"] / warm["median_s"]
    return {"n_ok": sum(1 for r in rows if r["ok"]),
            "total_bytes": sum(d["total_bytes"] for d in lat.values()),
            "baseline_median_s": base["median_s"] if base else None,
            "warm_median_s": warm["median_s"] if warm else None,
            "speedup": speedup}


def _fmt(v, spec: str) -> str:  # None-safe number formatting
    return format(v, spec) if v is not None else "n/a"


def _headline_lines(jobs: list) -> list[str]:
    h = headline_vs_baseline(timing_rows(jobs))
    lines = ["## Headline", ""]
    if h["speedup"] is not None:
        lines.append(f"- **Baseline→warm speedup: {h['speedup']:.1f}× faster** "
                     f"(median wall {h['baseline_median_s']:.2f}s → {h['warm_median_s']:.3f}s)")
    legacy = headline(jobs)   # shim-only cold→warm (kept for continuity with older reports)
    if legacy["speedup"] is not None:
        lines.append(f"- Cold→warm speedup (legacy, handler wall): {legacy['speedup']:.1f}×")
    lines += [f"- Jobs completed OK: {h['n_ok']}", f"- Total bytes served: {h['total_bytes']}", ""]
    return lines


def _coldstart_lines(jobs: list) -> list[str]:
    cs = coldstart(jobs)
    return ["## Cold-start vs steady-state", "",
            f"- Cold (first-invocation) jobs: {cs['n_cold']}",
            f"- Cold mean wall_seconds: {_fmt(cs['cold_mean_s'], '.3f')}",
            f"- Warm mean wall_seconds: {_fmt(cs['warm_mean_s'], '.3f')}",
            f"- Mean dep-upgrade ms: {_fmt(cs['dep_upgrade_ms'], '.0f')}", ""]


def _latency_lines(jobs: list) -> list[str]:
    lat = latency_by_schema_phase(timing_rows(jobs))
    lines = ["## Latency by phase", "", "| phase | n | median_s | p95_s | total_bytes |", "|---|---|---|---|---|"]
    for phase in SCHEMA_PHASES:
        if phase in lat:
            d = lat[phase]
            lines.append(f"| {phase} | {d['n']} | {d['median_s']:.3f} | {d['p95_s']:.3f} | {d['total_bytes']} |")
    return lines + [""]


def render_timing_core(runid: str, mechanism: str, jobs: list) -> list[str]:
    return ([f"# Runpod cache testbed report — {mechanism} {runid}", ""]
            + _headline_lines(jobs) + _coldstart_lines(jobs) + _latency_lines(jobs))
```

- [ ] **Step 4: Rewrite `main()` to use the core + the mechanism hook**

```python
def _load_jobs(path: str) -> list:
    import json
    jobs = []
    with open(path) as fh:
        for line in fh:
            if line.strip():
                jobs.append(json.loads(line))
    return jobs


def _load_metrics(path: str, runid: str) -> list:
    import os
    import pyarrow.parquet as pq
    if os.path.exists(path):
        return pq.read_table(path).to_pylist()
    print(f"warning: {path} not found — omitting pod hit-rate and peering sections "
          f"(run `make scrape RUNID={runid}` during a run to capture them)")
    return []


def _plots(mechanism: str, runid: str, jobs: list, metric_rows: list) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from runpod_testbed.mechanisms.shim import per_pod_stats
    lat = latency_by_schema_phase(timing_rows(jobs))
    phases = [p for p in SCHEMA_PHASES if p in lat]
    if phases:
        fig, ax = plt.subplots()
        ax.bar(phases, [lat[p]["median_s"] for p in phases])
        ax.set_ylabel("median wall_seconds (driver-observed)")
        ax.set_title(f"Latency by phase — {mechanism} ({runid})")
        fig.savefig(plot_path(mechanism, runid, "latency"))
        plt.close(fig)
    per_pod = per_pod_stats(metric_rows)
    if per_pod:
        fig, ax = plt.subplots()
        ax.bar(list(per_pod), [d["effective_hit_rate"] for d in per_pod.values()])
        ax.set_ylabel("effective_hit_rate")
        ax.set_title(f"Per-pod hit rate ({runid})")
        fig.savefig(plot_path(mechanism, runid, "hitrate"))
        plt.close(fig)


def main() -> None:  # integration: load jobs+metrics -> report.md + plots
    import os
    import sys
    from runpod_testbed.mechanisms import get_mechanism
    from runpod_testbed.mechanisms.base import ProvisionState, state_path

    runid = sys.argv[1]
    mechanism = "shim"   # pre-abstraction runs have no state.mechanism
    if os.path.exists(state_path(runid)):
        mechanism = ProvisionState.load(state_path(runid)).mechanism
    mech = get_mechanism(mechanism)

    jobs = _load_jobs(f"data/jobs-{runid}.jsonl")
    metric_rows = _load_metrics(f"data/pod-metrics-{runid}.parquet", runid) if mech.has_metrics() else []
    os.makedirs("data", exist_ok=True)
    _plots(mechanism, runid, jobs, metric_rows)

    lines = render_timing_core(runid, mechanism, jobs) + mech.report_sections(jobs, metric_rows)
    out_path = report_path(mechanism, runid)
    with open(out_path, "w") as fh:
        fh.write("\n".join(lines))
    print(f"wrote {out_path}")
```

Delete the old `main()` body entirely (its per-pod / peering rendering now lives in `shim.report_sections`, and its `_fmt` closure is the module-level `_fmt`).

- [ ] **Step 5: Run tests**

Run: `uv run --with pytest --with pyarrow pytest runpod_testbed/tests/test_report.py runpod_testbed/tests/test_mechanism_shim.py -q`
Expected: all PASS (6 legacy report tests untouched + 6 new).

- [ ] **Step 6: Commit**

```bash
git add runpod_testbed/harvest/report.py runpod_testbed/tests/test_report.py
git commit -m "feat(testbed): shared timing-core report + mechanism sections"
```

### Task 12: Makefile `MECHANISM` parametrization, README, full green suite (Phase 1 DoD)

**Files:**
- Modify: `runpod_testbed/Makefile`
- Modify: `runpod_testbed/README.md`

**Interfaces:**
- Produces: `make -C runpod_testbed up MECHANISM=<shim|volumecache|modelstore>` (env override to `config.load`), `drive`/`report`/`down RUNID=<id>` unchanged in shape, `report` output `data/report-<mechanism>-<runid>.md`, `demo-run` prints `data/report-*-$RUNID.md`.

- [ ] **Step 1: Edit the Makefile**

After `RUN := uv run --with runpod` add:

```make
# Mechanism under test. Empty = the `mechanism` key in config.toml decides;
# set MECHANISM=volumecache|modelstore|shim to override for this invocation.
# Exported so config.load(mechanism_override=os.environ.get("MECHANISM")) sees it.
MECHANISM ?=
ifneq ($(MECHANISM),)
export MECHANISM
endif
```

Change `test` to include pyarrow (the scrape parquet tests need it):

```make
test:
	@cd $(ROOT) && uv run --with pytest --with pyarrow pytest runpod_testbed/tests -v
```

In `demo-run`, replace the final `cat data/report-$$RUNID.md` with `cat data/report-*-$$RUNID.md` and the `report` help/echo lines:

```make
	@echo "    make up [MECHANISM=x]        provision the mechanism under test + the baseline endpoint"
	@echo "    make report RUNID=<id>       build the shared timing report (+ shim peering sections)"
```

Also update the header comment lines 8–13 to:

```make
#   make up [MECHANISM=shim|volumecache|modelstore]   provision mechanism + baseline endpoint
#   make scrape RUNID=<id>          poll pod metrics -> parquet (no-op for mechanisms without metrics)
#   make report RUNID=<id>          build data/report-<mechanism>-<id>.md
```

- [ ] **Step 2: Verify the Makefile still parses and `test` passes**

Run: `make -C runpod_testbed help && make -C runpod_testbed test`
Expected: help prints; all tests PASS.

- [ ] **Step 3: README — add a "Mechanisms" section and the cost note**

Insert after the "## Architecture" section heading paragraph (before the mermaid block) in `runpod_testbed/README.md`:

```markdown
## Mechanisms under test

The harness benchmarks one **mechanism** per run (`mechanism` in `config.toml`,
or `make up MECHANISM=...`), always alongside a **baseline** control endpoint
(`xet-dl-baseline`: plain `hf_hub_download` straight from HF). Every job emits
the same timing row (`mechanism / phase / model / wall_seconds / bytes /
breakdown / worker_cold / ok`, persisted in `data/jobs-<runid>.jsonl`), and the
headline is `baseline_wall / mechanism_warm_wall`.

| mechanism | what it exercises | phases | metrics scrape |
|---|---|---|---|
| `shim` | 3 peered xet-cache pods, `HF_ENDPOINT` interception (this README's original subject) | populate (cold) → warm burst | yes |
| `volumecache` | `runpod.serverless.VolumeCache` mirror on a network volume at `/runpod-volume` | populate → warm burst | no |
| `modelstore` | Runpod cached model pre-staged at `/runpod-volume/huggingface-cache/hub/...` | warm (scaled-from-zero cold start) | no |

All workers are CPU download-timing workers; nothing is loaded into VRAM.

**Cost note:** every run pays one extra WAN pull per model for the baseline on
top of the mechanism's own pulls. CPU-only + mandatory teardown keeps this to
cents per run; the baseline is what makes runs comparable, so do not skip it.
```

- [ ] **Step 4: Run the whole suite one last time (Phase 1 DoD: legacy tests untouched and green)**

Run: `git diff --stat 54944c2 -- runpod_testbed/tests/test_config.py runpod_testbed/tests/test_drive.py runpod_testbed/tests/test_report.py runpod_testbed/tests/test_timing.py runpod_testbed/tests/test_provision.py runpod_testbed/tests/test_scrape.py runpod_testbed/tests/test_selfconfig.py runpod_testbed/tests/test_fleet.py runpod_testbed/tests/test_preflight.py`
Expected: only additions (`+` lines) in `test_config.py`, `test_drive.py`, `test_report.py`, `test_timing.py`; zero changes in the others. Then `uv run --with pytest --with pyarrow pytest runpod_testbed/tests -q` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add runpod_testbed/Makefile runpod_testbed/README.md
git commit -m "docs(testbed): MECHANISM make param + mechanisms overview"
```

---

## Phase 2 — VolumeCache mechanism

### Task 13: `timing.volumecache_download` — hydrate → download → synchronous sync

**Files:**
- Modify: `runpod_testbed/worker/timing.py`
- Test: `runpod_testbed/tests/test_timing.py` (append)

**Interfaces:**
- Consumes: `from runpod.serverless import VolumeCache` (lazy, inside the function; runpod-python is in Flash's base image), `_unpack`, `hf_download`.
- Produces: `volumecache_download(model: str, *, download_fn=None, cache_factory=None, hf_home: str|None=None) -> (nbytes, first_byte_s, {"hydrate_s", "download_s"})`. `HF_HOME_FALLBACK = "/root/.cache/huggingface"`.

Decision (spec ambiguity): the spec wording says "wraps the download in `with VolumeCache(dirs=[...])`", but `VolumeCache.__exit__` calls `sync(background=True)` (runpod 1.12.0 `serverless/utils/rp_volume_cache.py:478`), so a populate job would return before the mirror is written and the first warm job would race it. The worker therefore calls `hydrate()` / `sync(background=False)` explicitly — same `VolumeCache(dirs, ...)` constructor (Global Constraint), deterministic ordering.

- [ ] **Step 1: Write the failing tests (append to `tests/test_timing.py`)**

```python
from runpod_testbed.worker.timing import volumecache_download


class _FakeVolumeCache:
    instances: list = []

    def __init__(self, dirs, *, namespace=None, volume_path="/runpod-volume", best_effort=True, max_workers=None):
        self.dirs, self.best_effort, self.calls = dirs, best_effort, []
        _FakeVolumeCache.instances.append(self)

    def hydrate(self):
        self.calls.append("hydrate")

    def sync(self, *, background=True):
        self.calls.append(f"sync(background={background})")


def test_volumecache_download_hydrates_downloads_then_syncs_synchronously():
    _FakeVolumeCache.instances.clear()
    order = []

    def fake_download(model):
        order.append("download")
        return (2048, 0.2, {"download_s": 0.2})

    nbytes, first_byte_s, bd = volumecache_download(
        "org/x@main", download_fn=fake_download, cache_factory=_FakeVolumeCache, hf_home="/tmp/hf")
    vc = _FakeVolumeCache.instances[0]
    assert vc.dirs == ["/tmp/hf"] and vc.best_effort is True
    assert vc.calls == ["hydrate", "sync(background=False)"] and order == ["download"]
    assert nbytes == 2048 and first_byte_s == 0.2
    assert set(bd) == {"hydrate_s", "download_s"} and bd["hydrate_s"] >= 0 and bd["download_s"] >= 0


def test_volumecache_download_defaults_hf_home_from_env(monkeypatch):
    _FakeVolumeCache.instances.clear()
    monkeypatch.setenv("HF_HOME", "/root/.cache/huggingface")
    volumecache_download("m", download_fn=lambda m: (1, 0.0), cache_factory=_FakeVolumeCache)
    assert _FakeVolumeCache.instances[0].dirs == ["/root/.cache/huggingface"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_timing.py -q`
Expected: FAIL — `ImportError: cannot import name 'volumecache_download'`

- [ ] **Step 3: Implement (append to `worker/timing.py`)**

```python
HF_HOME_FALLBACK = "/root/.cache/huggingface"


def volumecache_download(model: str, *, download_fn=None, cache_factory=None,
                         hf_home: str | None = None):
    """VolumeCache path: hydrate() restores the mirror from /runpod-volume, the HF
    download is then a local cache hit (or a real WAN pull on the populate run),
    and a SYNCHRONOUS sync() writes new files back so the next job can hydrate
    them. Explicit calls rather than `with VolumeCache(...)`: __exit__ syncs on a
    background daemon thread, which would return the populate job before the
    mirror is written and let the first warm job race it.
    """
    if cache_factory is None:
        from runpod.serverless import VolumeCache as cache_factory
    download_fn = download_fn or hf_download
    hf_home = hf_home or os.environ.get("HF_HOME", HF_HOME_FALLBACK)
    vc = cache_factory(dirs=[hf_home], best_effort=True)   # namespace defaults to RUNPOD_ENDPOINT_ID
    t0 = time.monotonic()
    vc.hydrate()
    hydrate_s = time.monotonic() - t0
    t1 = time.monotonic()
    nbytes, first_byte_s, _ = _unpack(download_fn(model))
    download_s = time.monotonic() - t1
    vc.sync(background=False)
    return (nbytes, first_byte_s,
            {"hydrate_s": round(hydrate_s, 3), "download_s": round(download_s, 3)})
```

- [ ] **Step 4: Run tests**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_timing.py -q`
Expected: `13 passed`

- [ ] **Step 5: Commit**

```bash
git add runpod_testbed/worker/timing.py runpod_testbed/tests/test_timing.py
git commit -m "feat(testbed): VolumeCache hydrate/download/sync timing path"
```

### Task 14: `provision/volumes.py` — Runpod REST network-volume list / find / delete

**Files:**
- Create: `runpod_testbed/provision/volumes.py`
- Test: `runpod_testbed/tests/test_volumes.py`

**Interfaces:**
- Produces:
  - `REST_BASE = "https://rest.runpod.io/v1"`
  - `list_network_volumes(api_key: str, opener=urllib.request.urlopen) -> list[dict]` — accepts both a bare list and `{"networkVolumes": [...]}` (Flash's client documents both).
  - `find_volume_id(name: str, volumes: list[dict]) -> str | None`
  - `delete_network_volume(volume_id: str, api_key: str | None = None, opener=urllib.request.urlopen) -> None` — `DELETE {REST_BASE}/networkvolumes/{id}`; 200/204 OK; 404 tolerated (already gone); anything else raises `RuntimeError` with status + body. `api_key` defaults to `os.environ["RUNPOD_API_KEY"]`.
- Consumed by: `mechanisms/volumecache.py` (Task 16), `provision/down.py` (Task 17).

Note: the spike (Task 0.1 Step 3) confirms the DELETE status code; if it differs from 200/204, add it to `_OK_DELETE`.

- [ ] **Step 1: Write the failing tests**

```python
# runpod_testbed/tests/test_volumes.py
import json
import urllib.error

import pytest

from runpod_testbed.provision.volumes import (
    REST_BASE, delete_network_volume, find_volume_id, list_network_volumes,
)


class _Resp:
    def __init__(self, status, body=b""):
        self.status, self._body = status, body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(status=200, body=b"[]", seen=None):
    def open_(req, timeout=None):
        if seen is not None:
            seen.append((req.get_method(), req.full_url, req.get_header("Authorization")))
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, None)
        return _Resp(status, body)
    return open_


def test_list_handles_bare_list_and_wrapped_shapes():
    vols = [{"id": "v1", "name": "xet-vc-r", "dataCenterId": "EU-RO-1"}]
    assert list_network_volumes("k", opener=_opener(body=json.dumps(vols).encode())) == vols
    assert list_network_volumes("k", opener=_opener(body=json.dumps({"networkVolumes": vols}).encode())) == vols


def test_list_sends_bearer_auth_to_rest_endpoint():
    seen = []
    list_network_volumes("secret", opener=_opener(seen=seen))
    assert seen == [("GET", f"{REST_BASE}/networkvolumes", "Bearer secret")]


def test_find_volume_id_by_name():
    vols = [{"id": "v1", "name": "a"}, {"id": "v2", "name": "xet-vc-r"}]
    assert find_volume_id("xet-vc-r", vols) == "v2"
    assert find_volume_id("nope", vols) is None


def test_delete_issues_delete_and_accepts_204():
    seen = []
    delete_network_volume("v2", api_key="k", opener=_opener(status=204, seen=seen))
    assert seen == [("DELETE", f"{REST_BASE}/networkvolumes/v2", "Bearer k")]


def test_delete_tolerates_404_already_gone():
    delete_network_volume("v2", api_key="k", opener=_opener(status=404))   # must not raise


def test_delete_raises_on_other_errors():
    with pytest.raises(RuntimeError, match="500"):
        delete_network_volume("v2", api_key="k", opener=_opener(status=500))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_volumes.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'runpod_testbed.provision.volumes'`

- [ ] **Step 3: Implement**

```python
# runpod_testbed/provision/volumes.py
"""Runpod REST network-volume helpers. Flash creates the volume (idempotently,
by name + datacenter) during `flash deploy`, but neither Flash's client nor
`flash undeploy` can delete one — teardown must, or the volume keeps billing."""
from __future__ import annotations
import json
import os
import urllib.error
import urllib.request

REST_BASE = "https://rest.runpod.io/v1"
_TIMEOUT_S = 20
_OK_DELETE = (200, 204)
_ALREADY_GONE = 404


def _request(method: str, url: str, api_key: str, opener) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method,
                                 headers={"Authorization": f"Bearer {api_key}"})
    try:
        with opener(req, timeout=_TIMEOUT_S) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, (e.read() if hasattr(e, "read") and e.fp else b"")


def list_network_volumes(api_key: str, opener=urllib.request.urlopen) -> list[dict]:
    status, body = _request("GET", f"{REST_BASE}/networkvolumes", api_key, opener)
    if status != 200:
        raise RuntimeError(f"GET /networkvolumes -> {status}: {body[:200]!r}")
    data = json.loads(body or b"[]")
    return data if isinstance(data, list) else data.get("networkVolumes", [])


def find_volume_id(name: str, volumes: list[dict]) -> str | None:
    for v in volumes:
        if v.get("name") == name:
            return v.get("id")
    return None


def delete_network_volume(volume_id: str, api_key: str | None = None,
                          opener=urllib.request.urlopen) -> None:
    api_key = api_key or os.environ["RUNPOD_API_KEY"]
    status, body = _request("DELETE", f"{REST_BASE}/networkvolumes/{volume_id}", api_key, opener)
    if status in _OK_DELETE or status == _ALREADY_GONE:
        return
    raise RuntimeError(f"DELETE /networkvolumes/{volume_id} -> {status}: {body[:200]!r}")
```

- [ ] **Step 4: Run tests**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_volumes.py -q`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add runpod_testbed/provision/volumes.py runpod_testbed/tests/test_volumes.py
git commit -m "feat(testbed): REST network-volume list/find/delete helpers"
```

### Task 15: Wire `volumecache_download` into `flash_app._DOWNLOADERS`

**Files:**
- Modify: `runpod_testbed/worker/flash_app.py`

**Interfaces:**
- Consumes: `timing.volumecache_download` (Task 13).
- Produces: `_DOWNLOADERS["volumecache"] is volumecache_download`.

- [ ] **Step 1: Edit the import and the table**

```python
from timing import run_download, hf_download, volumecache_download
```

```python
# Task 18 replaces the modelstore entry with modelstore_local_read.
_DOWNLOADERS = {"shim": hf_download, "baseline": hf_download,
                "volumecache": volumecache_download, "modelstore": hf_download}
```

- [ ] **Step 2: Import-smoke as Flash does**

Run: `cd runpod_testbed/worker && MECHANISM=volumecache MODELS=org/a@main VOLUME_NAME=xet-vc-t VOLUME_GB=10 uv run --with runpod-flash python -c "import flash_app; print(flash_app._DOWNLOADERS['volumecache'].__name__)"; cd -`
Expected: `volumecache_download`

- [ ] **Step 3: Commit**

```bash
git add runpod_testbed/worker/flash_app.py
git commit -m "feat(testbed): worker uses VolumeCache downloader for volumecache"
```

### Task 16: `mechanisms/volumecache.py` — provision (Flash `volume=`), jobs, teardown, registry

**Files:**
- Create: `runpod_testbed/mechanisms/volumecache.py`
- Modify: `runpod_testbed/provision/flash.py` (extract `volume_name(runid)`)
- Modify: `runpod_testbed/mechanisms/__init__.py` (register)
- Test: `runpod_testbed/tests/test_mechanism_volumecache.py`, `runpod_testbed/tests/test_mechanisms_registry.py` (add config text)

**Interfaces:**
- Consumes: `deploy_env`, `flash_deploy`, `manifest_endpoint_ids` (`provision/flash.py`), `list_network_volumes`, `find_volume_id` (`provision/volumes.py`), `cli_undeploy`, `ProvisionState`, `WorkerSpec`, `state_path`, `endpoint_name`.
- Produces:
  - `flash.volume_name(runid) -> f"xet-vc-{runid}"` (used by `deploy_env` and by `provision` to look the id up).
  - `VOLUME_LABEL = "volumecache"`; `VolumeCacheMechanism` with `name="volumecache"`, `has_metrics() -> False`, `worker_spec(cfg) -> WorkerSpec(handler="volumecache", network_volume_gb=cfg.volume_gb)`, `jobs(cfg)` = per model: one `populate` (replica 0) then `burst` × `warm`, all on endpoint label `"volumecache"`, `provision(cfg, runid, *, deploy=flash_deploy, manifest_ids=manifest_endpoint_ids, list_volumes=list_network_volumes, environ=os.environ) -> ProvisionState` (records `volumes={"volumecache": <id>}`), `teardown(state, *, flash_undeploy=None)` (undeploys `xet-dl-volumecache`; the volume itself is removed by `down.teardown_all`), `report_sections -> []`.

- [ ] **Step 1: Write the failing tests**

```python
# runpod_testbed/tests/test_mechanism_volumecache.py
from dataclasses import replace

from runpod_testbed.config import load_str
from runpod_testbed.mechanisms.base import BASELINE_LABEL, ProvisionState, WorkerSpec
from runpod_testbed.mechanisms.volumecache import VOLUME_LABEL, VolumeCacheMechanism
from runpod_testbed.provision.flash import volume_name
from runpod_testbed.tests.test_config import VALID_VOLUMECACHE

_ENV = {"HF_TOKEN": "hf_t", "RUNPOD_API_KEY": "rk"}


def _cfg():
    return load_str(VALID_VOLUMECACHE)   # models x,y ; burst 4 ; volume_gb 50


def test_identity_spec_and_no_metrics():
    m = VolumeCacheMechanism()
    assert m.name == "volumecache" and m.has_metrics() is False
    assert m.worker_spec(_cfg()) == WorkerSpec(handler="volumecache", network_volume_gb=50)
    assert m.report_sections([], []) == []


def test_jobs_populate_then_warm_burst_per_model():
    jobs = VolumeCacheMechanism().jobs(replace(_cfg(), burst=2))
    assert [(j["model"], j["phase"], j["replica"]) for j in jobs] == [
        ("org/x@main", "populate", 0), ("org/x@main", "warm", 0), ("org/x@main", "warm", 1),
        ("org/y@main", "populate", 0), ("org/y@main", "warm", 0), ("org/y@main", "warm", 1),
    ]
    assert all(j["endpoint"] == VOLUME_LABEL and j["mechanism"] == "volumecache" for j in jobs)


def test_provision_deploys_with_volume_and_records_volume_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    deploys, listed = [], []

    def _list(api_key):
        listed.append(api_key)
        return [{"id": "vol-77", "name": volume_name("r1"), "dataCenterId": "EU-RO-1"}]

    st = VolumeCacheMechanism().provision(
        _cfg(), "r1", deploy=lambda env: deploys.append(env),
        manifest_ids=lambda: {VOLUME_LABEL: "ep-vc", BASELINE_LABEL: "ep-base"},
        list_volumes=_list, environ=_ENV)
    env = deploys[0]
    assert env["MECHANISM"] == "volumecache" and env["VOLUME_GB"] == "50"
    assert env["VOLUME_NAME"] == "xet-vc-r1" and env["MODELS"] == "org/x@main,org/y@main"
    assert listed == ["rk"]
    assert st.endpoints == {VOLUME_LABEL: "ep-vc", BASELINE_LABEL: "ep-base"}
    assert st.volumes == {VOLUME_LABEL: "vol-77"} and st.pods == {}
    assert ProvisionState.load("data/state-r1.json") == st
    assert open("data/last-runid").read() == "r1"


def test_provision_warns_but_succeeds_when_volume_not_found(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    st = VolumeCacheMechanism().provision(
        _cfg(), "r2", deploy=lambda env: None,
        manifest_ids=lambda: {VOLUME_LABEL: "e", BASELINE_LABEL: "b"},
        list_volumes=lambda k: [], environ=_ENV)
    assert st.volumes == {}
    out = capsys.readouterr().out
    assert "xet-vc-r2" in out and "console" in out


def test_teardown_undeploys_only_its_endpoint():
    undeployed = []
    VolumeCacheMechanism().teardown(
        ProvisionState(mechanism="volumecache", runid="r1", volumes={VOLUME_LABEL: "vol-77"}),
        flash_undeploy=lambda env, names: undeployed.append((env, tuple(names))))
    assert undeployed == [("xet-r1", ("xet-dl-volumecache",))]
```

Add to `tests/test_mechanisms_registry.py`:

```python
from runpod_testbed.tests.test_config import VALID, VALID_VOLUMECACHE

_CONFIG_TEXT = {
    "shim": VALID,
    "volumecache": VALID_VOLUMECACHE,
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_mechanism_volumecache.py runpod_testbed/tests/test_mechanisms_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'runpod_testbed.mechanisms.volumecache'`; registry tests still pass for shim only.

- [ ] **Step 3: Implement**

In `provision/flash.py` add and use:

```python
def volume_name(runid: str) -> str:
    return f"xet-vc-{runid}"
```

and in `deploy_env` replace `env["VOLUME_NAME"] = f"xet-vc-{runid}"` with `env["VOLUME_NAME"] = volume_name(runid)`.

```python
# runpod_testbed/mechanisms/volumecache.py
"""VolumeCache as a Mechanism: one Flash CPU endpoint with a network volume
attached at /runpod-volume (Flash `Endpoint(volume=NetworkVolume(...))`, built
in worker/flash_app.py from VOLUME_NAME/VOLUME_GB). The worker mirrors HF_HOME
onto the volume via runpod.serverless.VolumeCache; the driver runs
populate -> warm burst per model.

Spike contingency (Phase 0 finding (a)): if Flash's `volume=` does not attach,
create the volume first with `POST {REST_BASE}/networkvolumes`
{"name": volume_name(runid), "size": cfg.volume_gb, "dataCenterId": cfg.dc},
record its id in state.volumes BEFORE deploy, bake VOLUME_ID into the deploy
env, and build `NetworkVolume(id=os.environ["VOLUME_ID"])` in flash_app._mk.
"""
from __future__ import annotations
import os

from runpod_testbed.mechanisms.base import ProvisionState, WorkerSpec, endpoint_name, state_path
from runpod_testbed.provision.down import cli_undeploy
from runpod_testbed.provision.flash import deploy_env, flash_deploy, manifest_endpoint_ids, volume_name
from runpod_testbed.provision.volumes import find_volume_id, list_network_volumes

VOLUME_LABEL = "volumecache"


class VolumeCacheMechanism:
    name = "volumecache"

    def has_metrics(self) -> bool:
        return False

    def worker_spec(self, cfg) -> WorkerSpec:
        return WorkerSpec(handler="volumecache", network_volume_gb=cfg.volume_gb)

    def jobs(self, cfg) -> list[dict]:
        jobs = []
        for m in cfg.models:   # populate (mirror empty -> WAN pull -> sync) then warm burst (hydrate)
            jobs.append(self._job(m, "populate", 0))
            jobs += [self._job(m, "warm", r) for r in range(cfg.burst)]
        return jobs

    def _job(self, model: str, phase: str, replica: int) -> dict:
        return {"mechanism": self.name, "endpoint": VOLUME_LABEL, "model": model,
                "phase": phase, "replica": replica}

    def provision(self, cfg, runid: str, *, deploy=flash_deploy, manifest_ids=manifest_endpoint_ids,
                  list_volumes=list_network_volumes, environ=os.environ) -> ProvisionState:
        state = ProvisionState(mechanism=self.name, runid=runid)
        os.makedirs("data", exist_ok=True)
        state.save(state_path(runid))
        deploy(deploy_env(self.worker_spec(cfg), cfg, environ["HF_TOKEN"], runid))
        state.endpoints = manifest_ids()
        vid = find_volume_id(volume_name(runid), list_volumes(environ["RUNPOD_API_KEY"]))
        if vid:
            state.volumes[VOLUME_LABEL] = vid
        else:
            print(f"warning: network volume {volume_name(runid)!r} not found after deploy — "
                  f"teardown cannot remove it; check Storage in the Runpod console")
        state.save(state_path(runid))
        with open("data/last-runid", "w") as fh:
            fh.write(runid)
        return state

    def teardown(self, state: ProvisionState, *, flash_undeploy=None) -> None:
        # The volume is deleted by down.teardown_all from state.volumes.
        (flash_undeploy or cli_undeploy)(f"xet-{state.runid}", names=(endpoint_name(VOLUME_LABEL),))

    def report_sections(self, jobs: list, metrics_rows: list) -> list[str]:
        return []
```

Register in `mechanisms/__init__.py`:

```python
from runpod_testbed.mechanisms.shim import ShimMechanism
from runpod_testbed.mechanisms.volumecache import VolumeCacheMechanism

MECHANISMS = {
    "shim": ShimMechanism(),
    "volumecache": VolumeCacheMechanism(),
}
```

- [ ] **Step 4: Run tests**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_mechanism_volumecache.py runpod_testbed/tests/test_mechanisms_registry.py runpod_testbed/tests/test_flash.py -q`
Expected: all PASS (registry now 3 × 2 + 1 = 7).

- [ ] **Step 5: Commit**

```bash
git add runpod_testbed/mechanisms/volumecache.py runpod_testbed/mechanisms/__init__.py runpod_testbed/provision/flash.py runpod_testbed/tests/test_mechanism_volumecache.py runpod_testbed/tests/test_mechanisms_registry.py
git commit -m "feat(testbed): VolumeCache mechanism (provision/jobs/teardown)"
```

### Task 17: Wire volume deletion into `down.teardown_all`; docs (Phase 2 DoD)

**Files:**
- Modify: `runpod_testbed/provision/down.py`
- Modify: `runpod_testbed/README.md`, `runpod_testbed/Makefile` (help text)
- Test: `runpod_testbed/tests/test_provision_dispatch.py` (append)

**Interfaces:**
- Produces: `down._default_volume_delete is volumes.delete_network_volume`; `_no_volume_delete` deleted.

- [ ] **Step 1: Write the failing test (append to `tests/test_provision_dispatch.py`)**

```python
def test_teardown_all_deletes_volumes_via_rest_helper_by_default():
    from runpod_testbed.provision import down
    from runpod_testbed.provision.volumes import delete_network_volume
    assert down._default_volume_delete is delete_network_volume
```

And **replace** `test_teardown_all_reports_unwired_volume_deletion_instead_of_silently_skipping` (its premise is gone) with:

```python
def test_teardown_all_uses_module_default_when_delete_volume_not_given(monkeypatch):
    from runpod_testbed.provision import down
    deleted = []
    monkeypatch.setattr(down, "_default_volume_delete", lambda vid: deleted.append(vid))
    st = ProvisionState(mechanism="fake", runid="r", volumes={"vc": "vol-1"})
    assert teardown_all(_FakeMech(), st, flash_undeploy=lambda env, names: None) == []
    assert deleted == ["vol-1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_provision_dispatch.py -q`
Expected: FAIL — `assert down._default_volume_delete is delete_network_volume` (still `_no_volume_delete`).

- [ ] **Step 3: Implement**

In `provision/down.py` delete `_no_volume_delete` and set:

```python
from runpod_testbed.provision.volumes import delete_network_volume

_default_volume_delete = delete_network_volume   # best-effort REST DELETE; 404 tolerated
```

- [ ] **Step 4: Run the full suite**

Run: `uv run --with pytest --with pyarrow pytest runpod_testbed/tests -q`
Expected: all PASS.

- [ ] **Step 5: Docs**

Makefile `help`: add under "Manual pieces":

```make
	@echo "    make up MECHANISM=volumecache  Flash endpoint + network volume (/runpod-volume) + baseline"
```

README: append after the "Mechanisms under test" table:

```markdown
### volumecache specifics

- `make up MECHANISM=volumecache` deploys `xet-dl-volumecache` with a network
  volume `xet-vc-<runid>` (`[volumecache] volume_gb`) attached at `/runpod-volume`,
  plus the baseline endpoint. The worker runs `VolumeCache(dirs=[HF_HOME])`:
  `hydrate()` before the download, synchronous `sync()` after.
- Isolation: `VolumeCache`'s `namespace` defaults to `RUNPOD_ENDPOINT_ID`, so a
  fresh endpoint per run never sees an older run's mirror; `make down` deletes
  the volume (`DELETE /v1/networkvolumes/<id>`) so nothing stays billing.
- Secrets: `RUNPOD_API_KEY` + `HF_TOKEN` only (`SHIM_AUTH_TOKEN` is shim-only).
```

- [ ] **Step 6: Commit**

```bash
git add runpod_testbed/provision/down.py runpod_testbed/tests/test_provision_dispatch.py runpod_testbed/Makefile runpod_testbed/README.md
git commit -m "feat(testbed): teardown removes network volumes; volumecache docs"
```

---

## Phase 3 — Model Store mechanism

### Task 18: `timing.modelstore_local_read` — resolve the staged snapshot, assert presence, time the local read

**Files:**
- Modify: `runpod_testbed/worker/timing.py`
- Modify: `runpod_testbed/worker/flash_app.py` (`_DOWNLOADERS["modelstore"]`)
- Test: `runpod_testbed/tests/test_timing.py` (append)

**Interfaces:**
- Produces:
  - `MODELSTORE_ROOT = "/runpod-volume/huggingface-cache/hub"`
  - `modelstore_snapshot_dir(model: str, root: str) -> Path` — `models--{org}--{name}/snapshots/{hash}`; the hash comes from `refs/{rev}` when present, else the single snapshot dir; anything else raises `FileNotFoundError` with an actionable message.
  - `modelstore_local_read(model: str, root: str | None = None) -> (nbytes, local_read_s, {"local_read_s": ...})` — no HF download; walks + stats every file in the snapshot (presence + size), errors if empty. If `MODEL` is set in the environment (baked by `plan_endpoints`) and differs from `model`, raises `ValueError` — a job routed to the wrong per-model endpoint must not silently read another model.
- Decision (spec: "asserts the model is present with expected size (and sha where available)"): the harness has no offline source of truth for expected sizes without a network call to HF, which would pollute the timing; presence + non-empty byte count is asserted and the byte total is reported so the report's `bytes` column can be compared against the baseline row for the same model.

- [ ] **Step 1: Write the failing tests (append to `tests/test_timing.py`)**

```python
import pytest

from runpod_testbed.worker.timing import MODELSTORE_ROOT, modelstore_local_read, modelstore_snapshot_dir


def _stage(root, org="org", name="x", rev="main", sha="abc123", files=(("model.safetensors", 1000), ("config.json", 24))):
    base = root / f"models--{org}--{name}"
    snap = base / "snapshots" / sha
    snap.mkdir(parents=True)
    for fname, size in files:
        (snap / fname).write_bytes(b"\0" * size)
    if rev:
        (base / "refs").mkdir()
        (base / "refs" / rev).write_text(sha + "\n")
    return snap


def test_snapshot_dir_follows_refs_then_falls_back_to_single_snapshot(tmp_path):
    snap = _stage(tmp_path)
    assert modelstore_snapshot_dir("org/x@main", str(tmp_path)) == snap
    snap2 = _stage(tmp_path, name="y", rev=None, sha="deadbeef")
    assert modelstore_snapshot_dir("org/y@main", str(tmp_path)) == snap2


def test_snapshot_dir_errors_when_model_not_staged(tmp_path):
    with pytest.raises(FileNotFoundError, match="org/missing@main"):
        modelstore_snapshot_dir("org/missing@main", str(tmp_path))


def test_local_read_reports_bytes_and_breakdown(tmp_path):
    _stage(tmp_path)
    nbytes, local_read_s, bd = modelstore_local_read("org/x@main", root=str(tmp_path))
    assert nbytes == 1024 and local_read_s >= 0
    assert set(bd) == {"local_read_s"} and MODELSTORE_ROOT == "/runpod-volume/huggingface-cache/hub"


def test_local_read_errors_on_empty_snapshot(tmp_path):
    _stage(tmp_path, files=())
    with pytest.raises(FileNotFoundError, match="no files"):
        modelstore_local_read("org/x@main", root=str(tmp_path))


def test_local_read_rejects_model_mismatch_with_endpoint(tmp_path, monkeypatch):
    _stage(tmp_path)
    monkeypatch.setenv("MODEL", "org/other@main")
    with pytest.raises(ValueError, match="org/other@main"):
        modelstore_local_read("org/x@main", root=str(tmp_path))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_timing.py -q`
Expected: FAIL — `ImportError: cannot import name 'MODELSTORE_ROOT'`

- [ ] **Step 3: Implement (append to `worker/timing.py`)**

```python
MODELSTORE_ROOT = "/runpod-volume/huggingface-cache/hub"


def modelstore_snapshot_dir(model: str, root: str) -> Path:
    """Runpod cached-model layout mirrors HF_HOME/hub: models--{org}--{name}/snapshots/{hash}."""
    repo, _, rev = model.partition("@")
    org, _, name = repo.partition("/")
    base = Path(root) / f"models--{org}--{name}"
    ref = base / "refs" / (rev or "main")
    if ref.is_file():
        return base / "snapshots" / ref.read_text().strip()
    snapshots = base / "snapshots"
    dirs = sorted(p for p in snapshots.iterdir() if p.is_dir()) if snapshots.is_dir() else []
    if len(dirs) == 1:
        return dirs[0]
    raise FileNotFoundError(
        f"model {model}: expected refs/{rev or 'main'} or exactly one snapshot under "
        f"{snapshots} (found {len(dirs)}) — is the cached model declared on this "
        f"endpoint and finished staging?")


def modelstore_local_read(model: str, root: str | None = None):
    """Model Store path: the platform staged the weights before the handler ran;
    assert they are present and time the local walk. No HF download."""
    expected = os.environ.get("MODEL")
    if expected and expected != model:
        raise ValueError(f"job asked for {model} but this endpoint caches {expected}")
    root = root or os.environ.get("MODELSTORE_ROOT", MODELSTORE_ROOT)
    t0 = time.monotonic()
    snap = modelstore_snapshot_dir(model, root)
    files = [f for f in snap.rglob("*") if f.is_file()]
    if not files:
        raise FileNotFoundError(f"model {model}: snapshot {snap} has no files")
    total = sum(f.stat().st_size for f in files)
    local_read_s = time.monotonic() - t0
    return (total, local_read_s, {"local_read_s": round(local_read_s, 3)})
```

- [ ] **Step 4: Wire into `flash_app.py`**

```python
from timing import run_download, hf_download, volumecache_download, modelstore_local_read
```

```python
_DOWNLOADERS = {"shim": hf_download, "baseline": hf_download,
                "volumecache": volumecache_download, "modelstore": modelstore_local_read}
```

Remove the "Task 18 replaces..." comment above the table.

- [ ] **Step 5: Run tests + import smoke**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_timing.py -q`
Expected: `18 passed`
Run: `cd runpod_testbed/worker && MECHANISM=modelstore MODELS=org/a@main uv run --with runpod-flash python -c "import flash_app; print(flash_app._DOWNLOADERS['modelstore'].__name__)"; cd -`
Expected: `modelstore_local_read`

- [ ] **Step 6: Commit**

```bash
git add runpod_testbed/worker/timing.py runpod_testbed/worker/flash_app.py runpod_testbed/tests/test_timing.py
git commit -m "feat(testbed): Model Store local-read timing path"
```

### Task 19: `mechanisms/modelstore.py` — one endpoint per model, warm-only jobs, manual/API cached-model step, reuse path

**Files:**
- Create: `runpod_testbed/mechanisms/modelstore.py`
- Modify: `runpod_testbed/mechanisms/__init__.py` (register)
- Test: `runpod_testbed/tests/test_mechanism_modelstore.py`, `runpod_testbed/tests/test_mechanisms_registry.py` (add config text)

**Interfaces:**
- Consumes: `deploy_env`, `flash_deploy`, `manifest_endpoint_ids`, `cli_undeploy`, `fleet._graphql`, `ProvisionState`, `WorkerSpec`, `state_path`, `endpoint_name`, `timing.MODELSTORE_ROOT`.
- Produces:
  - `model_label(i: int) -> f"m{i}"`, `REUSED_PREFIX = "reused-"`
  - `ModelStoreMechanism` with `name="modelstore"`, `has_metrics() -> False`, `worker_spec(cfg) -> WorkerSpec(handler="modelstore")`, `jobs(cfg)` = per model `burst` × `warm` (replica 0 is the scaled-from-zero cold start; `worker_cold` in the timing row marks it) on label `m{i}`, or `reused-m{i}` for models mapped in `cfg.modelstore_endpoints`.
  - `provision(cfg, runid, *, deploy=flash_deploy, manifest_ids=manifest_endpoint_ids, graphql=None, environ=os.environ) -> ProvisionState`:
    - **owned path** (no `[modelstore.endpoints]`): `flash deploy` one `xet-dl-m{i}` per model + baseline; then `declare_cached_models(...)` — default implementation prints the manual console step (branch (i) below).
    - **reuse path** (`[modelstore.endpoints]` maps every model): validates each id exists via GraphQL `query { myself { endpoints { id name } } }`, deploys only the baseline (`MODELS=""` in the deploy env), records `endpoints={"reused-m{i}": id, "baseline": id}`. Partial maps raise `ValueError` before spend.
  - `teardown(state, *, flash_undeploy=None)` — undeploys `endpoint_name(label)` for every label that is neither `baseline` nor `reused-*` ("tears down what it owns").
  - `manual_step_lines(cfg, state) -> list[str]` — the operator instructions (also used by the README).

- [ ] **Step 1: Write the failing tests**

```python
# runpod_testbed/tests/test_mechanism_modelstore.py
from dataclasses import replace

import pytest

from runpod_testbed.config import load_str
from runpod_testbed.mechanisms.base import BASELINE_LABEL, ProvisionState, WorkerSpec
from runpod_testbed.mechanisms.modelstore import (
    REUSED_PREFIX, ModelStoreMechanism, manual_step_lines, model_label,
)
from runpod_testbed.tests.test_config import VALID_VOLUMECACHE

_ENV = {"HF_TOKEN": "hf_t", "RUNPOD_API_KEY": "rk"}
_MS = VALID_VOLUMECACHE.replace('mechanism = "volumecache"', 'mechanism = "modelstore"') \
    .replace("[volumecache]\nvolume_gb = 50", "[modelstore]")
_MS_REUSE = _MS + '\n[modelstore.endpoints]\n"org/x@main" = "ep-x"\n"org/y@main" = "ep-y"\n'


def test_identity_spec_and_no_metrics():
    m = ModelStoreMechanism()
    assert m.name == "modelstore" and m.has_metrics() is False
    assert m.worker_spec(load_str(_MS)) == WorkerSpec(handler="modelstore")
    assert m.report_sections([], []) == []


def test_jobs_are_warm_only_per_model_endpoint():
    jobs = ModelStoreMechanism().jobs(replace(load_str(_MS), burst=2))
    assert [(j["endpoint"], j["model"], j["phase"], j["replica"]) for j in jobs] == [
        ("m0", "org/x@main", "warm", 0), ("m0", "org/x@main", "warm", 1),
        ("m1", "org/y@main", "warm", 0), ("m1", "org/y@main", "warm", 1),
    ]
    assert all(j["mechanism"] == "modelstore" for j in jobs)
    assert model_label(3) == "m3"


def test_jobs_use_reused_labels_when_endpoints_are_mapped():
    jobs = ModelStoreMechanism().jobs(replace(load_str(_MS_REUSE), burst=1))
    assert [j["endpoint"] for j in jobs] == [f"{REUSED_PREFIX}m0", f"{REUSED_PREFIX}m1"]


def test_provision_owned_path_deploys_per_model_endpoints_and_prints_manual_step(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    deploys = []
    st = ModelStoreMechanism().provision(
        load_str(_MS), "r1", deploy=lambda env: deploys.append(env),
        manifest_ids=lambda: {"m0": "e0", "m1": "e1", BASELINE_LABEL: "eb"}, environ=_ENV)
    assert deploys[0]["MECHANISM"] == "modelstore" and deploys[0]["MODELS"] == "org/x@main,org/y@main"
    assert st.endpoints == {"m0": "e0", "m1": "e1", BASELINE_LABEL: "eb"}
    out = capsys.readouterr().out
    assert "xet-dl-m0" in out and "org/x@main" in out and "e1" in out
    assert "/runpod-volume/huggingface-cache/hub" in out
    assert ProvisionState.load("data/state-r1.json") == st


def test_provision_reuse_path_validates_ids_and_deploys_only_baseline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    deploys, queries = [], []

    def _graphql(q, key):
        queries.append((q, key))
        return {"data": {"myself": {"endpoints": [{"id": "ep-x", "name": "n1"}, {"id": "ep-y", "name": "n2"}]}}}

    st = ModelStoreMechanism().provision(
        load_str(_MS_REUSE), "r2", deploy=lambda env: deploys.append(env),
        manifest_ids=lambda: {BASELINE_LABEL: "eb"}, graphql=_graphql, environ=_ENV)
    assert deploys[0]["MODELS"] == "" and queries[0][1] == "rk"
    assert st.endpoints == {f"{REUSED_PREFIX}m0": "ep-x", f"{REUSED_PREFIX}m1": "ep-y", BASELINE_LABEL: "eb"}


def test_provision_reuse_path_fails_before_spend_on_unknown_or_partial_ids(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    deploys = []
    gone = lambda q, key: {"data": {"myself": {"endpoints": [{"id": "ep-x", "name": "n1"}]}}}
    with pytest.raises(ValueError, match="ep-y"):
        ModelStoreMechanism().provision(load_str(_MS_REUSE), "r3", deploy=lambda env: deploys.append(env),
                                        manifest_ids=lambda: {}, graphql=gone, environ=_ENV)
    partial = load_str(_MS_REUSE.replace('"org/y@main" = "ep-y"\n', ""))
    with pytest.raises(ValueError, match="org/y@main"):
        ModelStoreMechanism().provision(partial, "r4", deploy=lambda env: deploys.append(env),
                                        manifest_ids=lambda: {}, graphql=gone, environ=_ENV)
    assert deploys == []


def test_teardown_undeploys_owned_model_endpoints_only():
    undeployed = []
    st = ProvisionState(mechanism="modelstore", runid="r1",
                        endpoints={"m0": "e0", f"{REUSED_PREFIX}m1": "ep-y", BASELINE_LABEL: "eb"})
    ModelStoreMechanism().teardown(st, flash_undeploy=lambda env, names: undeployed.append(tuple(names)))
    assert undeployed == [("xet-dl-m0",)]


def test_manual_step_lines_list_each_owned_endpoint_with_its_model():
    st = ProvisionState(mechanism="modelstore", runid="r1", endpoints={"m0": "e0", "m1": "e1", BASELINE_LABEL: "eb"})
    text = "\n".join(manual_step_lines(load_str(_MS), st))
    assert "xet-dl-m0 (e0): cache org/x@main" in text and "xet-dl-m1 (e1): cache org/y@main" in text
```

Add to `tests/test_mechanisms_registry.py`:

```python
_CONFIG_TEXT = {
    "shim": VALID,
    "volumecache": VALID_VOLUMECACHE,
    "modelstore": VALID_VOLUMECACHE.replace('mechanism = "volumecache"', 'mechanism = "modelstore"')
                                   .replace("[volumecache]\nvolume_gb = 50", "[modelstore]"),
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_mechanism_modelstore.py runpod_testbed/tests/test_mechanisms_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'runpod_testbed.mechanisms.modelstore'`

- [ ] **Step 3: Implement — branch (i) is the default; branch (ii) applies only if the spike found an API**

```python
# runpod_testbed/mechanisms/modelstore.py
"""Runpod Model Store (cached models) as a Mechanism: one Flash CPU endpoint per
model (platform limit: one cached model per endpoint), weights pre-staged by
the platform to /runpod-volume/huggingface-cache/hub/... outside the handler.
The handler only asserts presence + times a local read; the yardstick is the
driver's end-to-end wall on a scaled-from-zero worker (replica 0 of each burst).

Owned path: `flash deploy` creates xet-dl-m{i}; the cached model is then
declared per Phase 0 finding (b) — manual console step (default) or API.
Reuse path ([modelstore.endpoints] maps every model to an endpoint id from a
previous run that already has its model cached): validate the ids, deploy only
the baseline, drive, and tear down only what this run owns.
"""
from __future__ import annotations
import os

from runpod_testbed.mechanisms.base import (
    BASELINE_LABEL, ProvisionState, WorkerSpec, endpoint_name, state_path,
)
from runpod_testbed.provision.down import cli_undeploy
from runpod_testbed.provision.flash import deploy_env, flash_deploy, manifest_endpoint_ids
from runpod_testbed.worker.timing import MODELSTORE_ROOT

REUSED_PREFIX = "reused-"
_ENDPOINTS_QUERY = "query { myself { endpoints { id name } } }"


def model_label(i: int) -> str:
    return f"m{i}"


def manual_step_lines(cfg, state: ProvisionState) -> list[str]:
    lines = ["MANUAL STEP (Model Store is console-only per the Phase 0 spike):",
             "  In the Runpod console, Serverless -> edit each endpoint below -> declare its cached model,",
             f"  then wait until the worker shows the files under {MODELSTORE_ROOT}/models--<org>--<name>/snapshots/<hash>/.",
             "  Then run: make -C runpod_testbed drive RUNID=" + state.runid]
    for i, model in enumerate(cfg.models):
        label = model_label(i)
        if label in state.endpoints:
            lines.append(f"    - {endpoint_name(label)} ({state.endpoints[label]}): cache {model}")
    return lines


def _validate_reused(cfg, graphql, api_key: str) -> dict[str, str]:
    missing_models = [m for m in cfg.models if m not in cfg.modelstore_endpoints]
    if missing_models:
        raise ValueError(f"[modelstore.endpoints] must map every model; missing {missing_models}")
    live = {e["id"] for e in graphql(_ENDPOINTS_QUERY, api_key)["data"]["myself"]["endpoints"]}
    unknown = sorted(eid for eid in cfg.modelstore_endpoints.values() if eid not in live)
    if unknown:
        raise ValueError(f"[modelstore.endpoints] ids not found on the account: {unknown}")
    return {f"{REUSED_PREFIX}{model_label(i)}": cfg.modelstore_endpoints[m]
            for i, m in enumerate(cfg.models)}


class ModelStoreMechanism:
    name = "modelstore"

    def has_metrics(self) -> bool:
        return False

    def worker_spec(self, cfg) -> WorkerSpec:
        return WorkerSpec(handler="modelstore")

    def jobs(self, cfg) -> list[dict]:
        prefix = REUSED_PREFIX if cfg.modelstore_endpoints else ""
        return [{"mechanism": self.name, "endpoint": f"{prefix}{model_label(i)}", "model": m,
                 "phase": "warm", "replica": r}
                for i, m in enumerate(cfg.models) for r in range(cfg.burst)]

    def provision(self, cfg, runid: str, *, deploy=flash_deploy, manifest_ids=manifest_endpoint_ids,
                  graphql=None, environ=os.environ) -> ProvisionState:
        if graphql is None:
            from runpod_testbed.provision.fleet import _graphql as graphql
        state = ProvisionState(mechanism=self.name, runid=runid)
        os.makedirs("data", exist_ok=True)
        env = deploy_env(self.worker_spec(cfg), cfg, environ["HF_TOKEN"], runid)
        reused = {}
        if cfg.modelstore_endpoints:            # validate BEFORE any spend
            reused = _validate_reused(cfg, graphql, environ["RUNPOD_API_KEY"])
            env["MODELS"] = ""                  # plan_endpoints -> baseline only
        state.save(state_path(runid))
        deploy(env)
        state.endpoints = {**manifest_ids(), **reused}
        state.save(state_path(runid))
        with open("data/last-runid", "w") as fh:
            fh.write(runid)
        if not reused:
            self.declare_cached_models(cfg, state, environ)
        return state

    def declare_cached_models(self, cfg, state: ProvisionState, environ) -> None:
        # Branch (i) — console-only (default until the spike says otherwise).
        print("\n".join(manual_step_lines(cfg, state)), flush=True)

    def teardown(self, state: ProvisionState, *, flash_undeploy=None) -> None:
        owned = [endpoint_name(label) for label in state.endpoints
                 if label != BASELINE_LABEL and not label.startswith(REUSED_PREFIX)]
        if owned:
            (flash_undeploy or cli_undeploy)(f"xet-{state.runid}", names=tuple(owned))

    def report_sections(self, jobs: list, metrics_rows: list) -> list[str]:
        return []
```

**Branch (ii) — only if Phase 0 finding (b) recorded an API.** Create `runpod_testbed/provision/modelstore_api.py` with `declare_cached_model(endpoint_id: str, model: str, api_key: str, opener=urllib.request.urlopen) -> None` that issues **exactly** the request recorded in the findings block (method, URL, JSON body — e.g. a REST `PATCH {REST_BASE}/endpoints/{endpoint_id}` or a GraphQL `saveEndpoint` mutation via `fleet._graphql`), raising `RuntimeError` on a non-2xx status, using the `_request` helper pattern from `provision/volumes.py`. Test it in `tests/test_modelstore_api.py` with the `_opener` fake from `tests/test_volumes.py` (assert method/URL/body/Bearer header; assert non-2xx raises). Then change `declare_cached_models` to:

```python
    def declare_cached_models(self, cfg, state: ProvisionState, environ) -> None:
        from runpod_testbed.provision.modelstore_api import declare_cached_model
        for i, model in enumerate(cfg.models):
            declare_cached_model(state.endpoints[model_label(i)], model, environ["RUNPOD_API_KEY"])
        print("declared cached models; the platform stages them before the first job", flush=True)
```

and swap `test_provision_owned_path_deploys_per_model_endpoints_and_prints_manual_step`'s stdout assertions for a monkeypatched `declare_cached_model` recording `(endpoint_id, model)` pairs `[("e0","org/x@main"),("e1","org/y@main")]`.

Register in `mechanisms/__init__.py`:

```python
from runpod_testbed.mechanisms.modelstore import ModelStoreMechanism
from runpod_testbed.mechanisms.shim import ShimMechanism
from runpod_testbed.mechanisms.volumecache import VolumeCacheMechanism

MECHANISMS = {
    "shim": ShimMechanism(),
    "volumecache": VolumeCacheMechanism(),
    "modelstore": ModelStoreMechanism(),
}
```

- [ ] **Step 4: Run tests**

Run: `uv run --with pytest pytest runpod_testbed/tests/test_mechanism_modelstore.py runpod_testbed/tests/test_mechanisms_registry.py -q`
Expected: all PASS (registry now 3 × 3 + 1 = 10).

- [ ] **Step 5: Commit**

```bash
git add runpod_testbed/mechanisms/modelstore.py runpod_testbed/mechanisms/__init__.py runpod_testbed/tests/test_mechanism_modelstore.py runpod_testbed/tests/test_mechanisms_registry.py
git commit -m "feat(testbed): Model Store mechanism (per-model endpoints, reuse path)"
```

### Task 20: Model Store docs + preflight note + final green suite (Phase 3 DoD)

**Files:**
- Modify: `runpod_testbed/README.md`, `runpod_testbed/Makefile` (help)
- Modify: `runpod_testbed/provision/preflight.py` (docstring only — the orphan check now legitimately sees reused modelstore endpoints)

**Interfaces:** none new.

- [ ] **Step 1: README — modelstore section**

Append after the "volumecache specifics" section:

```markdown
### modelstore specifics

- `make up MECHANISM=modelstore` deploys one CPU endpoint per model
  (`xet-dl-m0`, `xet-dl-m1`, ...) plus the baseline, then prints the **manual
  step**: in the console, declare each endpoint's cached model (one per
  endpoint — platform limit) and wait for staging to finish under
  `/runpod-volume/huggingface-cache/hub/models--<org>--<name>/snapshots/<hash>/`.
  (If the Phase 0 spike found an API, this step is automated — see
  `docs/superpowers/specs/2026-09-24-runpod-native-cache-testbeds-design.md`,
  "Spike findings".)
- The warm metric is the driver's end-to-end wall on a **scaled-from-zero**
  worker (replica 0 of each model's burst has `worker_cold = true`); the handler
  never downloads — it asserts presence and reports `local_read_s`.
- Reuse across runs: map `[modelstore.endpoints] "org/name@rev" = "<endpoint id>"`
  for endpoints you already configured; `make up` then validates the ids, deploys
  only the baseline, and `make down` leaves the reused endpoints alone (they keep
  billing only while workers run; idle timeout is 30 s). Preflight's orphan check
  will list them — that is expected in reuse mode.
```

- [ ] **Step 2: Makefile help line**

```make
	@echo "    make up MECHANISM=modelstore   per-model endpoints + baseline; prints the console cached-model step"
```

- [ ] **Step 3: Preflight docstring**

Append to the module docstring of `provision/preflight.py`:

```
In modelstore reuse mode ([modelstore.endpoints] set) the pre-created endpoints
are expected to exist and will be reported here as "orphaned" — read that line
as a reminder of what is being reused, not as a failure to fix.
```

- [ ] **Step 4: Full suite + legacy-test diff gate**

Run: `uv run --with pytest --with pyarrow pytest runpod_testbed/tests -q`
Expected: all PASS.
Run: `git diff --stat 54944c2 -- runpod_testbed/tests/test_provision.py runpod_testbed/tests/test_scrape.py runpod_testbed/tests/test_selfconfig.py runpod_testbed/tests/test_fleet.py runpod_testbed/tests/test_preflight.py`
Expected: no output (those files are byte-identical to the pre-plan commit).

- [ ] **Step 5: Commit**

```bash
git add runpod_testbed/README.md runpod_testbed/Makefile runpod_testbed/provision/preflight.py
git commit -m "docs(testbed): Model Store run/reuse instructions"
```

---

## Deferred — comparison roll-up (note only, no tasks)

All runs now emit `data/jobs-<runid>.jsonl` rows with a `timing` object in the shared schema and a per-mechanism `data/report-<mechanism>-<runid>.md`. The roll-up (out of scope here) reads `data/jobs-*.jsonl` across mechanism runs, joins on `model` + `phase`, and tabulates `baseline_median / warm_median` per mechanism. No schema work is needed for it — that is the point of Phase 1.

## Self-review notes

- **Spec coverage:** Decisions 1–6 → Tasks 1–12 (abstraction, baseline, schema, shim port, CPU-only); measurement model (driver-observed wall + handler breakdown) → Tasks 3, 10, 11; per-mechanism mechanics → Tasks 7 (shim), 13–17 (volumecache), 18–19 (modelstore); config → Task 2; Ops/Makefile + teardown safety → Tasks 9, 12, 17; testing (no-spend, conformance, mixed fixture, legacy green) → Tasks 8, 11, 12/20 diff gates; risks (spike, namespace isolation, cost note) → Tasks 0.x, 13, 12/17 docs.
- **Ambiguities resolved (recorded for the executor):**
  1. Spec protocol has no job-plan hook but prescribes three driver sequences → added `Mechanism.jobs(config)` (Task 1).
  2. `ProvisionState` gained `runid` so `scrape`/`report`/`down` derive paths from the state alone (Task 1).
  3. Shim driver phase stays `"cold"` internally (legacy tests pin it) and maps to schema `"populate"` in `make_timing_row` (Task 3).
  4. `[shim]` subtable **or** legacy top-level shim keys are accepted; shim keys are only required when `mechanism == "shim"` (Task 2).
  5. `with VolumeCache(...)` replaced by explicit `hydrate()` / `sync(background=False)` — `__exit__` syncs in a background thread and would race the warm burst (Task 13).
  6. Model Store "expected size / sha" assertion reduced to presence + byte total (no offline source of truth without a timing-polluting HF call) (Task 18).
  7. Model Store fallback = deploy owned endpoints + printed console step, plus a reuse path for pre-configured endpoints via `[modelstore.endpoints]` with "tear down only what this run owns" (Task 19).
  8. Baseline endpoint is deployed by the same `flash deploy` as the mechanism's endpoints (`plan_endpoints` always emits it), rather than a separate provisioning call (Task 6).
  9. Modelstore endpoint names are `xet-dl-m{i}` (labels without `-`) because `endpoint_ids()` keys by the trailing `-` token (Tasks 5/6).
- **Type consistency check:** `ProvisionState(mechanism, runid, endpoints, pods, volumes, metrics_urls)` used identically in Tasks 1, 7, 9, 16, 19; `WorkerSpec(handler, env, deps, network_volume_gb)` in Tasks 1, 4, 5, 7, 16, 19; `deploy_env(spec, cfg, hf_token, runid)` in Tasks 5, 7, 16, 19; `cli_undeploy(env_name, names=...)` in Tasks 7, 9, 16, 19; `make_timing_row(mechanism, job, result, wall_seconds)` in Tasks 3, 10; `_DOWNLOADERS` keys `shim/baseline/volumecache/modelstore` in Tasks 6, 15, 18 match `EndpointPlan.downloader` values.
