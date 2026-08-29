# Placement-Event Instrumentation

**Purpose:** define the worker-start event the platform must emit so the locality
study (`placement_locality_sim.py`) runs on real data instead of the demo fixture.
Net-new — this is the data contract, not a description of something that exists.

**Confidence key:** ✅ derivable from existing control-plane state · ⚠️ needs a new hook.

---

## 1. What we're capturing

One event per **serverless worker start** — i.e. each time the scaler brings up a
worker (a Pod) that will load a model. That event is the unit the sim replays:
"model M started on host H at time T." Everything the study needs (hit rate,
stickiness, cache sizing, WAN saved) is derivable from a stream of these.

We are **not** instrumenting the data path (no bytes intercepted). This is
control-plane + lifecycle telemetry.

---

## 2. Where it's emitted

Two emission points, because no single actor knows all fields:

| Field group | Emitter | When | Why there |
|---|---|---|---|
| placement facts (host, gpu, dc, endpoint, model) | **control plane / scheduler** ✅ | at worker placement | it *decides* the host and knows the endpoint's model config |
| pull cost (`cold_pull_bytes`, `pull_duration_s`) | **worker** (or the cache shim, once built) ⚠️ | at model-load completion | only the worker observes the actual download |

Correlate the two with a `worker_id`. The placement row is the primary record;
the pull-cost row enriches it (LEFT JOIN, §5). A worker that reused a warm local
cache emits `cold_pull_bytes = 0` — that's signal, not missing data.

**Recommended v0:** emit the placement row only. It already yields hit rate and
stickiness. Pull-cost enrichment (v1) turns hit rate into "GiB and hours saved" —
valuable but not blocking.

---

## 3. Event schema (field-by-field)

Matches `REQUIRED_COLUMNS` / optionals in `placement_locality_sim.py`.

| Field | Type | Source | Notes |
|---|---|---|---|
| `worker_id` | string | control plane ✅ | join key; not consumed by the sim |
| `ts` | int (epoch s) or timestamp | placement time ✅ | order key; sim sorts on it |
| `endpoint_id` | string | control plane ✅ | |
| `repo_id` | string | endpoint template / env ✅ | the HF model id, e.g. `org/model` |
| `revision` | string | **resolve to commit SHA** ⚠️ | **critical** — see §4 |
| `host_id` | string | scheduler ✅ | the machine the worker landed on |
| `gpu_type` | string | machine spec ✅ | segments disjoint host pools (§9 of measurement doc) |
| `datacenter` | string | machine spec ✅ | region code, e.g. `US-CA-1` |
| `rack_id` | string | machine topology ⚠️ | needed only to evaluate rack-scope caching |
| `model_size_bytes` | int | resolve / repo metadata ✅ | sum of weight-file sizes at that revision |
| `cold_pull_bytes` | int | worker/shim ⚠️ | 0 if served from warm local cache |
| `pull_duration_s` | float | worker/shim ⚠️ | cold-start download seconds |

---

## 4. The one field that must be right: `revision`

`(repo_id, revision)` is the cache identity. If workers pull `main` and you log
the literal string `"main"`, two different commits collapse into one key and the
sim **overstates** hit rate (a silent revision bump looks like a hit).

**Rule:** resolve `revision` to the **immutable commit SHA** at pull time — the
same SHA the resolve call uses — and log that, not the branch alias. ✅ (the SHA
is already in the resolve response; capture it there.)

For Tier 2, the xorb map is keyed on the same `(repo_id, revision)`, so this must
match across both datasets or the join silently drops rows (`unmapped_starts`).

---

## 5. Sink and extract

Emit as a structured event (log line / event bus) → land in a queryable table →
export Parquet for the sim. Warehouse-agnostic; adjust names to your stack.

```sql
CREATE TABLE worker_starts (
    worker_id         STRING,
    ts                TIMESTAMP,
    endpoint_id       STRING,
    repo_id           STRING,
    revision          STRING,       -- commit SHA, never "main" (§4)
    host_id           STRING,
    gpu_type          STRING,
    datacenter        STRING,
    rack_id           STRING,
    model_size_bytes  BIGINT
);

-- optional enrichment table (v1)
CREATE TABLE worker_pull_cost (
    worker_id         STRING,
    bytes_downloaded  BIGINT,       -- 0 = served from warm local cache
    duration_seconds  DOUBLE
);
```

Export (feeds `events.parquet` — same query as measurement doc §3, with the join):

```sql
SELECT
    ws.ts, ws.endpoint_id, ws.repo_id, ws.revision, ws.host_id,
    ws.gpu_type, ws.datacenter, ws.rack_id, ws.model_size_bytes,
    pc.bytes_downloaded AS cold_pull_bytes,
    pc.duration_seconds AS pull_duration_s
FROM worker_starts ws
LEFT JOIN worker_pull_cost pc USING (worker_id)
WHERE ws.ts >= NOW() - INTERVAL '30' DAY      -- ≥ longest expected reuse interval
ORDER BY ws.ts;
```

---

## 6. Building the Tier 2 xorb map (`xorbs.parquet`)

Separate extract, keyed `(repo_id, revision, xorb_hash, xorb_size_bytes)`:

- **Once the CAS relay exists** (design §4): it parses reconstruction manifests
  already — have it emit one row per `(file → xorb, size)` it sees. Free.
- **Before the relay** (offline): for each `(repo_id, revision)` in the event log,
  call resolve + reconstruction, read `terms[]` / `xorbs`, and flatten to the
  four columns. This is the offline manifest-scraper — a small standalone job.

Keep revision as commit SHA here too (§4), or the Tier 2 join drops rows.

---

## 7. Rollout

1. **v0 — placement row only.** Scheduler emits the 9 ✅ fields at placement.
   Unlocks hit rate + stickiness_x → the *build/don't-build* and *scope* decisions.
2. **v1 — pull-cost enrichment.** Worker emits bytes/duration at load completion.
   Turns hit rate into WAN-GiB and cold-start-hours saved (the business case).
3. **v2 — xorb map.** Offline scraper now; CAS-relay-emitted later. Unlocks Tier 2.

v0 is the only prerequisite to start measuring. Everything after sharpens the number.

---

## 8. Gotchas

- **`revision = "main"`** — the §4 trap; the single most likely way this dataset
  lies. Resolve to SHA at capture.
- **Survivorship** — log *all* start attempts, including failed/retried cold pulls
  (the ones that hurt most). If the scheduler only logs successful placements, the
  worst cases are invisible.
- **Clock source** — `ts` must be a single monotonic control-plane clock, not
  per-host wall clocks (skew reorders events and corrupts the point-in-time
  baseline).
- **`repo_id` sensitivity** — private model names are user data; treat the event
  stream as internal telemetry with the usual access controls.
- **Retention ≥ reuse interval** — export window must exceed the longest model
  reuse gap, or long-lived models look colder than they are.
