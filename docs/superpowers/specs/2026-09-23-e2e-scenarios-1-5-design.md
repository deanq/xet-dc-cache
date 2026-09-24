# E2E scenarios #1–5 — design

**Status:** approved 2026-09-23. Extends the shim's end-to-end coverage with
five new/expanded scenarios and refactors the local Docker harness
(`deploy/e2e/`) into a scenarios package.

## Motivation

The current e2e suite proves peering + cross-revision dedup on a **single LFS
file**. Several documented invariants and real-world usage patterns are not
exercised end-to-end:

- Full `snapshot_download` (mixed LFS + non-LFS files) — the `Content-Length`
  pass-through invariant in `CLAUDE.md` is a fixed regression with **no e2e
  guard**; the shim could silently serve big safetensors but break small
  companion files (`config.json`, `tokenizer.json`).
- Sharded models (multiple `.safetensors`) — concurrent xorb fetch + peering
  across many files, not one.
- Concurrent cold pulls — the `singleflight` invariant (collapse concurrent
  cold fetches for the same `(hash, range)`) is untested.
- Cache eviction under a byte budget (`XORB_CACHE_MAX_GIB`) + atomic-write
  no-false-HIT guarantee.
- Serverless cold-start penalty (worker boot + the hf_xet force-upgrade) is
  unmeasured, so the demo can't honestly separate first-job cost from steady
  state.

## Scope decisions (settled)

1. **#5 is instrumentation only** — measured on the next real `up→drive`, no
   paid Runpod run as part of this work.
2. **Refactor into a scenarios package** rather than growing `run_e2e.py`.
3. **Models:** reuse `HuggingFaceTB/SmolLM2-1.7B-Instruct` (single-file) for
   #1/#3/#4; use `Qwen/Qwen2.5-3B-Instruct` (2 real shards, 3.97 GB + 2.20 GB)
   for #2. Verified sharded + balanced via the HF API.

## File structure

```
deploy/e2e/
  hf_worker.py      # NEW: "download in a clean subprocess" entrypoint.
                    #   huggingface_hub fixes cache/endpoint at import, so every
                    #   download must be its own process. Modes:
                    #     file     -> hf_hub_download one file, print sha256
                    #     snapshot -> snapshot_download whole repo, print JSON
                    #                 {rfilename: sha256} for every file
  harness.py        # NEW: shared helpers, extracted from run_e2e.py:
                    #   COMPOSE, NODES, caches path
                    #   sh(), wait_healthy(), reset_cache(node)
                    #   metrics(port), delta(before, after, key)
                    #   download(endpoint, rev), timed_download(...),
                    #     snapshot(endpoint, rev) -> dict[str,str]
                    #   Report, rate(nbytes, seconds)
  scenarios/
    __init__.py     # ORDER list + run(name, ctx) dispatch; each scenario is a
                    #   function taking a shared context (Report + node ports).
    peering.py      # existing S1–S4, moved verbatim (behavior unchanged)
    snapshot.py     # #1
    sharded.py      # #2
    concurrency.py  # #3
    eviction.py     # #4
  run_e2e.py        # thin orchestrator: parse E2E_SCENARIOS, build/start stack,
                    #   run selected scenarios in ORDER, print summary, teardown.
  docker-compose.yml# add `node-evict` service (see #4)
```

The `--worker` reentry currently shells to `run_e2e.py` via `__file__`; moving
it to `hf_worker.py` keeps that concern isolated and lets `harness.download()`
shell to a stable module path (`python -m … hf_worker`).

## Scenario specs

Each scenario resets only the state it needs and asserts on `/metrics` deltas.
Selection via `E2E_SCENARIOS` (comma list; default = all). Ground-truth DIRECT
pulls provide the byte-identity anchor, as today.

### #1 snapshot — full repo, mixed LFS + non-LFS

- Model: SmolLM2-1.7B-Instruct (has config.json, tokenizer files, one
  safetensors). Reset node-a cold.
- DIRECT `snapshot_download` → `truth_manifest: {rfilename: sha256}`.
- `snapshot_download` through node-a → `shim_manifest`.
- Asserts:
  - snapshot completed through the shim (no "Distant resource does not have a
    Content-Length" failure) — a raised exception fails the scenario.
  - `shim_manifest == truth_manifest` (every file present + byte-identical,
    including the non-LFS companions — the regression guard).
  - `wan_bytes` > 0 on the cold pull (LFS bytes flowed through the shim).

### #2 sharded — Qwen2.5-3B multi-shard (opt-in-heavy)

- Reset all nodes cold.
- Cold WAN snapshot through node-c → assert manifest == DIRECT truth, and
  `wan_bytes` ≈ total safetensors bytes (both shards pulled), `peer_bytes` == 0.
- Warm **peer** snapshot through node-a (node-c now warm) → assert manifest ==
  truth and `peer_bytes` > 0 covering the shard bytes (multi-file peering).
- Only 2 pulls, to bound the ~6 GB/pull cost.

### #3 concurrency — thundering herd / singleflight

- Reset node-a cold. Snapshot n/a — single file `model.safetensors`.
- Fire **6 concurrent** `download()` calls through node-a
  (`ThreadPoolExecutor(max_workers=6)`, each its own subprocess/HF_HOME).
- Asserts:
  - all 6 return `sha == truth`.
  - `wan_bytes` delta `< 1.5 × filesize` (concurrent cold fetches collapsed to
    ~one WAN pull — the singleflight guarantee; lenient to avoid timing flake).
  - `served_bytes` delta ≈ `6 × filesize` (every caller got the full file).

### #4 eviction — byte budget + no false HIT

- New compose service `node-evict` (port 8004, **not** in any peer set),
  env `XORB_CACHE_MAX_GIB=1`, own bind-mount `caches/evict`.
- Reset node-evict cold.
- Pull SmolLM2 `model.safetensors` (2.78 GB > 1 GiB budget) → `sha == truth`.
- Re-pull the same file → asserts:
  - `misses` delta > 0 on the re-pull (evicted xorbs re-fetched from WAN →
    eviction actually happened; a fully-warm cache would show 0 misses).
  - `sha == truth` still (atomic writes → eviction never yields a corrupt/partial
    false HIT).
  - (optional) `du` the bind-mount ≤ ~budget + one-xorb slack.

### #5 serverless cold-start — instrument only

- `worker/timing.py` / `worker/flash_app.py`: return a timing breakdown per job
  — `dep_upgrade_ms` (the first-call pip upgrade), `download_ms`, and
  `cold_first_invocation: bool` (true on the worker's first job).
- `drive/run.py`: persist those fields per job row in `data/jobs-<runid>.jsonl`.
- `harvest/report.py`: add a "cold-start vs steady-state" section (mean cold
  first-job wall time incl. dep upgrade vs mean warm-job wall time).
- No live run now; unit-tested against synthetic job rows. Numbers land on the
  next real `up→drive`.

## Testing

- **#1–#4** are themselves integration tests (real Docker + real HF). They run
  under `make test-e2e`. Behavior-preserving refactor is verified by the moved
  S1–S4 still passing.
- **Harness helpers** (`delta`, `rate`, manifest compare, scenario selection
  parsing) get unit tests under `deploy/e2e/` — pure functions, no network.
- **#5** instrumentation: unit-test the report's cold-start aggregation against
  hand-built job rows (`runpod_testbed/tests/`), and the worker timing shape.
- Go shim: unchanged. `cd shim-go && go test ./...` must stay green (no shim
  code changes expected; if any prove necessary, `-race` on concurrency paths).

## Non-goals

- No Tier-2 chunk-dedup scenarios (settled dead-end).
- No live paid Runpod run in this work (#5 is instrumentation).
- No changes to the Go shim behavior; scenarios test existing behavior.

## Risks / notes

- #2/#4 add real download volume to `make test-e2e` (~6 GB and ~5.5 GB
  respectively). Gate the heavy ones behind `E2E_SCENARIOS` so the default
  demo run can stay lean; document the full-run cost in the README.
- #3 thresholds are timing-sensitive; keep them lenient (order-of-magnitude,
  not exact) — the assertion is "collapse happened", not a precise byte count.
- `node-evict` always-up adds one container to the stack; acceptable overhead,
  and it keeps the eviction scenario fully isolated from the peer set.
