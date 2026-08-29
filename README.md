# Xet-Aware DC Cache

A datacenter-local caching layer that lets **stock HF/vLLM images pull Xet-backed
models at LAN speed** — no Dockerfile changes required. It buffers the WAN and kills
the repeated model-download tax that serverless scale-out pays on every cold start.

The fix lives **below the user's container** (env injection via `HF_ENDPOINT`), so
users who bring a pre-built vLLM image get it for free.

---

## Project layout

```
shim-go/     The cache service (Go). Hub shim + CAS relay + Tier 1 xorb range cache.
tools/       Offline analysis tools (Python): fsck.py, dedup_study.py.
xet_verify/  Rust pyo3 helper (pinned xet-core chunker) that fsck.py binds.
study/       Phase 1 feasibility study: "is host-local caching worth building?"
docs/        Design + reference prose (see below).
```

- `docs/xet-cache-shim-design.md` — build-ready design: the three components, the
  exact resolve/reconstruction transforms, auth lifecycle, failure modes.
- `docs/xet-cache-findings.md` — empirical results: whole-file dedup is the whole
  game; content-address verification is not achievable via the client API.
- `docs/lfs-support-handoff.md` — proposed follow-up: add git-LFS caching to the Go shim.
- `docs/superpowers/` — the spec + plan + acceptance notes for the Go rewrite.

---

## The cache service (`shim-go/`)

A single Go binary playing three roles: (A) Hub proxy that rewrites the
`xet-read-token` response — both the body `casUrl` and the `X-Xet-Cas-Url`
connection-info header current `huggingface_hub` reads — to point CAS traffic at
itself, (B) CAS reconstruction relay, (C) Tier 1 xorb range cache keyed by
`(hash, range)`. Transparent to stock HF/vLLM images via `HF_ENDPOINT`.

Optionally, a **Tier 1.5 cross-DC peer cache**: on a local miss a shim can pull a
warm range from a sibling shim over a private backbone before falling back to the
CDN — a best-effort accelerator that never fails a request the CDN would serve.
Off by default; enable with `PEERS` (see `CLAUDE.md` and `deploy/README.md`).

```bash
make build            # -> shim-go/xetcache
make run              # foreground on :8000 (Ctrl-C to stop)
make start / stop / status / logs / metrics
make test             # go test ./...
make test-e2e         # 3-node cross-DC peering e2e in Docker (needs network)
```

Point a worker at it: `export HF_ENDPOINT=http://<shim-host>:8000` (leave
`HF_HUB_DISABLE_XET` unset). Live end-to-end acceptance: `cd shim-go && uv run
acceptance.py`. Cross-DC peering end-to-end (3 peered containers vs. real HF,
asserting peer warm-hit + WAN fallback + peers-down resilience): `make test-e2e`
(harness in `deploy/e2e/`).

> The shim was originally prototyped in Python and rewritten to Go as a validated
> 1:1 parity port; recover the Python version from git history if ever needed. The
> offline tools stayed in Python (`tools/`) because they bind the Rust xet-core
> chunker, which the Go shim does not.

---

## The feasibility study (`study/`)

*Do this first for a greenfield deployment.* A retrospective log-replay study
(nothing in the data path) that answers **"is host-local caching even worth
building?"** — deciding the cache **scope** (host / rack / datacenter) and
predicting the hit rate before any engineering.

- `study/placement-locality-measurement.md` — the method + the SQL to extract a
  real event log. Read first.
- `study/placement_locality_sim.py` — the runnable sim: replays a worker-start
  event log through a simulated LRU cache; reports hit rate, random-placement
  baseline, and WAN saved.
- `study/placement-event-instrumentation.md` — the data contract for emitting a
  *real* `events.parquet` (replaces the demo fixture).

**The decision gate:** the sim tells you whether — and at what scope — to deploy
the shim. If locality isn't there, the honest outcome is "don't cache host-local,"
which the study surfaces cheaply.

### Quickstart

Dependencies are declared inline (PEP 723), so `uv run` handles the environment.
Run from `study/` (scripts read/write parquet in the working directory):

```bash
cd study
uv run make_sample_data.py                                   # DEMO fixture: events.parquet + xorbs.parquet
uv run placement_locality_sim.py events.parquet --mode tier1 --ttl-hours 24
uv run placement_locality_sim.py events.parquet --mode tier2 --xorb-map xorbs.parquet
```

`make_sample_data.py` is a **fixture generator for demoing the tool**, not real
data. Once the platform emits placement events, replace `events.parquet` with the
scheduler-log extract (schema + SQL in `placement-locality-measurement.md`); the
sim reads the identical schema. The `*.parquet` files are gitignored.

**Inputs** (CSV or Parquet):

- `events.parquet` — one row per worker-start:
  `ts, endpoint_id, repo_id, revision, host_id, gpu_type, datacenter,
  model_size_bytes, [cold_pull_bytes], [pull_duration_s], [rack_id]`.
  Pull from scheduler/placement logs — no new instrumentation needed.

- `xorbs.parquet` (Tier 2 sim only) — one row per xorb per revision:
  `repo_id, revision, xorb_hash, xorb_size_bytes`. Build a **real** one from HF:

  ```bash
  uv run scrape_xorb_map.py --events events.parquet     # distinct (repo,rev) from the log
  uv run scrape_xorb_map.py --repos org/model@<sha>     # or explicit
  uv run scrape_xorb_map.py --self-test                 # offline logic check
  ```

**Reading the output:**

- `hit_rate` vs `cache_gib` → the working-set knee = per-node NVMe sizing.
- `stickiness_x` (`hit_rate / random_baseline`) → **>1** means the scheduler
  already clusters workers and a cache compounds it. **≈1** means hits are just
  pool-size luck; the real lever is placement affinity, not a cache.
- Pick the smallest scope where `host` hit rate clears ~60%; else `rack`, else `datacenter`.

---

## Status

- **Implementation:** shipped as the Go shim (`shim-go/`) — Tier 1 range cache,
  validated live (byte-identical downloads, cache hits, restart-reuse) and
  race-tested. See `docs/superpowers/plans/notes-go-acceptance.md`.
- **Tier 1.5 cross-DC peering:** shipped (off by default via `PEERS`). Validated
  end-to-end across 3 peered containers against real HF — a cold node pulls a warm
  peer's bytes over the backbone (zero WAN, byte-identical), and falls back to the
  CDN seamlessly when no peer has the range or peers are down. Run `make test-e2e`.
  Design + plan in `docs/superpowers/`.
- **Tier 2 (chunk coalescing):** investigated and dropped — measured dedup payoff
  is ~0 for model serving (weights are immutable across revisions, ~0% shared
  chunks across fine-tunes). All real dedup is whole-file, which Tier 1 captures.
  See `docs/xet-cache-findings.md`.
- **Content-address verification:** found **not achievable** via the client API
  (`X-Xet-Hash` is a server-side HMAC; no reproducible anchor). `docs/xet-cache-findings.md`.
- **Next (optional):** git-LFS caching for un-migrated repos — `docs/lfs-support-handoff.md`.
