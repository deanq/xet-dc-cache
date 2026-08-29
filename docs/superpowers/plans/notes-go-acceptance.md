# Task 12 (REVISED): External black-box acceptance test for the Go shim

`shim-go/acceptance.py` builds the compiled `xetcache` Go binary, runs it as a
real subprocess (not in-process, unlike `m1/integration_test.py` which boots
the Python app via `uvicorn.Config(shim.app, ...)`), and drives it purely
over HTTP with the real `hf-xet` client via `HF_ENDPOINT`.

Repo: `HuggingFaceTB/SmolLM2-135M-Instruct`, file: `model.safetensors`,
revision: `main`.

## Result: PASS (all 6 steps)

| # | Step | Result |
|---|------|--------|
| 1 | Build binary + start shim subprocess, poll `/healthz` | PASS |
| 2 | Download through shim (`HF_ENDPOINT` set) into isolated client cache A | PASS — 269,060,552 bytes in 4.9s |
| 3 | Go `CACHE_DIR` populated with xorb files (excl. `manifests/`) | PASS — 9 xorb files on disk |
| 4 | Direct download (no shim) into isolated client cache B; sha256 must match step 2 | PASS — identical |
| 5 | Second through-shim download (fresh client cache C); `/metrics` hits + `wan_bytes_saved` must increase | PASS — hits 0 → 9, `wan_bytes_saved` 0 → 268,233,304 |
| 6 | Kill + restart binary on same `CACHE_DIR`, download again (fresh client cache D); `/metrics` must show hits (on-disk cache survives restart, LRU reseeded from disk) | PASS — hits 0 → 9 after restart |

### sha256 (byte parity gate)
```
via_shim sha256: 5af571cbf074e6d21a03528d2330792e532ca608f24ac70a143f6b369968ab8c
direct   sha256: 5af571cbf074e6d21a03528d2330792e532ca608f24ac70a143f6b369968ab8c
```
Match confirmed — the Go shim serves byte-identical content to a direct
download of the same revision.

### Sizes
- File size: 269,060,552 bytes (both via-shim and direct downloads).

### Metrics after 2nd through-shim download
```
hit_rate=0.5  hits=9  misses=9  reconstructions=4
served_bytes=536,466,608  wan_bytes=268,233,304  wan_bytes_saved=268,233,304
```

### Restart-reuse metrics (fresh client cache D, after killing + restarting the binary on the same CACHE_DIR)
```
hit_rate=1  hits=9 (0 -> 9 for this run)  wan_bytes_saved=268,233,304
```
The Go binary's `seedLRU()` (main.go) rebuilt Tier 1 LRU accounting from the
on-disk cache dir at startup, so the restarted process served the download
entirely from cache with zero new WAN bytes.

## Go code: no changes needed
`go test ./...` was green before and after (`ok xetcache 0.367s` / `0.380s`,
no test count change). The acceptance run did not reveal any Go bug.

## Script bug found and fixed during development
The first draft of `acceptance.py` ran all `hf_hub_download` calls in a
single Python process, mirroring `m1/integration_test.py`'s in-process
style. That produced a false negative: `huggingface_hub.constants` computes
`HF_XET_CACHE` (the client's local Xet chunk-cache path) once at import
time from `os.environ`, so only the *first* download in the process actually
honored a fresh `HF_XET_CACHE`/`HF_HOME` — every subsequent download in that
process kept quietly reading from client-A's local xet chunk cache no matter
what env vars were set, making it look like zero new requests reached the
shim. Fixed by running each `hf_hub_download` call in its own subprocess
(`subprocess.run([sys.executable, "-c", ...], env=...)`), which guarantees
each call gets a truly fresh, isolated client cache. This is the correct
design for any test that runs multiple `hf_hub_download` calls with
different `HF_HOME`/`HF_XET_CACHE` in the same script.

## How to run
```
cd shim-go && uv run acceptance.py
```
Port 8000, scratch temp `CACHE_DIR` only (`XORB_CACHE_MAX_GIB=0`, unbounded
for the test run) — no real cache directory was touched. Takes ~1-2 minutes
against a public repo, no `HF_TOKEN` required.
