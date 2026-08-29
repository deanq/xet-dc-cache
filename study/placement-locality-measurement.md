# Placement-Locality Measurement — Xet DC Cache

**Purpose:** Decide the *scope* of the Xet cache (host-local vs rack vs DC) and predict its hit rate **before** building anything. Companion to `../docs/xet-cache-shim-design.md` (§11 M1, §12 open items).

**Key property:** this is a **retrospective log-replay** study. No shim, no worker changes, nothing in the data path. It runs on data the scheduler already emits. Do this *before* M1 — it's the cheap de-risk.

---

## 1. The question that gates Tier 1

> When the serverless scaler starts a new worker for model *M*, how often does it land on a host that **already has M's xorbs cached locally**?

- **High** (workers cluster on a small candidate set) → host-local Tier 1 wins. Build M1 as designed.
- **Low** (workers scatter across a large pool) → host-local cache mostly misses; you need a **shared rack/DC-scoped** store, which changes C's placement (and makes Tier 2 whole-xorb the real payoff, not Tier 1).

One number decides the scope. Don't guess it — measure it.

---

## 2. The single decision it drives

| Measured host-local hit rate | Cache scope to build | Consequence |
|---|---|---|
| ≥ ~60% | **Host-local** (Tier 1 as-is) | Cheapest. Ship M1. |
| ~20–60% | **Rack / AZ-shared** store | One cache per rack; workers hit a LAN neighbor. Adds a hop, still << WAN. |
| < ~20% | **DC-shared** store, skip host-local | Locality isn't there; only content-addressed DC dedup (Tier 2) pays off. |

Thresholds are starting points — set the real cutoff against WAN-pull cost vs. added-hop latency (§7).

---

## 3. Data source — event log (already emitted)

One row per **worker-start** event. Pull from scheduler/placement logs; no new instrumentation needed for Phase 0.

```
worker_start {
  ts                # start timestamp
  endpoint_id
  repo_id           # HF model id      ── cache-key dimension
  revision          # commit/sha       ── cache-key dimension (revision bump = new xorbs)
  host_id           # machine it landed on
  gpu_type          # placement constraint
  datacenter        # placement constraint
  cold_pull_bytes   # WAN bytes on this start (0 if reused local) — if available
  pull_duration_s   # cold-start download time — if available
}
```

`(repo_id, revision)` is the cache identity. If you can't get exact revision, approximate with repo_id and note the over-count (a revision bump would falsely look like a hit).

`cold_pull_bytes` / `pull_duration_s` are optional but turn "hit rate" into "$ and seconds saved."

---

## 4. Metrics (all computable by SQL/pandas over the log)

1. **Host-local hit rate** (headline)
   For each start, was `(repo_id, revision)` served on the same `host_id` within the retention window *W*?
   `hits / total_starts`. Compute per gpu_type and per datacenter — it will vary.

2. **Host fan-out per model** = distinct `host_id` serving a given `(repo,revision)` over *W*.
   Low fan-out ⇒ clustering ⇒ host-local cache viable.

3. **Model reuse interval** = time between consecutive starts of the same `(repo,revision)` on the same host.
   Hit requires this < eviction TTL. Plot the distribution; the tail past TTL is your miss floor.

4. **Candidate-host-set size** per `(gpu_type, datacenter)` = the placement denominator.
   Big pool + scatter = low locality by construction.

5. **WAN saved** (if bytes present) = Σ `cold_pull_bytes` over hits. The business number.

---

## 5. Cache-sim replay (sweep the parameters you don't know yet)

Replay the event log through a **simulated per-host LRU** to get hit rate as a function of the two knobs you'd otherwise guess:

```
for scope in {host, rack, datacenter}:
  for cache_size in {sweep: 100GB … 4TB}:
    cache = {scope_key: LRU(cache_size)}      # keyed by (repo,revision) or by xorb hash for Tier 2
    for e in sorted(events, by=ts):
      key = scope_key(e)                        # host_id | rack_id | dc_id
      hit = cache[key].contains(e.model)
      cache[key].touch(e.model, size=e.model_size)
      record(scope, cache_size, hit)
```

Outputs:
- **hit rate vs. cache size** per scope → the working-set knee (where more disk stops helping).
- **hit rate vs. scope** → directly answers §2.
- For **Tier 2**, key the sim by **xorb hash** instead of `(repo,revision)` to measure cross-revision dedup (needs a repo→xorb map; if unavailable, approximate revision-bump savings by shared-file ratio between adjacent revisions).

TTL: model eviction either as pure LRU (size-bound) or add a wall-clock TTL to match a "cache worker recycled after N hours" policy — sweep both.

---

## 6. Baseline — is the locality *real* or just pool size?

Compare actual host-local hit rate to the **random-placement expectation**:

```
E[hit | random] ≈ (# workers of M currently live on a host) / (candidate_host_set_size)
```

- Actual ≫ random ⇒ scheduler already exhibits **stickiness** (affinity, warm-worker reuse). Host-local cache compounds it — strong signal to build Tier 1.
- Actual ≈ random ⇒ no stickiness; hit rate is just pool luck. Either add **placement affinity** (schedule scale-out toward hosts warm for that model) *or* go rack/DC scope.

This comparison is the honest gut-check: it separates "locality exists" from "we got lucky on a small pool."

---

## 7. Decision output

The study produces exactly three things:
1. **Scope** (host / rack / DC) — from §2 + §6.
2. **Cache size per node** — from the §5 knee.
3. **Predicted M1 ROI** — hit rate × avg cold-pull cost = WAN bytes & seconds saved/day.

If (3) doesn't clear the build cost, the answer is "don't build Tier 1; pursue placement affinity or Tier 2 first" — a valid and cheap outcome.

---

## 8. Phase 1 — live validation (after M1 ships, not before)

Phase 0 predicts; Phase 1 confirms. Once the shim exists, emit from the xorb store (§5 of design doc):
- hit/miss counter by `hash` and by `(repo,revision)`
- WAN bytes fetched vs. LAN bytes served
- p50/p99 cold-start with cache vs. the pre-cache baseline

Reconcile Phase 1 measured hit rate against the Phase 0 simulated prediction. Divergence = a wrong modeling assumption (usually TTL/eviction or revision granularity) — fix the model, it's your capacity-planning tool going forward.

---

## 9. Gotchas

- **Revision granularity.** Collapsing revisions inflates hit rate (a revision bump reuses *some* but not all xorbs). Keep revision in the key; use Tier-2 xorb-hash sim to recover the partial-reuse credit.
- **Cache-worker recycling.** If exited "cache workers" are torn down on a timer, that timer — not disk size — caps host-local hit rate. Model it as TTL in §5.
- **Survivorship in logs.** If logs only record *successful* starts, you miss ret/failed cold pulls (the ones that hurt most). Confirm the log captures all start attempts.
- **GPU-type fragmentation.** A model runnable on 3 GPU SKUs scatters across 3 disjoint host pools → lower locality than the aggregate suggests. Always segment by `gpu_type`.
- **Small-N models.** Rarely-started models never cluster; don't let their misses drag the headline — report hit rate weighted by start volume, not by model count.
