# Runpod-native cache testbeds (Model Store + VolumeCache) — design

**Status:** design approved, spec for implementation planning.
**Date:** 2026-09-24
**Author:** dean.quinanola@runpod.io

## Problem

The `runpod_testbed/` harness benchmarks the Go xet-cache shim on real Runpod
infrastructure and emits a report (cold→warm speedup, latency-by-phase, per-pod
peering payoff). Runpod already ships two native mechanisms that solve the same
problem — the repeated multi-GB model-download tax on serverless cold starts:

- **Model Store / cached models** — a platform tiered cache (host-local disk →
  DC-scoped network volume → origin). Weights are pre-staged to
  `/runpod-volume/huggingface-cache/hub/models--{org}--{name}/snapshots/{hash}/`;
  the worker is not billed while they download.
- **VolumeCache** (`from runpod.serverless import VolumeCache`) — a runpod-python
  context manager that mirrors cache directories onto an attached network volume:
  `hydrate()` restores on cold start, `sync()` writes new files back on exit.
  Per-endpoint, best-effort, opt-in.

We want to run the same kind of exercises against these two mechanisms and emit
**comparable reports**, so the shim's approach can eventually be ranked head-to-head
against Runpod's existing mechanisms.

## Goals

- Two new testbed mechanisms (Model Store, VolumeCache) that run CPU
  download-timing exercises and emit a report in a **shared timing schema**.
- A shared **naive-HF baseline** so every mechanism's speedup is measured against
  the identical yardstick.
- The existing shim testbed **ported onto the same abstraction**, so there is one
  code path and one report schema.
- A later comparison roll-up is trivial because all runs emit the same schema.

## Non-goals

- The comparison roll-up itself (deferred to a later phase; the schema makes it
  cheap).
- GPU workers / real model load into VRAM (excluded — it adds a load phase
  identical across mechanisms, plus cost and noise). A CPU download-timing path
  is the apples-to-apples comparison of the *transfer/cache* mechanisms.
- Any change to the shim's on-disk format, protocol handlers, or peering logic.

## Decisions (settled during brainstorming)

1. **Deliverable:** per-mechanism testbeds now, comparison roll-up later — but all
   emit a shared timing schema from day one.
2. **Worker type:** CPU, download-timing only (matches the shim testbed).
3. **Baseline:** a shared naive-HF-download control, provisioned for every run.
   Headline speedup = `baseline_wall / mechanism_warm_wall`.
4. **Structure:** extend `runpod_testbed/` with a `Mechanism` abstraction (shared
   core + per-mechanism plugins). Not sibling packages, not a standalone core lib.
5. **Shim:** ported onto the new abstraction now (single code path), behavior-
   preserving, guarded by the existing tests.
6. **Provisioning automation:** unknown for both mechanisms — resolved by a spike
   (Phase 0) before the `provision/` design is committed.

## The measurement model

The three mechanisms do **not** all acquire the model inside the handler:

- **shim, VolumeCache** — acquisition is in-handler (download-through-shim;
  `VolumeCache.hydrate()` restore). Timeable in-handler.
- **Model Store** — weights are pre-staged by the *platform* around worker startup,
  outside the handler and un-billed. Inside the handler the bytes are already local.

Therefore the **unifying yardstick is driver-observed end-to-end job wall-time**
(job submit → model ready), which `drive/run.py` already records. The handler
additionally reports an internal breakdown (`download_s` / `hydrate_s` /
`local_read_s`) as detail. This keeps a single honest headline metric across all
three mechanisms while preserving each one's internal story.

## Architecture

```
runpod_testbed/
  mechanisms/
    base.py         # Mechanism protocol + shared dataclasses
    baseline.py     # the naive-HF control (shared yardstick)
    shim.py         # ported from today's provision/worker/report bits
    volumecache.py  # NEW
    modelstore.py   # NEW
  provision/        # up/down dispatch to mechanism.provision()/teardown()
  worker/           # flash_app dispatches on mechanism; timing.py shared
  drive/run.py      # shared: submits baseline + warm jobs, records wall-time
  harvest/
    scrape.py       # runs only when a mechanism exposes a metrics endpoint
    report.py       # shared timing core + mechanism.report_sections() hook
  config.py         # adds `mechanism` + per-mechanism subtables
```

### The `Mechanism` protocol (`mechanisms/base.py`)

The protocol captures only what differs between mechanisms. Everything else
(driver, timing, report core, teardown safety) is shared.

```python
from dataclasses import dataclass, field
from typing import Protocol

@dataclass
class WorkerSpec:
    """How to build the Flash worker for this mechanism."""
    handler: str                       # branch key in worker/flash_app.py
    env: dict[str, str] = field(default_factory=dict)
    deps: list[str] = field(default_factory=list)
    network_volume_gb: int | None = None   # None = no volume attached

@dataclass
class ProvisionState:
    """Everything teardown needs; serialized to data/state-<runid>.json."""
    mechanism: str
    endpoints: dict[str, str]          # label -> endpoint id
    pods: dict[str, str] = field(default_factory=dict)     # shim only
    volumes: dict[str, str] = field(default_factory=dict)  # volume id(s)
    metrics_urls: dict[str, str] = field(default_factory=dict)  # scrape targets

class Mechanism(Protocol):
    name: str
    def provision(self, config, runid: str) -> ProvisionState: ...
    def worker_spec(self, config) -> WorkerSpec: ...
    def teardown(self, state: ProvisionState) -> None: ...
    def report_sections(self, jobs: list, metrics_rows: list) -> list[str]: ...
    def has_metrics(self) -> bool: ...   # gates harvest/scrape
```

- `shim.report_sections` returns the peering-payoff + per-pod hit-rate blocks
  (moved verbatim out of today's `report.py`). `volumecache`/`modelstore` return
  `[]`. `baseline` is not a full mechanism (see below).
- `shim.has_metrics()` is `True`; the others `False`, so `harvest/scrape.py` runs
  only for the shim.

### Baseline / control (`mechanisms/baseline.py`)

The baseline is provisioned by the **shared core for every run**, independent of
the mechanism under test: one plain Flash CPU endpoint whose handler does a naive
`hf_hub_download` straight from HF — no `HF_ENDPOINT`, no `VolumeCache`, no cached
model. The driver measures a `baseline` phase on this control endpoint for the
same models the mechanism run uses. This is the shared yardstick.

## Per-mechanism mechanics

### shim (ported)

- **provision:** 3 peered cache pods + Flash endpoints with
  `HF_ENDPOINT=<pod addr>` (today's `provision/up.py` logic, moved behind
  `shim.provision`). `network_volume_gb=None`. `has_metrics()=True`.
- **worker:** `hf_hub_download` through the shim (today's `worker/flash_app.py`
  branch). Reports `download_s`.
- **warm phase:** pull through a warm pod (LAN/peer hit), as today.
- **report_sections:** peering payoff + per-pod hit rate (needs `scrape`).

### volumecache (new)

- **provision:** Flash CPU endpoint(s) with a network volume attached at
  `/runpod-volume` (`network_volume_gb` from `[volumecache]` config). Provisioning
  mechanics confirmed by the spike. `has_metrics()=False`.
- **worker:** wraps the download in
  `with VolumeCache(dirs=[HF_CACHE_DIR]): hf_hub_download(...)`. On a populated
  volume, `hydrate()` restores the files and the HF download is a no-op. Reports
  `hydrate_s` and `download_s` separately.
- **driver sequence per model:** `populate` (cold — mirror empty, downloads from
  HF, `sync()` writes to volume) → `warm` burst (mirror populated — `hydrate()`
  restores, zero HF bytes).
- **warm metric:** driver wall-time of the warm burst vs the baseline.

### modelstore (new)

- **provision:** one Flash CPU endpoint **per model** (platform limit: one cached
  model per endpoint) with the model declared as cached. Whether this is
  automatable is a spike output; if console-only, the design falls back to a
  documented manual create step + harness-driven run/teardown (see Risks).
  `has_metrics()=False`.
- **worker:** resolves the local path
  `/runpod-volume/huggingface-cache/hub/models--{org}--{name}/snapshots/{hash}/`,
  asserts the model is present with expected size (and sha where available), and
  reports `local_read_s`. No HF download in the warm path.
- **warm metric:** driver end-to-end cold-start on a scaled-from-zero worker
  (submit → handler confirms model ready) vs the baseline. This captures the
  platform's DC-staging benefit, which is the point of Model Store.

## Shared timing schema

Every job result records the same shape (produced by `worker/timing.py`,
persisted by `drive/run.py` into `data/jobs-<runid>.jsonl`):

```python
{
    "mechanism": "shim" | "volumecache" | "modelstore" | "baseline",
    "phase": "baseline" | "populate" | "warm",
    "model": "org/name@rev",
    "wall_seconds": float,          # driver-observed, submit -> ready
    "bytes": int,
    "breakdown": {                  # handler-reported, mechanism-specific keys
        "download_s": float | None,
        "hydrate_s": float | None,
        "local_read_s": float | None,
    },
    "worker_cold": bool,            # first invocation on this worker (dep install paid)
    "ok": bool,
}
```

This schema is the comparison contract. The deferred roll-up reads
`data/jobs-*.jsonl` across mechanism runs and joins on `model` + `phase`.

## Report

`harvest/report.py` keeps a **shared timing core**:

- **Headline:** baseline→warm speedup (`baseline_wall / warm_wall`), jobs OK,
  bytes served.
- **Latency by phase:** table over `baseline` / `populate` / `warm`.
- **Cold-start vs steady-state:** the existing `coldstart()` section.

Then it appends `mechanism.report_sections(jobs, metrics_rows)` — non-empty only
for the shim. Output path becomes `data/report-<mechanism>-<runid>.md` so the
three mechanisms' reports coexist. Plots follow the same naming.

## Config

`config.toml` gains a top-level `mechanism` key and per-mechanism subtables;
shared keys (`dc`, `worker_cpu`, `models`, `container_disk_gb`,
`scrape_interval_s`, `job_timeout_s`, `burst`) stay top-level.

```toml
mechanism = "volumecache"        # shim | volumecache | modelstore

models = ["org/a@main", "org/b@main", "org/c@main"]
# ... shared keys as today ...

[shim]                           # only read when mechanism = "shim"
max_pods = 3
[shim.overlap]
A = ["org/a@main", "org/b@main"]

[volumecache]                    # only read when mechanism = "volumecache"
volume_gb = 50

[modelstore]                     # only read when mechanism = "modelstore"
# one endpoint per model; endpoint ids filled in by provision (or manual step)
```

`config.py` validates only the subtable for the selected mechanism, and always
validates the shared keys + the presence of a baseline-capable worker flavor.

## Ops / Makefile

The existing flow (`up` → `drive` → `scrape` → `report` → `down`, plus the
`demo-up` / `demo-run` orchestration with the mandatory teardown `trap`) is
preserved and parametrized by `MECHANISM`:

```
make -C runpod_testbed up      MECHANISM=volumecache
make -C runpod_testbed drive   RUNID=<id>
make -C runpod_testbed report  RUNID=<id>
make -C runpod_testbed down    RUNID=<id>
```

- `scrape` is a no-op (skipped) when `mechanism.has_metrics()` is `False`.
- Teardown safety is unchanged: `down` is mandatory, `demo-run` tears down on
  `EXIT` even on failure, and `provision.down` best-effort-removes endpoints,
  pods, and any network volumes recorded in `ProvisionState`.
- Secrets: `RUNPOD_API_KEY` + `HF_TOKEN` always; `SHIM_AUTH_TOKEN` only for the
  shim mechanism.

## Testing

- Reuse the `tests/` pytest layout; all tests are **no-spend** (mock the Runpod /
  Flash / runpod-python SDKs).
- Per-mechanism: `provision` builds the right `ProvisionState`/`WorkerSpec`;
  `worker` selects the right branch; `report_sections` returns the expected blocks.
- A **protocol-conformance test** that every registered mechanism implements the
  `Mechanism` interface and round-trips `ProvisionState` through JSON.
- `report.py` timing-core tests over the shared schema (headline, latency-by-phase,
  cold-start), including a mixed-mechanism fixture to lock the schema.
- Shim port is guarded by the existing shim tests, which must stay green
  unchanged (behavior-preserving refactor).

## Risks & mitigations

- **Provisioning automation unknown (both mechanisms).** Phase 0 spike resolves
  it. If Model Store is console-only, `modelstore.provision` degrades to
  validating pre-created endpoints from `[modelstore]` config + a documented
  manual setup step; the harness still drives + tears down what it owns.
- **Shim-port regression on the demo-critical path.** The port is behavior-
  preserving and gated by the existing shim tests; do it as its own phase with a
  green-tests DoD before building new mechanisms.
- **Model Store staging happens outside the handler.** Addressed by the
  driver-observed end-to-end yardstick; the handler only asserts model presence.
- **VolumeCache namespace collisions across runs.** Use a run-scoped `namespace`
  (default is `RUNPOD_ENDPOINT_ID`, which is per-endpoint and already isolated);
  teardown removes the volume so a stale mirror can't warm a later run falsely.
- **Cost.** CPU-only + mandatory teardown + baseline = one extra WAN pull per
  model per run; documented in the README like the shim testbed's download-cost
  note.

## Phasing

- **Phase 0 — spike:** probe Flash SDK / runpod-python / GraphQL for (a) attaching
  a network volume to an endpoint and (b) declaring a cached model on an endpoint.
  Output: which provisioning is automatable → finalizes `provision/`. Throwaway
  code; findings recorded back into this spec.
- **Phase 1 — abstraction + shim port + baseline:** extract `Mechanism`, move the
  shim's provision/worker/report bits behind it (tests green), add the shared
  baseline/control endpoint and the shared timing schema to `drive`/`report`.
- **Phase 2 — VolumeCache mechanism.**
- **Phase 3 — Model Store mechanism.**
- **Deferred — comparison roll-up** over `data/jobs-*.jsonl`.

## Spike findings (Phase 0) — confirmed live 2026-09-25

The Phase 0 spike was run against a live account during the first end-to-end
exercise. Results:

- **(a) Network-volume attach — automatable.** Flash's
  `Endpoint(volume=NetworkVolume(name, size, datacenter))` creates and attaches
  the volume at deploy; a volumecache run provisioned + attached + tore down a
  10 GB volume cleanly. The Runpod REST API (`rest.runpod.io/v1/networkvolumes`)
  is usable for list/`DELETE` **but requires a `User-Agent` header** — without
  one, Cloudflare returns `403` (error 1010). `DELETE /v1/networkvolumes/{id}`
  returns 200/204; the GraphQL `deleteNetworkVolume(input:{id})` mutation also
  works. This is why `provision/volumes.py` now sends a User-Agent.

- **(b) Model Store cached model — automatable via an UNDOCUMENTED GraphQL
  field.** It is absent from every *public* surface: the full REST OpenAPI
  (`Runpod API 0.1.0`, 23 paths) has no `model`/`cache` field, a live endpoint
  object exposes none, neither `runpod-python` nor `runpod-flash` exposes one,
  and GraphQL introspection is disabled. **But** the console's own JS uses a
  `modelReferences` field on the `saveEndpoint` mutation, and it works with a
  plain API key (verified live: `myself { endpoints { modelReferences } }` reads
  it; `saveEndpoint(input: EndpointInput!)` accepts it). Caveats: `saveEndpoint`
  is an UPSERT — you must read the endpoint's full current config (via
  `myself { endpoints }`; there is no top-level `endpoint(id)` query) and resend
  all of it plus `modelReferences`, and re-carry `modelReferences` on every
  later save or it is dropped. `modelReferences` is `["org/name[:rev]"]`
  lowercased; the API normalizes to a HF URL, so compare loosely. The endpoint
  must NOT also have a network volume (both mount `/runpod-volume`). Staging
  shows up as the platform's `delayTime` (worker held until a host has the
  model), not handler time. **`ModelStoreMechanism.declare_cached_models` now
  performs this GraphQL declaration** (`provision/modelstore_api.py`), falling
  back to printing the manual console step if the call fails. The reuse path
  (pre-created endpoints) remains available. *(Credit: reverse-engineered by a
  sibling benchmarking session; not an officially supported API — it may change.)*

- **(c) VolumeCache availability on the worker.** The Flash base image ships a
  `runpod` that predates `runpod.serverless.VolumeCache`, and `WORKER_DEPS` does
  not override base packages. The worker therefore force-upgrades `runpod>=1.12.0`
  at first invocation (for the volumecache downloader only) and drops the
  pre-imported `runpod.serverless` from `sys.modules` before the lazy
  `VolumeCache` import. With this, volumecache runs end-to-end
  (baseline→warm ≈ 2.7× on the tiny-model set).

- **(d) Model Store staging is GPU-only — the CPU testbed cannot measure it.**
  Declaration (b) succeeds on CPU endpoints (`modelReferences` set and normalized
  to HF URLs), but the platform never stages the files to a CPU worker. Confirmed
  live across multiple runs, including benchmark's exact working GPU pattern
  (declare right after deploy, before any worker) and a forced-cold worker
  (`workersMax 0→1` with `modelReferences` re-carried): every CPU worker reported
  `no models--<org>--<name> dir under /runpod-volume/huggingface-cache/hub`, so
  zero Model Store warm jobs completed. This matches the platform's Global-Volume
  restriction ("CPU endpoints don't support them"). **Conclusion:** the
  `modelstore` mechanism's declaration + worker code are correct and shipped, but
  a real Model Store *measurement* requires GPU endpoints, which is out of scope
  for this CPU download-timing testbed. shim (8.2×) and volumecache (2.7×) are the
  two mechanisms this testbed can measure on CPU.
