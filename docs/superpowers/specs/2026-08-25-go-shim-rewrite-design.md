# Design: rewrite the Xet DC-cache shim in Go

**Date:** 2026-08-25 **Status:** approved, not started.
**Scope:** 1:1 Xet-path parity port of the Python shim (`m1/`) to Go. LFS and
Tier 2 are explicitly out of scope.
**Prereq reading:** `m1/README.md` (the shipping shim), `m1/shim.py` (the port
source of truth), `xet-cache-findings.md` (why Tier 2 is dropped),
`lfs-support-handoff.md` (the deferred follow-up).

## Why

The shim is a pure I/O-bound streaming reverse proxy — Go's home turf. The
decision to port (see the language-choice discussion, 2026-08-25) rests on three
concrete wins: single static binary deploy (no `uv`/rustc toolchain on worker
hosts), bounded-memory streaming as the native idiom, and trivial collapse of
concurrent duplicate cold fetches. This spec covers **Xet parity only** so the
port can be validated 1:1 against the existing black-box tests before any new
behavior (LFS) is layered on.

Non-goals for this port: LFS caching (`lfs-support-handoff.md`), Tier 2
coverage-map coalescing (measured zero payoff for model serving —
`xet-cache-findings.md`), content-address verification (impossible via the
client API), and TLS/auth on the shim itself.

## Architecture

One self-referential HTTP service playing all three roles from the original
design doc, mirroring `m1/shim.py`:

- **(A) Hub shim** — transparent proxy to `huggingface.co`. Rewrites only the
  `xet-read-token` response body (`casUrl` → `PUBLIC_BASE/cas`). Everything else
  — including the resolve `302` + `X-Xet-Hash` — passes through verbatim.
- **(B) CAS relay** — `GET /cas/{version}/reconstructions/{file_id}`: proxies
  the CAS server, rewrites each xorb `url` → `PUBLIC_BASE/xorb/...`, and records
  the signed CDN url in an ephemeral side-map.
- **(C) Xorb store** — `GET /xorb/xorbs/default/{hash}` + `Range`: Tier 1 cache
  keyed by `(hash, exact range)`. Miss replays the range against a signed CDN url
  from the side-map.

### Package layout

Flat `main` package mirroring the `m1/` flat layout (not `cmd/`+`internal/` —
overkill for ~600 lines). Zero third-party deps except `x/sync/singleflight`
(Go-team owned).

```
shim-go/
  go.mod              module xetcache; require golang.org/x/sync
  main.go             env config, dependency wiring, http.Server on :PORT
  proxy.go            (A) hub handler, proxyHub, clean/hop-by-hop headers
  reconstruction.go   (B) CAS relay, manifest rewrite, offline-serve
  xorb.go             (C) getXorb Tier 1, fetchAuthorized, rememberSigned
  signedmap.go        TTLMap        (port of ttlmap.py)
  lru.go              LruDiskCache  (port of eviction.py)
  manifests.go        ManifestCache (port of manifest_cache.py, range-keyed)
  metrics.go          Metrics + snapshot
  *_test.go           per-file unit tests
```

## Components

### Config (`main.go`)

Read at startup from env, same names and defaults as `shim.py`:

| Env var | Default | Meaning |
|---|---|---|
| `HF_UPSTREAM` | `https://huggingface.co` | Hub origin |
| `CAS_UPSTREAM` | `https://cas-server.xethub.hf.co` | CAS origin |
| `PUBLIC_BASE` | `http://127.0.0.1:8000` | baked into rewritten urls |
| `CACHE_DIR` | `./xorb-cache` | cache root |
| `XORB_CACHE_MAX_GIB` | `0` (unbounded) | Tier 1 LRU cap |
| `SIGNED_URL_TTL_SECONDS` | `3600` | side-map entry TTL |
| `SIGNED_URL_MAX_ENTRIES` | `100000` | side-map cap |
| `MANIFEST_CACHE_MAX_ENTRIES` | `50000` | manifest cache cap |
| `PORT` | `8000` | listen port |

**Dropped:** `CACHE_TIER` (Tier 2 removed). Binds `0.0.0.0:PORT`.

### TTLMap (`signedmap.go`)

Port of `ttlmap.py`. `Set(key, value)`, `Get(key) (string, bool)` with
monotonic-clock expiry and oldest-drop cap. Injectable clock for tests.
`sync.Mutex` guarded. Value type is `[]string` here (candidate url list — see
xorb.go), so the map is `map[string]entry{expiry, []string}`.

### LruDiskCache (`lru.go`)

Port of `eviction.py`. Byte-capped LRU accountant over an ordered map
(`container/list` + `map`). `Load(entries)` seeds oldest-first from disk mtime;
`Touch`, `Record(key,size)`, `Forget`; `Evict` via injected `delete(key)`
callback. `max_bytes <= 0` disables eviction. `sync.Mutex` guarded.

### ManifestCache (`manifests.go`)

Port of `manifest_cache.py`. Range-keyed on-disk JSON:
`{version}_{file_id}_{sanitized_range or "full"}.json` — **filename format
identical to Python** so caches interoperate. `Get`/`Put(version,file_id,range_key,
manifest)`, mtime-ordered trim to cap. Sanitize with the same
`[^0-9A-Za-z_-] → _` rule.

### Metrics (`metrics.go`)

Port of `metrics.py`. Mutex-guarded `map[string]int64` counter with `Incr(key,n)`
and `Snapshot()` computing `wan_bytes_saved = served_bytes - wan_bytes` and
`hit_rate = hits/(hits+misses)`. `/metrics` returns JSON; adds
`signed_urls_tracked`. (No `cache_tier` field — Tier 2 gone.)

### HTTP client

`http.Client` with `CheckRedirect: return http.ErrUseLastResponse` (parity with
httpx `follow_redirects=False`) and `Transport{DisableCompression: true}` for
verbatim passthrough. Timeout 60s. Exposed behind a small `httpDoer` interface
(`Do(*http.Request) (*http.Response, error)`) so tests inject a fake — the Go
equivalent of monkeypatching `shim.client`.

## Data flow

Unchanged from the verified protocol:

```
client -> (A) GET .../xet-read-token/...   -> rewrite casUrl -> PUBLIC_BASE/cas
client -> (B) GET /cas/{v}/reconstructions/{file_id} [Range]
              -> proxy CAS; per xorb: rememberSigned(hash,url); url -> /xorb/...
              -> cache rewritten manifest keyed by (v,file_id,range)
client -> (C) GET /xorb/xorbs/default/{hash} [Range]
              -> Tier 1 hit: serve from disk (X-Cache: HIT)
              -> miss: fetchAuthorized(hash,range) -> write -> serve (X-Cache: MISS)
```

### fetchAuthorized / rememberSigned (xorb.go) — carries both bug fixes

- `rememberSigned(hash, url)`: prepend, dedupe, cap at `SIGNED_URL_CANDIDATES=8`,
  most-recent-first. A boundary-spanning xorb appears in multiple ranged
  reconstructions with different window-scoped urls; we keep several.
- `fetchAuthorized(hash, range)`: iterate candidates, return first `200/206`;
  `409` if the hash is unknown (needs reconstruction first); `502` if candidates
  exist but none authorize the range. (This is the "502 Bad Gateway" fix.)

### reconstruction (reconstruction.go) — carries the other bug fix

- Forward `Authorization` and `Range`; force `Accept-Encoding: identity` (we must
  parse JSON).
- **Transport error** (unreachable/timeout): serve cached manifest for *this
  exact range* only; else `502`. Never a range-mismatched manifest.
- **Reachable non-200** (e.g. 416): propagate verbatim so the client retries.
  (This is the "byte range not sequential" fix.)
- **200**: rewrite manifest, cache it keyed by range, return.

### Deliberate Go-native improvements (not silent drift)

1. **`singleflight` on cold xorb fetch**: N concurrent cold requests for the same
   `(hash,range)` collapse to one CDN pull. Python let them all fetch. Keyed by
   the cache filename. This is the concurrency payoff motivating the port.
2. **Verbatim encoding passthrough**: `DisableCompression=true` + forward bytes
   as-is on the passthrough path; force identity encoding only on the two JSON-
   rewriting handlers. Cleaner than Python's decode-then-strip; content-encoding
   preserved on passthrough.

Ranges stay **buffered** in memory (≤64 MB/xorb), matching Python — streaming was
the LFS motivation and LFS is out of scope. The fetch path is structured so
streaming drops in later.

## Error handling (parity)

- Missing `Range` on `/xorb` → `400`.
- Unparseable `Range` → `400`.
- Unknown xorb hash → `409`.
- No candidate url authorizes the range → `502`.
- Hop-by-hop headers (RFC 7230 §6.1) stripped both directions, plus
  `content-length` and `content-encoding` on rewritten responses.
- Fail-safe unchanged: if the shim is unreachable, a worker with `HF_ENDPOINT`
  unset falls back to `huggingface.co` — no outage.

## On-disk compatibility

**Critical:** the Go cache reuses the existing on-disk format so the warm
production cache (`$HOME/.cache/xet-dc-cache`, ~2 GB) is reused, not re-pulled.

- Xorb cache key: `sha256(fmt.Sprintf("%s:%s", hash, range))` hex — byte-for-byte
  identical to Python's `_cache_path`.
- Manifest filenames identical (see ManifestCache).
- LRU rebuilds from disk mtime at startup, same as Python.

## Testing

### Go unit tests (`go test ./...`, no network)
- `TTLMap`: expiry via injected clock, oldest-drop cap.
- `LruDiskCache`: LRU order, cap eviction, `cap=0` passthrough, `Load` seed+trim.
- `ManifestCache`: range-keying (distinct ranges → distinct files), trim.
- `rememberSigned`: order, dedupe, cap.
- `fetchAuthorized`: candidate iteration (wrong-window 403 → next), stops at first
  authorized, `502` when none, `409` when unknown — via injected `httpDoer` fake.
- `_cache_path` stability: same `(hash,range)` → same filename as Python
  (assert against a known Python-produced hex to prove interop).

### Black-box acceptance (existing Python tests, unchanged)
The `hf-xet` Rust client doesn't care what language the shim is. Point the
existing tests at the Go binary via `HF_ENDPOINT`:
- `m1/smoke_test.py` — token → resolve → reconstruction → xorb-range; asserts
  MISS→HIT and bytes identical to a direct upstream fetch.
- `m1/integration_test.py` — real `hf_hub_download`, sha256 byte-identity vs
  direct, cache populated, second pull is a HIT.

These are the acceptance gate: parity is proven when both pass against the Go
binary exactly as they do against the Python shim.

## Makefile changes

- Add `build`: `cd shim-go && go build -o xetcache .`
- `start` / `run`: exec the binary instead of `uv run shim.py` (same env vars).
- `test`: run `go test ./...` (in `shim-go`) plus the Python black-box tests.
- Keep `stop`/`status`/`logs`/`metrics`/`clean-cache` unchanged (port-based).

## Definition of done

- [ ] `go build` produces a single static `xetcache` binary; `go vet` clean.
- [ ] All Go unit tests pass; `_cache_path` interop test proves identical keys.
- [ ] `smoke_test.py` passes against the Go binary (MISS→HIT, byte-identical).
- [ ] `integration_test.py` passes: real `hf_hub_download` byte-identical to
      direct, cache populated, second pull HIT.
- [ ] Warm existing cache dir is reused (a pull of a previously-cached range is a
      HIT with no WAN bytes) — proves on-disk compatibility.
- [ ] `/metrics` and `/healthz` behave as today; `/metrics` shows
      `wan_bytes_saved` and `hit_rate`.
- [ ] Concurrent cold requests for the same `(hash,range)` produce one CDN pull
      (singleflight) — unit test with a counting fake `httpDoer`.
- [ ] Makefile `build`/`start`/`run`/`test` work; the Python shim remains runnable
      as a fallback until the Go binary is validated in place.

## Risks / edge cases

- **Header transparency drift** — Go's `http.Transport` and header canonicalization
  differ from httpx. Mitigate: `DisableCompression=true`, explicit hop-by-hop
  stripping, and the black-box tests catch any client-visible regression.
- **`ServeMux` catch-all vs specific routes** — Go 1.22 mux must register the
  specific `/cas/`, `/xorb/`, `/healthz`, `/metrics` routes with higher precedence
  than the `/` catch-all; 1.22 precedence rules handle this, but verify the
  catch-all doesn't shadow. Test with a request to each route.
- **`Range` header casing / multiple ranges** — parity: support single
  `bytes=lo-hi` only, `400` otherwise (same as Python `_parse_range`).
- **singleflight error sharing** — a failed shared fetch propagates one error to
  all waiters; acceptable (they'd all have failed). Losers do not cache.
- **Coexistence** — Python shim and Go binary must not run on the same port
  simultaneously; Makefile `start` stops the port first (unchanged).

## Rollback

The Python shim stays in `m1/` untouched and runnable. If the Go binary
misbehaves in place, `make start` can be pointed back at `uv run shim.py` by
reverting one Makefile change. Reversible by design.
