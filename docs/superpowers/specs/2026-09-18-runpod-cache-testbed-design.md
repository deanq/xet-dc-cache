# Runpod Cache Testbed — Design

**Date:** 2026-09-18
**Status:** Design approved; ready for implementation planning.
**Owner:** dean.quinanola@runpod.io

## Goal

Stand up a repeatable **staging / reproduction environment on Runpod** that deploys
the xet-dc-cache shim onto pods (each pod = one "DC" cache), points real Runpod
Serverless endpoints at those pods, drives a realistic multi-model download
workload, and harvests both the pod side and the endpoint/user side into a
shareable report — so we can grow real data on how well DC-local caching (and
Tier 1.5 peering) performs, and where it doesn't.

## What we are measuring (all in scope)

- **Cold-start download tax** — wall-clock model-download seconds a serverless
  worker experiences, cold vs. warm vs. peer-served (the "user ending level").
- **WAN bytes / hit rate** — bytes served from cache vs. fetched upstream, and
  hit rate, across a realistic scale-up (the "pod level").
- **Cross-pod peering** — peer hit rate, `peer_bytes` vs `wan_bytes`, hedge
  win/waste ratios, when pods pull overlapping models from each other.
- **E2E proof** — stock-style serverless workers pulling real models through cache
  pods on Runpod, end to end, repeatably.

## Settled decisions (from brainstorming)

| Decision | Choice | Rationale |
|---|---|---|
| Networking | **Public TCP port** | Works anywhere, low infra. Accepts plaintext + HF-token exposure over the public internet — mitigated with a throwaway/scoped HF token. `PUBLIC_BASE` must be the pod's **external** mapped `ip:port`. |
| Fleet topology | **3 cache pods, full-mesh peering** | Each pod = a "DC" with its own worker group; matches the existing 3-node e2e. Richest peering dynamics. |
| Workload | **Multi-model mix with deliberate overlap** | Overlapping models across endpoints force cross-pod peer hits (A={X,Y}, B={Y,Z}, C={Z,X}). |
| Serverless surface | **3 real Serverless endpoints, one per pod** | Satisfies "Service and points"; each worker's `HF_ENDPOINT` baked to its local pod. |
| Worker fidelity | **Approach A: purpose-built download-timer handler on CPU workers** | Isolates the WAN download tax (the only cold-start component the shim moves); cheap enough to sweep the model mix. GPU/vLLM is a later swap on identical endpoint wiring. |
| Orchestration | **Version-controlled scripts** (`runpodctl` + GraphQL API) | Repeatable, lives in the repo. |
| Reporting | **Pull-to-parquet + Python report** | Matches `study/` house style; produces a shareable artifact, not a live dashboard. |

## Non-goals

- No changes to `shim-go/` (the cache binary is used as-is via `deploy/Dockerfile`).
- No GPU workers, no vLLM model-load timing (cache-invariant; a later fidelity swap).
- No live Prometheus/Grafana stack (chose the parquet+report artifact instead).
- Not re-testing the shim or peering engine — `make test-e2e` and `acceptance.py`
  already cover that. The testbed tests only its own glue.
- Not deciding production fleet topology — that is the `deployment-readiness-handoff.md`
  Step 2/3 locality question. This is a measurement testbed, not a rollout.

## Architecture

A self-contained subsystem in a new top-level `runpod-testbed/` directory. It
provisions the fleet, drives the workload, harvests both data sources, and
reports. The cache image is the **existing** `deploy/Dockerfile`, unchanged.

```
runpod-testbed/
  README.md                 # run it end-to-end; cost + teardown + token warnings
  config.example.toml       # fleet size, DC, registry, models, overlap matrix, burst, budget
  provision/
    fleet.py                # thin runpodctl + GraphQL client (create/get/terminate pods+endpoints)
    up.py                   # create 3 pods -> discover external addrs -> create 3 endpoints
    down.py                 # idempotent, fail-secure teardown (endpoints + pods)
    entrypoint.sh           # self-configuring cache-pod entrypoint (see below)
  worker/
    Dockerfile              # CPU: python + huggingface_hub + runpod SDK
    handler.py              # download-timer handler (Approach A)
  drive/
    run.py                  # submit the multi-model/overlap job matrix; record per-job JSON
  harvest/
    scrape.py               # poll each pod /metrics/prometheus at interval -> parquet
    report.py               # join pod-level + endpoint-level -> summary stats + plots
  data/                     # git-ignored: state, jobs, metrics, report, plots
```

### The `PEERS` / `PUBLIC_BASE` mutual dependency (load-bearing)

Both `PUBLIC_BASE` and `PEERS` are baked into the URLs the shim hands to clients,
but a pod's external `ip:port` only exists **after** Runpod schedules it. Solution:
a self-configuring cache-pod entrypoint (`entrypoint.sh`), baked into a testbed
image built `FROM` the existing shim image (or mounted at pod create). Given a
Runpod API key + the three pod IDs at creation time, it:

1. Polls the Runpod API until all three pods' external TCP addresses resolve.
2. Assembles `PUBLIC_BASE` (its own external addr) and `PEERS` (the other two),
   plus the shared `SHIM_AUTH_TOKEN` and cache/budget env.
3. `exec`s `xetcache`.

This makes the fleet self-forming and the run idempotent, and keeps the shim
binary untouched. `up.py` passes the three pod IDs to each pod as creation-time
env once all three `create` calls return (IDs exist immediately; addresses do not).

### Run lifecycle

```
up.py:
  1. push worker image if changed; push cache image (existing deploy/Dockerfile) once
  2. create 3 CPU cache pods (fleet tag, TCP 8000 exposed, volume > Σ model sizes),
     passing each: the 3 pod IDs, API key, shared SHIM_AUTH_TOKEN, cache/budget env
  3. entrypoint on each pod self-resolves addrs -> PUBLIC_BASE + PEERS -> exec xetcache
  4. poll each pod public /healthz until green (fail fast if an external addr is unreachable)
  5. create 3 CPU serverless endpoints, one per pod:
       env HF_ENDPOINT=http://<pod-ext-ip:port>, throwaway HF token, min=0/max=burst, queue-based
  6. write everything created to data/state-<runid>.json

drive/run.py:
  - read overlap matrix from config: A={X,Y}, B={Y,Z}, C={Z,X}
  - phase COLD:       one job per (endpoint, model) — first touch, expect MISS/peer
  - phase WARM-BURST: N concurrent jobs per (endpoint, model) — expect local HITs
  - each job returns {model, bytes, wall_seconds, worker_id, first_byte_ms}
  - append every job (with submit/return timestamps) to data/jobs-<runid>.jsonl

harvest/scrape.py (runs alongside drive):
  - every INTERVAL, GET each pod /metrics/prometheus -> rows (ts, pod, series, value)
  - append to data/pod-metrics-<runid>.parquet

down.py: terminate endpoints then pods from state file; safe to re-run.
```

### Data model and join

Two datasets, joined on `(pod, time-window)`:

- **Endpoint-level** (`jobs-<runid>.jsonl`): user-facing wall-clock download seconds
  and bytes per job — cold vs warm vs peer. What a user feels.
- **Pod-level** (`pod-metrics-<runid>.parquet`): the *why* — HIT/MISS/peer split,
  `wan_bytes` vs `peer_bytes`, hedge win/waste — as counter time series.

The handler reports wall-clock + bytes only; cache classification comes from
pod-metric **deltas** over each job's window. We deliberately do **not** thread
per-file `X-Cache` back through `huggingface_hub` — it batches many requests and
that seam is unreliable (see the acceptance-script note in `CLAUDE.md`).

## Components

### `worker/handler.py` (Approach A)

A Runpod Serverless handler. Job input: `{"models": ["org/repo@rev", ...],
"path": optional}`. For each model: `snapshot_download` (or `hf_hub_download` for a
single path) through the cache — `HF_ENDPOINT` is set in endpoint env — timing the
transfer with a monotonic clock; measures `first_byte_ms` and total `wall_seconds`;
sums bytes on disk. Returns `{model, bytes, wall_seconds, first_byte_ms, worker_id}`
per model. CPU-only image: `huggingface_hub` + `runpod` SDK, no torch. Leaves
`HF_HUB_DISABLE_XET` unset so the Xet path (the thing under test) is exercised.

### `provision/fleet.py`

Thin client wrapping `runpodctl` (pod/endpoint create/terminate where ergonomic)
and the GraphQL API (reading a pod's mapped external TCP port; endpoint creation
with env — the parts runpodctl doesn't expose cleanly). One module so `up.py` /
`down.py` read as intent, not API mechanics.

### `harvest/report.py`

Consumes both files, emits `data/report-<runid>.md` + plots:

- **User-ending headline:** download wall-seconds distribution per phase (cold /
  warm-burst / peer) — the cold-start tax and how much the cache removes.
- **Pod-level:** per-pod hit rate, `effective_hit_rate` (counts peer hits — the
  metric `deploy/README.md` recommends when peering is on), `wan_bytes_saved`.
- **Peering payoff:** fleet `xet_peer_bytes_total` vs `xet_wan_bytes_total` (the
  go/no-go metric named in `CLAUDE.md`); hedge win ratio
  `peer_hedge_peer_won / peer_hedge_fired`; `peer_bytes_wasted` (insurance cost).
- **Cost:** wall-clock × CPU-pod + endpoint pricing, so each run's dollar cost is
  in the artifact.

Parquet + matplotlib, `study/` house style.

## Error handling, cost & safety

- **Fail-secure, idempotent teardown:** `up.py` records every created resource to
  `data/state-<runid>.json`; `down.py` terminates by ID, tolerating already-gone
  resources. `up.py` defaults to teardown-on-error so a half-provisioned fleet
  never silently bills.
- **Budget guard:** `config.toml` carries `max_pods` / `max_burst` ceilings;
  `up.py` refuses to exceed them. CPU-only everywhere (no GPU spend).
- **Token safety:** the throwaway HF token lives only in endpoint env + a
  git-ignored local config, never in the repo. README states the public-TCP
  plaintext-token exposure plainly and recommends a scoped/throwaway token.
- **Port mapping:** `PUBLIC_BASE` uses the discovered **external** `ip:port`;
  `up.py` asserts each pod's public `/healthz` is reachable before creating
  endpoints (fail fast on an unreachable address).

## Testing

- **Unit (`uv run`, no Runpod):** `scrape.py`'s Prometheus-text parser and
  `report.py`'s join/aggregation, against captured `/metrics/prometheus` fixtures.
  This is the glue logic that carries the data's correctness.
- **Dry-run mode:** `drive/run.py --dry-run <cache-url>` drives a stock
  `hf_hub_download` against one pod directly (no serverless) to validate wiring +
  the harvester before spending on the full endpoint fleet.
- **The shim + peering itself is already covered** by `make test-e2e` and
  `acceptance.py`; the testbed does not re-test it. Provisioning is validated by a
  real `up.py` → `--dry-run` → `down.py` cycle against Runpod.

## Open questions for planning

- Exact GraphQL mutations/fields for (a) reading a pod's mapped external TCP port
  and (b) creating a CPU serverless endpoint with env — confirm against the
  current Runpod API during Task 1 (`fleet.py`), since these are the least-certain
  integration points.
- Cache-pod storage: container volume vs. network volume, sized to Σ model sizes
  of the chosen mix. Default to a container volume for simplicity unless a run
  needs persistence across teardown.
- Concrete model set for X/Y/Z (real Xet repos, sizes) — a planning input, driven
  by desired Σ-size vs. cost.
```

