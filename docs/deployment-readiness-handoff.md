# Handoff: deployment readiness — from "validated canary" to "DC production"

**Status:** the cache engine (`shim-go/`) is validated and production-quality; the
*deployment* is not yet. This doc is the ordered work plan to close that gap. Work
the steps top-to-bottom — each is independently shippable and later steps assume
earlier ones landed.

**Verdict (2026-08):** deployable today as a **single-host canary on a trusted LAN**;
**not** ready for a hardened multi-host DC rollout. The gaps are around the core
(packaging, topology, ops, security), not in it.

**Progress:** Step 1 ✅ (artifacts in `deploy/`; on-host runtime DoD pending a
linux host). Step 2 ⏸ parked — blocked on a real placement-log extract (fleet
data out of reach). Step 3 ⛔ gated by Step 2. Step 4 ✅ (Prometheus exposition,
concurrency cap, structured logs, optional auth, trust-boundary docs).
Step 5 ⏳ optional/demand-driven.

## What already works (validated, don't re-verify)

- Transparent Xet interception via `HF_ENDPOINT`; downloads through the shim are
  byte-identical to direct (real `hf-xet` client), MISS→HIT, cache survives restart.
- Tier 1 range cache: byte-capped LRU (`XORB_CACHE_MAX_GIB`), atomic writes,
  `singleflight` fan-out collapse, offline-serve on WAN blips. `go test -race` clean.
- `/healthz`, `/metrics` (JSON), env config, `make start/stop/status/logs/metrics`.
- Live acceptance: `cd shim-go && uv run acceptance.py`.

## The gap table (source of the ordered plan below)

| Gap | Why it matters | Effort |
|---|---|---|
| No Linux/container build | `make build` targets host arch (dev box = darwin/arm64); DC = linux/amd64. No Dockerfile / k8s manifest. | Small |
| No service supervision | `make start` is `nohup`, not systemd; crash = stays down; no graceful drain. | Small |
| Topology undecided | One process = one cache. Per-host vs shared rack/DC (SPOF + funnel + hop). No clustering/sharding. **The** unmade architectural decision. | Medium–Large |
| Locality study never run on real data | `study/` proves the method on a demo fixture only. No evidence the fleet's placement yields a worthwhile hit rate. This is the go/no-go gate. | Medium |
| LFS repos uncached | Only Xet is cached; non-Xet repos pass through at full WAN cost. Design ready in `lfs-support-handoff.md`. | ~2 days |
| Security posture | Plaintext HTTP, no shim auth, forwards client tokens to CAS/hub. OK inside trusted LAN only. | Varies |
| Observability | `/metrics` is JSON not Prometheus; stdlib logging; no tracing/alerting. | Small–Medium |
| Memory under load | Xorb ranges buffered in memory (≤64 MB each); many concurrent distinct misses spike RSS; no global concurrency cap. | Small–Medium |

## Ordered plan

### Step 1 — Linux build + service supervision (canary-unblocker) — NEXT

Goal: run the shim on one real DC host as a supervised service.

- Add a cross-compile target to the `Makefile`: `GOOS=linux GOARCH=amd64 go build
  -o xetcache-linux-amd64 .` (keep the existing host-arch `build`). Static-ish Go
  binary; no cgo in this codebase, so it's a clean single artifact.
- Add a `systemd` unit (e.g. `deploy/xet-dc-cache.service`): `Restart=always`,
  `EnvironmentFile` for the env vars (see `shim-go/main.go`), `WorkingDirectory` at
  the cache host, run as a non-root service user. Document install steps.
- Optional: a minimal `Dockerfile` (scratch/distroless + the linux binary) if the
  fleet is containerized; and a DaemonSet manifest if per-host (see Step 3).
- **`PUBLIC_BASE` must be the host's LAN-reachable address:port** — it's baked into
  the URLs handed to clients. Localhost default will not work for remote workers.
- DoD: binary runs under systemd on a linux DC host; survives a kill (auto-restart);
  a worker on that host with `HF_ENDPOINT` set downloads a model and `/metrics`
  shows a hit on the second pull.

### Step 2 — Run the locality study on real placement logs (the go/no-go gate)

- Extract a real `events.parquet` from scheduler/placement logs (schema + SQL in
  `study/placement-locality-measurement.md`; emission contract in
  `study/placement-event-instrumentation.md`). Replace the demo fixture.
- Run `cd study && uv run placement_locality_sim.py events.parquet --mode tier1`.
- Read `stickiness_x`: **>1** ⇒ the scheduler clusters workers, a host-local cache
  compounds it (proceed per-host). **≈1** ⇒ hits are pool-size luck; host-local
  won't pay — reconsider scope (rack/DC) or don't build fleet-wide.
- DoD: a decision, on real numbers, for cache **scope** (host / rack / DC) and
  per-node NVMe sizing (the `hit_rate` vs `cache_gib` knee). This decision gates
  Step 3.

### Step 3 — Topology / rollout (shaped by Step 2)

- **If per-host wins:** package as a DaemonSet (or per-host systemd) so each host
  runs its own shim + local NVMe cache; workers target `HF_ENDPOINT=localhost:PORT`.
  No clustering needed. Simplest, best locality.
- **If shared scope wins:** design a rack/DC-shared deployment — needs an HA story
  (≥2 instances behind a VIP), attention to the bandwidth funnel, and possibly
  cache sharding. Materially more work; only do it if Step 2 says host-local doesn't pay.
- DoD: rollout mechanism for the chosen scope; documented worker `HF_ENDPOINT` wiring.

### Step 4 — Production hardening (parallelizable once Step 3 lands)

- **Observability:** Prometheus exposition (wrap the existing counters), structured
  logs, basic alerts (hit_rate drop, disk pressure, upstream error rate).
- **Load safety:** a global in-flight/concurrency cap and per-request memory
  awareness (ranges are buffered; cap concurrent misses or stream if it becomes a
  problem). Re-check RSS on a >1 GB multi-xorb pull under fan-out.
- **Security:** decide TLS + auth posture for the shim (trusted-LAN-only may be
  acceptable; document the trust boundary — the shim sees client tokens).

### Step 5 — LFS caching (optional, demand-driven)

- Only if non-Xet repos matter for the fleet. Full design in
  `lfs-support-handoff.md` (already retargeted to the Go shim). ~1.5–2 days.

## Notes for whoever picks this up

- Settled dead-ends (don't rebuild): content-address verification isn't achievable;
  Tier 2 chunk coalescing has ~0 payoff. See `xet-cache-findings.md` and `CLAUDE.md`.
- Invariants that constrain any change to the hot path are in `CLAUDE.md`
  ("Invariants that will bite you if changed").
