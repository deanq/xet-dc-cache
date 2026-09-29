# Network Transport Feasibility — Implementation Plan

> Spike-turned-implementation. The feasibility study (QUIC no-go; streaming
> client↔shim is a real win) is the design. This plan lands the measurable
> parts and records the numbers.

**Goal:** Land the streaming cache-hit optimization behind a toggle, add the
metrics needed to measure it, run the two live confirmations (at-scale RSS,
netem TCP-knobs), and commit a findings doc.

**Architecture:** Minimal, reversible shim changes (one env toggle, one gauge),
a testbed A/B seam, and two self-contained live measurements. No new deps. QUIC
is explicitly NOT built.

**Tech Stack:** Go (shim), Python (runpod_testbed), Runpod Flash + CPU pods.

## Global Constraints

- No new third-party Go deps (shim stays dep-light: only `x/sync`).
- `STREAM_CACHE_HITS` defaults **off** so the A/B is clean; flip default only
  after at-scale confirmation.
- On-disk cache format and all invariants in CLAUDE.md are untouched.
- Streaming applies to the **disk-HIT path only** (a complete cached file is
  safe to `io.Copy`); the MISS/tee path is explicitly out of scope.
- Every live run tears down (MANDATORY); verify no lingering spend after.
- Never touch benchmark peer's resources (`hfbench-*`).

---

### Task 1: Heap-inuse gauge (measurement prerequisite)

**Files:**
- Modify: `shim-go/prometheus.go` (add `xet_heap_inuse_bytes` gauge)
- Test: `shim-go/prometheus_test.go`

**Interfaces:**
- Produces: a `xet_heap_inuse_bytes` gauge sourced from
  `runtime.ReadMemStats().HeapInuse`, scraped per request so a burst's peak is
  visible to `harvest/scrape.py`.

Steps: write failing test asserting the gauge appears in `prometheusText()` →
add `runtime.ReadMemStats` read + `promMetric(&b, "xet_heap_inuse_bytes",
"gauge", ...)` → pass → commit.

### Task 2: Streaming disk-hit path (the optimization)

**Files:**
- Modify: `shim-go/xorb.go` (`getXorb` hit branch; add `writeXorbStream`)
- Modify: `shim-go/server.go` (`Server.streamHits bool`)
- Modify: `shim-go/main.go` (read `STREAM_CACHE_HITS`, default false)
- Test: `shim-go/xorb_test.go`

**Interfaces:**
- Consumes: `Server.streamHits`, `cachePath`, `contentRangeForHit`.
- Produces: identical response (206, `X-Cache: HIT`, `Content-Range`,
  `Accept-Ranges`, `Content-Type`, body bytes) whether buffered or streamed.

Behavior when `streamHits`: `os.Open(path)` → set headers → `WriteHeader(206)`
→ `Flush()` if supported → `io.Copy(w, f)`. Keep `lru.Touch`, `hits`,
`served_bytes`, `Observe("hit", …)` bookkeeping identical.

Steps: table-driven failing test (both modes → byte-identical body + headers for
a seeded cache file) → implement toggle + `writeXorbStream` → pass → `go test
-race ./...` → commit.

### Task 3: Testbed A/B seam + heap scrape

**Files:**
- Modify: `runpod_testbed/mechanisms/shim.py` (`_pod_env` passes
  `STREAM_CACHE_HITS` from config/env)
- Modify: `runpod_testbed/harvest/scrape.py` + `report.py` (capture/report
  `xet_heap_inuse_bytes` peak)
- Modify: `runpod_testbed/config.example.toml` (document the knob)
- Test: `runpod_testbed/tests/` (shim `_pod_env` carries the flag; scrape parses
  the gauge)

Steps: failing tests → thread the env + parse the gauge → pass → commit.

---

## Orchestration (controller-driven, not subagent tasks)

1. **Build + push image** (user runs `! docker push` — outward-facing).
2. **At-scale confirm:** two shim runs (stream off/on) with a large model;
   scrape peak `xet_heap_inuse_bytes` under burst; compare. Teardown.
3. **netem TCP-knobs:** cheap Linux CPU pod; self-contained benchmark injecting
   RTT+loss (needs NET_ADMIN/sysctl — if the container denies it, that denial IS
   the finding: tuning needs host access). H1 throughput with/without larger
   socket buffers + BBR. Teardown.
4. **Findings doc:** `docs/network-transport-feasibility.md` — QUIC no-go, the
   local streaming numbers, at-scale RSS confirmation, netem numbers, and the
   TCP-level recommendations. Link from README.

## Self-Review

- Coverage: streaming (T2), its measurement (T1, T3), at-scale + netem (orch),
  doc (orch). ✓
- No placeholders; exact metric names and header set specified. ✓
- Types consistent: `streamHits` defined T2/server.go, read T2/main.go,
  surfaced T3/shim.py. ✓
