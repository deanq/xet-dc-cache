# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A datacenter-local cache that lets stock HF/vLLM images pull Xet-backed models at
LAN speed on serverless workers, buffering the WAN cold-start download tax.
It works by transparent interception: a worker sets `HF_ENDPOINT` to the shim and
all HuggingFace traffic flows through it — no image or client changes.

The shim is a single **Go** binary (`shim-go/`, package `main`). It was originally
a Python prototype, rewritten to Go as a 1:1 parity port; the Python offline tools
(`tools/`) were kept because they bind a Rust chunker the Go shim doesn't need.

## Commands

```bash
make build          # -> shim-go/xetcache
make run            # foreground on :8000 (Ctrl-C)
make start / stop / restart / status / logs / metrics / clean-cache
make test           # cd shim-go && go test ./...
make test-e2e       # cross-DC peering e2e: 3 peered containers vs real HF (Docker + network)
```

`make test-e2e` (harness in `deploy/e2e/`) builds a 3-node peered stack and drives
real `hf_hub_download`s through it, asserting peer warm-hit (cold node pulls a warm
peer's bytes, zero WAN, byte-identical), WAN fallback, and peers-down resilience.
Override the model with `SMOKE_REPO`/`SMOKE_REV`/`SMOKE_PATH`. The nodes run as
root and mount host bind-mounts under `deploy/e2e/caches/` (git-ignored) so the
driver can reset them — that's e2e-only; production runs non-root.

Go dev (from `shim-go/`):
```bash
go test ./...
go test -run TestGetXorbMissThenHit ./...   # single test (by name)
go test -race ./...                          # race detector — run this on concurrency changes
go vet ./...
```

Live end-to-end acceptance (network; downloads ~269 MB a few times):
```bash
cd shim-go && uv run acceptance.py
```
It launches the binary, downloads a model through it vs directly, and asserts
byte-identity + cache hits + restart-reuse. **Run each `hf_hub_download` in its own
subprocess** — `huggingface_hub` fixes `HF_XET_CACHE`/cache paths once at import,
so multiple downloads in one process silently share client-side cache (this is why
the acceptance script forks per download; the deleted in-process Python tests could
not test the Go binary for the same reason).

Offline tools (`tools/`, PEP 723 `uv` scripts; `fsck.py` needs the Rust wheel):
```bash
uvx maturin@1 build --release -m xet_verify/Cargo.toml         # build the chunker wheel
cd tools && uv run --with ../xet_verify/target/wheels/xet_verify-*.whl fsck.py --parity
cd tools && uv run dedup_study.py probe org/model path         # cross-revision immutability probe
```

Feasibility study (`study/`, run from that dir — scripts read/write parquet in cwd):
```bash
cd study && uv run placement_locality_sim.py events.parquet --mode tier1 --ttl-hours 24
```

## The Xet protocol the shim mediates

Understanding the three handlers requires understanding the download flow (all
verified against the live API; see `docs/xet-cache-shim-design.md`):

1. **token** — client GETs `.../xet-read-token/...`. The response carries the CAS
   endpoint **both** in the body (`casUrl`) **and** in response headers
   (`X-Xet-Cas-Url`, plus `X-Xet-Access-Token`/`X-Xet-Token-Expiration`). Current
   `huggingface_hub` reads the **headers** (`parse_xet_connection_info_from_headers`),
   so the `hub` handler must forward the upstream headers and rewrite
   `X-Xet-Cas-Url` → `PUBLIC_BASE/cas` (it also rewrites the body `casUrl` for
   back-compat), passing the access token/expiration through untouched. Dropping
   the headers (e.g. returning only a rewritten body) breaks every download with
   "Xet headers have not been correctly set by the server".
2. **resolve** — a `302` carrying `X-Xet-Hash`; passes through untouched.
3. **reconstruction** — `GET /cas/{v}/reconstructions/{file_id}` returns
   `{terms, xorbs: {hash: [{url, ranges}]}}` where `url` is a signed CDN URL. The
   relay rewrites each `url` → `PUBLIC_BASE/xorb/...` and stashes the signed URL.
   **Reconstruction is Range-specific**: a request for a different byte range
   returns different terms/offsets, so it must be cached keyed by the Range.
4. **xorb fetch** — `GET /xorb/xorbs/default/{hash}` + `Range`; the store serves
   from disk or replays the range against the stashed signed URL.

`Server` (in `server.go`) plays all three roles: `hub` (`proxy.go`),
`reconstruction` (`reconstruction.go`), `getXorb` (`xorb.go`).

## Invariants that will bite you if changed

These encode real bugs already fixed and a hard external constraint — a fresh
reader will not infer them from any single file:

- **On-disk format is a compatibility contract.** Xorb cache key =
  `sha256("{hash}:{range}")` hex (`cachePath`, `util.go`); manifest filename =
  `{version}_{fileID}_{sanitized-range or "full"}.json` (`manifests.go`, sanitize
  `[^0-9A-Za-z_-]→_`). Unit tests pin these against literal values. Changing
  either silently breaks reuse of a warm cache written by an older build. Peers
  share this key, so the probe `HEAD` and fetch `GET` a peer receives resolve to
  the exact same file a local request would — cross-node reuse needs both nodes on
  the same key format.
- **The token response must carry the Xet headers** (`X-Xet-Cas-Url` rewritten to
  `PUBLIC_BASE/cas`; access token + expiration forwarded). Current `huggingface_hub`
  reads connection info from these headers, not the body; the `hub` handler
  (`proxy.go`) must not drop them (regression-tested in `proxy_test.go`).
- **`fetchAuthorized` tries multiple signed URLs per xorb** (`xorb.go`). A xorb
  spanning a segment boundary appears in several ranged reconstructions, each with
  a CDN URL whose policy authorizes a *different* byte window. Keep a candidate
  list; 409 if the hash is unknown, 502 only if none authorize. Do not collapse to
  one URL.
- **Reconstruction offline-serve is range-scoped and transport-only**
  (`reconstruction.go`): serve a cached manifest *only* on a transport error and
  *only* for the exact incoming Range. A reachable non-200 (e.g. 416) must be
  propagated verbatim, never replaced with a cached manifest — otherwise the client
  gets a range-incoherent reconstruction ("byte range not sequential").
- **Cache writes are atomic** (`writeCacheFileAtomic`, `xorb.go`): temp file in the
  same dir + rename, so a partial write never becomes a false HIT. `seedLRU`
  (`main.go`) skips dotfiles for this reason (temp debris) and the `manifests`
  subdir.
- **The hub pass-through must preserve upstream `Content-Length`** (`proxy.go`). A HEAD to `resolve`
  for a **non-LFS** file (config.json, tokenizer.json, most repo files) carries its size ONLY in
  `Content-Length`; LFS/Xet files use `X-Linked-Size`. `cleanHeaders` strips `Content-Length` (it must,
  for the body-rewriting token path), so the pass-through re-sets it from upstream. Drop it and every
  full `snapshot_download` fails with huggingface_hub's "Distant resource does not have a Content-Length"
  — the shim silently works for the big safetensors (X-Linked-Size) but not the small companion files.
  Regression-tested in `proxy_test.go` (`TestHubPreservesContentLengthOnHeadResolve`).
- **HTTP client**: `CheckRedirect → http.ErrUseLastResponse` (no redirect
  following — the 302 must reach the client), `DisableCompression`, and every
  upstream request sets `Accept-Encoding: identity`.
- **ServeMux (Go 1.22+)**: a `GET /` pattern also matches `HEAD`. Do NOT also
  register `HEAD /` — it panics at startup. `GET /` alone serves HEAD-to-hub.
- **`singleflight`** (`s.sf`) collapses concurrent cold fetches for the same
  `(hash, range)`; inside the closure count `misses`/`wan_bytes` once,
  `served_bytes` per caller (outside) so `wan_bytes_saved` stays correct under
  fan-out.

## Settled dead-ends — do not rebuild

- **Content-address verification of downloads is not achievable** via the client
  API (`X-Xet-Hash` is a server-side HMAC with an unshared salt; no reproducible
  anchor). `xet_verify` exists only for offline chunk-parity/dedup analysis, not
  download verification. See `docs/xet-cache-findings.md`.
- **Tier 2 (chunk coalescing) was measured at ~0 payoff** for model serving
  (weights are immutable across revisions, ~0% shared chunks across fine-tunes) and
  was dropped from the Go shim. All real dedup is whole-file, captured by Tier 1.

## Configuration

Env vars (read in `main.go`): `HF_UPSTREAM`, `CAS_UPSTREAM`, `PUBLIC_BASE`,
`CACHE_DIR`, `XORB_CACHE_MAX_GIB` (0 = no byte budget), `CACHE_MIN_FREE_PCT`
(default 10; always-on disk-free watermark via `statfs`, 0 = disable),
`SIGNED_URL_TTL_SECONDS`,
`SIGNED_URL_MAX_ENTRIES`, `MANIFEST_CACHE_MAX_ENTRIES`,
`SIGNED_CANDIDATES_PER_XORB` (default 8; how many signed CDN URLs to retain per
xorb hash — a xorb spanning >N ranged reconstructions needs a deeper list or a
window's authorizing URL can be evicted → spurious 502), `PORT`,
`MAX_INFLIGHT_FETCHES` (0 = unlimited; caps concurrent upstream misses),
`SHIM_AUTH_TOKEN` (empty = open), `PEERS` (comma-separated sibling base URLs;
empty = peering off), `SELF_URL` (filtered from `PEERS`),
`PEER_PROBE_TIMEOUT_MS` (default 200), `PEER_STICKY_TTL_SECONDS` (default 60),
`PEER_FETCH_TIMEOUT_MS` (default 10000; hard per-GET ceiling on the peer range
fetch). Peer transport tuning (Mechanism A): `PEER_MAX_IDLE_CONNS_PER_HOST`
(default 64), `PEER_SOCKET_BUFFER_BYTES` (0 = OS autotune), `PEER_KEEPALIVE_INTERVAL_MS`
(0 = off). Adaptive hedge (Mechanism B): `PEER_HEDGE_FACTOR` (default 1.5),
`PEER_HEDGE_MIN_MS` (default 50), `PEER_HEDGE_MAX_MS` (default 1000).
**`PUBLIC_BASE` is baked into the rewritten URLs handed to clients**, so it
must be reachable from the worker — set it explicitly for any non-localhost
deployment.

**Tier 1.5 peering** (see `docs/superpowers/specs/2026-09-03-peer-transfer-optimization-design.md`):
on a miss the shim serves from a sibling over the private backbone instead of the
CDN. Two mechanisms. **(A)** peer traffic uses a dedicated, HTTP/1.1-forced
transport (`peer_transport.go`, distinct from the CDN `doer`; fat idle pool,
optional socket buffers, optional keepalive) so a peer pull starts warm.
**(B)** an **adaptive hedged race** (`peer_hedge.go` `raceOnePeer`): fire the peer
`GET`, give it a head start sized by the peer's throughput EWMA (`peerStats`),
and if it runs long, race a CDN pull in parallel, take the first to finish, and
cancel the loser. This bounds a **single race** to `WAN + PEER_HEDGE_MAX_MS`
(plus one discovery HEAD RTT on a cold, non-sticky range). The bound is
**per-race, not per-request**: `raceOnePeer` returns failure only when its peer
*and* CDN sides both fail, so on cascading CDN failure `fetchFromPeer` can run a
sticky race, then a fan-out race, and `getXorb` then still runs the plain CDN
path — up to three sequential CDN attempts. That chain is gated on CDN failure
(healthy-CDN requests always finish in the first race) but exceeds the single-race
bound when it triggers. The sticky path fires a **bare peer GET** (no HEAD — a peer serves
hit-or-404, so a 404 *is* the miss signal); the fan-out **HEAD** probe stays for
discovery. Peer-served bytes count as `peer_bytes`, a hedged CDN win as
`wan_bytes`; the decision metric is `xet_peer_bytes_total` vs `xet_wan_bytes_total`.
Note: since HF pays CDN egress, hedged CDN bytes are ~free to the operator — the
hedge dial trades HF-CDN load, not egress dollars.

Endpoints: `GET /healthz`, `GET /metrics` (JSON), `GET /metrics/prometheus`
(text exposition, hand-rolled in `prometheus.go`), plus the three protocol
handlers. Requests pass through `withLogging`→`withAuth` (`middleware.go`):
slog JSON request logs, and — only when `SHIM_AUTH_TOKEN` is set — a Bearer gate
on the **peer channel** (`X-Xet-Peer: 1`) that exempts `/healthz` and
`/metrics*`. The gate does **not** apply to client traffic: a stock HF client
only sends its own HF token in `Authorization` (forwarded upstream), so it can
never present the shim secret — gating client paths would 401 every real
download. Client traffic relies on network isolation. The shim forwards client
HF tokens upstream over plaintext HTTP: it is a **trusted-LAN component** (trust
boundary documented in `deploy/README.md`).

## Layout

- `shim-go/` — the cache service (Go). One file per concern; `*_test.go` alongside.
- `tools/` — Python offline analysis tools; `xet_verify/` is the Rust pyo3 chunker they bind.
- `study/` — Phase-1 feasibility sim ("is host-local caching worth building?"), independent of the shim.
- `docs/` — design (`xet-cache-shim-design.md`), findings (`xet-cache-findings.md`),
  the LFS follow-up (`lfs-support-handoff.md`), and `superpowers/` (the Go-rewrite spec/plan/notes).

Follow-up work is scoped in `docs/lfs-support-handoff.md` (add git-LFS-bridge
caching) — written against the Go shim. Note its premise correction: all HF repos
are Xet-enabled and dual-available, so LFS caching only matters for non-Xet
*client* stacks, not for any un-migrated repo class.
