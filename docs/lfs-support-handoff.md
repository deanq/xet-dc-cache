# Handoff: add git-LFS caching to the Go shim

**Status:** proposed, not started. **Owner:** _unassigned_. **Est:** ~1.5–2 days.
**Prereq reading:** the Go shim source under [`shim-go/`](../shim-go/) (start with
`proxy.go`, `xorb.go`, `server.go`, `main.go`), `xet-cache-findings.md` (why
whole-file dedup is the whole game), `xet-cache-shim-design.md` (the Xet protocol
the shim already implements).

> **This doc was rewritten for the Go shim.** The shim was ported from Python to
> Go (`shim-go/`, package `main`); the old Python implementation is gone. All
> symbols and file paths below refer to the Go code.

## Why

The shim caches the **Xet** download path today. Repos not yet migrated to Xet
(the shrinking long tail, opted-out, some private) still serve weights via
**git-LFS**. Those downloads currently pass straight through the `hub` handler to
the CDN — they work, but they are **uncached**, so they pay the full WAN tax on
every cold worker. Goal: cache LFS objects too, with the same cross-worker /
cross-revision dedup we get for Xet.

This is **purely additive**. Do not change the Xet path or the LFS passthrough
fallback; if anything here fails, LFS must still degrade to "works, uncached."

## What's already true (verified live 2026-08)

An LFS resolve is dramatically simpler than the Xet reconstruction dance — the
resolve response *is* the whole story. `HEAD https://huggingface.co/{repo}/resolve/{rev}/{path}`:

| Signal | Value / meaning |
|---|---|
| status | `302` |
| `X-Linked-Etag` | quoted **SHA256 oid** of the file, e.g. `"68d45e…9d3"`. The stable content key — constant across revisions and signed-URL rotation. **This is the cache key.** |
| `X-Linked-Size` | object size in bytes (e.g. `440449768`) |
| `Location` | one signed CDN URL (`us.aws.cdn.hf.co`, path `/xet-bridge-us/...`) that serves **arbitrary `Range` over the whole object** — verified: `bytes=0-1023`, a mid-file range, and the last 1 KB all returned `206`. No per-window signature, no chunk footer. |
| `X-Xet-Hash` | present *too* on migrated repos. If present, the `hf-xet` client takes the Xet path (already cached); a plain git-lfs / no-hf_xet client follows the `302`. Rewrite the `302` whenever `X-Linked-Etag` is present — it's harmless for Xet clients (they ignore it). |

Contrast with Xet, both **in LFS's favor**:
- **One signed URL, whole-object authorized.** None of the Xet per-window
  signed-URL juggling — the candidate-list logic in `fetchAuthorized`
  (`xorb.go`) and its 502-window bug simply cannot occur for LFS. One url per oid.
- **Content verification is POSSIBLE.** The oid *is* the whole-file SHA256, so
  the cache can hash what it stores and reject corruption — the fail-secure
  integrity that was impossible for Xet (salted `X-Xet-Hash`, unreachable
  footer; see `xet-cache-findings.md`).

## Design

Three pieces; two reuse existing machinery in `shim-go/`.

### 1. Detect + rewrite (in the `hub` handler, `proxy.go`)

`hub` already proxies the resolve through `proxyHub` and returns it. Add: if the
upstream response carries `X-Linked-Etag`, stash `Location` in an oid→URL side-map
and rewrite `Location` to point at us. Do this **before** the existing final
passthrough, and leave the `xet-read-token` rewrite branch untouched.

```go
// in hub(), after proxyHub returns `upstream, body, err`:
oid := strings.Trim(upstream.Header.Get("X-Linked-Etag"), `"`)
if oid != "" && upstream.StatusCode >= 300 && upstream.StatusCode < 400 &&
    upstream.Header.Get("Location") != "" {
    s.lfsURLs.Set(oid, []string{upstream.Header.Get("Location")})
    cleanHeaders(w.Header(), upstream.Header)
    w.Header().Set("Location", s.publicBase+"/lfs/"+oid)
    w.WriteHeader(upstream.StatusCode)
    _, _ = w.Write(body)
    return
}
```

- Add a `lfsURLs *TTLMap` field to `Server` (`server.go`) and wire it in `main.go`
  exactly like `signed` (`NewTTLMap(SIGNED_URL_TTL, SIGNED_URL_MAX, nil)`). Reuse
  `TTLMap` as-is — its value type is already `[]string`; store a **single-element**
  slice per oid (whole-object authorized, so no candidate list needed).
- Keep the existing `xet-read-token` branch and the plain passthrough untouched.

### 2. Serve `GET /lfs/{oid}` (mirror `getXorb`, simpler + streaming)

Register in `main.go`: `mux.HandleFunc("GET /lfs/{oid}", s.getLFS)`.

```go
func (s *Server) getLFS(w http.ResponseWriter, r *http.Request) {
    oid := r.PathValue("oid")
    byteRange := r.Header.Get("Range")          // may be empty (whole file)
    // Tier 1 cache hit: key by (oid, range) via cachePath(s.cacheDir, oid, byteRange)
    // miss: cands,_ := s.lfsURLs.Get(oid); if none -> 409 (client re-resolves)
    //       STREAM the signed url -> disk (see below), serve; record LRU + metrics
    //       collapse concurrent cold fetches with s.sf.Do(name, ...)
}
```

Reuse verbatim: `cachePath(dir, hash, byteRange)` (pass `oid` as `hash` — the key
is `sha256(oid+":"+range)`, same scheme as xorbs), `s.lru` (`*lruCache`,
`Touch`/`Record`), `s.metrics` (`Incr`; fold into the existing
`hits`/`misses`/`served_bytes`/`wan_bytes` so `wan_bytes_saved` stays unified, and
optionally add `lfs_hits`/`lfs_misses`), and `s.sf` (`singleflight.Group`) to
collapse concurrent cold fetches for the same oid — the "per-oid fetch lock" the
old design flagged as a risk is **free** here, `s.sf` already exists on `Server`.

**The one net-new bit: streaming.** Xet ranges are ≤64 MB/xorb so `getXorb`
buffers them (`io.ReadAll` + `writeCacheFileAtomic`). LFS objects are whole
multi-GB files — do **not** buffer. Stream instead:

```go
// inside the singleflight miss closure, resp = the signed-url response:
tmp, _ := os.CreateTemp(s.cacheDir, "."+name+".tmp-*")
h := sha256.New()
// fan out the stream to disk, the client, and the hasher in one pass:
_, err := io.Copy(io.MultiWriter(tmp, w, h), resp.Body)
// then: close tmp; if whole-object fetch, verify hex(h) == oid before rename;
// on match os.Rename(tmp, path); on mismatch os.Remove(tmp) (quarantine, no cache)
```

This mirrors `writeCacheFileAtomic`'s temp-file-then-rename discipline (`xorb.go`)
but with `io.Copy` streaming through an `io.MultiWriter` so shim RSS stays bounded
regardless of object size. Note the client is written to as part of the same copy,
so on a hash mismatch the client already received the bytes — that's fine: git-lfs
/ hf clients verify the oid themselves and retry, and we simply never cached the
bad object. On a cache **hit**, serve with `http.ServeContent` / `io.Copy` from the
open file (native `Range` support), not `os.ReadFile` (don't buffer GBs).

### 3. Integrity (feasible here, unlike Xet)

When a **whole** object finishes streaming, `sha256 == oid` → rename into cache;
on mismatch remove the temp and count a `lfs_corrupt` metric (fail-secure). For
ranged fetches, defer verification (a single range can't be checked against the
whole-file oid); rely on per-range self-consistency and re-verify opportunistically
if/when the whole object is present. Recommend: verify on whole-file fetches (the
common `hf_hub_download` case); document ranged as best-effort.

## Reuse vs build

| Reuse as-is | Build new |
|---|---|
| `proxyHub`, `cleanHeaders`, `hopByHop` (`proxy.go`/`util.go`) | oid detection + `Location` rewrite in `hub` |
| `TTLMap` (→ new `Server.lfsURLs`) | `GET /lfs/{oid}` handler `getLFS` |
| `cachePath`, `s.lru` (`lruCache`), `s.sf` (`singleflight`) | **streaming** fetch→disk→serve helper (`io.MultiWriter`) |
| `Metrics`, `/metrics`, `/healthz`, config + env in `main.go` | oid SHA256 verification (whole-file) |
| `writeCacheFileAtomic`'s temp+rename pattern | `seedLRU` awareness (see edge cases) |

## Validation plan

- **Unit** (`shim-go/lfs_test.go`, no network): oid extraction (quote-stripping),
  `Location` rewrite only when `X-Linked-Etag` present, `lfsURLs` set/get, cache
  key stability, sha256-mismatch → quarantine (no file left at `path`). Mirror the
  fake-`httpDoer` pattern in `xorb_test.go`/`proxy_test.go` (inject `s.doer`).
- **Integration** (real client through the binary, like `shim-go/acceptance.py`):
  `hf_hub_download` a genuine LFS object through the shim; assert byte-identical to
  a direct pull and that the object is cached; second pull is a HIT with
  `wan_bytes_saved` rising. Run each download in its **own subprocess** —
  `huggingface_hub` computes `HF_XET_CACHE`/cache paths once at import, so multiple
  downloads in one process silently share client-side cache (this bit
  `acceptance.py`; see `docs/superpowers/plans/notes-go-acceptance.md`).
- **Finding a non-Xet repo is now hard** (HF migrated even bert/gpt2). Force the
  LFS branch by setting `HF_HUB_DISABLE_XET=1` on the client — it then follows the
  `302`/LFS path even for Xet-backed repos, exercising `/lfs` end-to-end. Failing
  that, hunt a still-LFS repo (private/older/opted-out).
- **Regression**: the existing Go suite (`cd shim-go && go test ./...`, plus
  `go test -race ./...`) must stay green, and `shim-go/acceptance.py` (Xet path)
  must still pass.

## Definition of done

- [ ] A worker with `HF_HUB_DISABLE_XET=1` (or a non-Xet repo) downloads through
      the shim, byte-identical to direct, and the object is cached.
- [ ] Second download (any worker) is served from cache; `/metrics` shows the
      hit and `wan_bytes_saved`.
- [ ] Whole-file sha256 verified against oid; corruption quarantined (no cache
      entry, `lfs_corrupt` counted).
- [ ] Large (multi-GB) object streams via `io.MultiWriter` — shim RSS stays
      bounded (no whole-file buffering); confirm with a memory check on a >1 GB pull.
- [ ] Xet path + LFS passthrough fallback both still work; `go test ./...`,
      `go test -race ./...`, and `acceptance.py` all green.

## Risks / edge cases

- **`seedLRU` and the shared cache dir.** LFS entries land under the same
  `cacheDir` as xorbs (both keyed via `cachePath`), so `seedLRU` (`main.go`) picks
  them up for free — good. But it currently skips only dotfiles and the `manifests`
  subdir; if you give LFS its own subdir, teach `seedLRU` about it, or keep LFS
  entries flat alongside xorbs (recommended — one LRU budget).
- **Streaming correctness under concurrency** — two workers requesting the same
  uncached oid simultaneously collapse via `s.sf.Do(name, ...)`; the loser waits
  and re-reads from the now-cached file. Verify the singleflight closure writes the
  file fully before returning so the waiter's `os.Open` sees a complete object.
- **Range vs whole-file mix** — safetensors/vLLM may issue `Range` reads; plain
  `hf_hub_download` grabs whole. Support both; key by `(oid, range)` via
  `cachePath`. A coverage-map (as in the dropped Xet Tier 2) is almost certainly
  not worth it — whole-file dedup dominates (`xet-cache-findings.md`).
- **Small/regular (non-LFS) files** — resolve returns `200` inline or a redirect
  with no `X-Linked-Etag`. Leave as passthrough; not worth caching (tiny).
- **Auth** — the CDN signed URL is pre-authorized (no `Authorization` to the CDN).
  The resolve itself may need the client's token; `proxyHub` already forwards it.
- **Disk pressure** — LFS whole files are large; the existing
  `XORB_CACHE_MAX_GIB` LRU applies, but a few multi-GB models fill a cap fast —
  tune operator expectations.

## Strategic note

HF's aggressive Xet migration is shrinking the LFS long tail — the Xet path the
shim already ships covers the overwhelming majority of popular-model pulls. Build
this for completeness and unconverted repos, but weigh the priority against your
actual workload. In Go it's genuinely cheap: mostly reuse, and the two things that
were *hard* in the Python design — bounded-memory streaming and collapsing
concurrent cold fetches — are `io.MultiWriter` and the already-present `s.sf`
respectively.
