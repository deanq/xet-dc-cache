# Xet-Aware DC Cache — Shim Skeleton Design

**Status:** draft / build-ready
**Goal:** A datacenter-local caching layer that lets stock HF/vLLM images (no Dockerfile changes) pull Xet-backed models at LAN speed, buffering the WAN and eliminating the repeated download tax on serverless scale-out.

**Confidence key:** ✅ confirmed from HF Xet spec / `xet-core` · ⚠️ inferred, verify before building.

---

## 1. Scope

**In:** transparent interception of the Xet download path; content-addressed caching of xorbs; cross-worker + cross-revision whole-file dedup (Tier 1). Chunk-level coalescing (Tier 2) is experimental/optional — see §5 and `xet-cache-findings.md`.

**Out:** uploads (`xet-write-token`, xorb/shard PUT), LFS fallback path (already handled by existing Strategy B), GPU/memory snapshotting (separate workstream).

**Non-goal:** reimplementing CAS semantics from scratch — wrap `xet-core` client crates instead (§7).

---

## 2. Components

Three services behind one skeleton. The skeleton (A + B) is mandatory for *every* tier; only C's backend changes between Tier 1 and Tier 2.

```
  worker (stock image)                     your DC cache
  ─────────────────────      ┌───────────────────────────────────────┐
  HF_ENDPOINT ───────────────▶ (A) Hub shim   /resolve, /xet-*-token │
                             │      rewrites X-Xet-Cas-Url            │
  reconstruction ────────────▶ (B) CAS relay  /v2/reconstructions/…  │
                             │      caches manifest, rewrites xorb url│
  xorb range GET ────────────▶ (C) Xorb store /xorbs/default/{hash} │
                             │      range cache, key=(hash,byterange) │
                             └──────────────────┬────────────────────┘
                                     size/HEAD  │  bytes (Range GET)
                                cas-server ◀────┤  miss → upstream
                                                ▼
        cas-server.xethub.hf.co (reconstruction + HEAD)   ·   us.aws.cdn.hf.co (xorb bytes)
```

> **Verified 2026-08 (materially changes §5):** there is **no whole-xorb-by-hash
> download**. Xorb *bytes* come only from the CDN signed URL as **`Range` requests**,
> authorized up to the reconstruction's declared byte range. CAS-server
> `GET /v1/xorbs/default/{hash}` returns **401** (HEAD works, returns size only).
> So the cache operates at **(hash, byte-range)** granularity, not whole objects.

---

## 3. Component A — Hub shim

Reverse-proxy for `huggingface.co`. Selected into workers via env injection (§9). Two routes matter; everything else passes through untouched.

### 3.1 Resolve  ✅ *(verified against live API 2026-08)*
`GET /{namespace}/{repo}/resolve/{rev}/{path}` → proxy upstream, **pass through unchanged**.

| Header | Action |
|---|---|
| `X-Xet-Hash` | pass through — the **only** Xet header on resolve; it's the `file_id` for reconstruction |

- **Correction:** resolve does **not** return `X-Xet-Cas-Url` / `X-Xet-Access-Token` (an earlier draft assumed it did). A HEAD/GET on resolve returns **302 + only `X-Xet-Hash`**. The CAS url and token come from the token route (§3.2) — that's the sole place to rewrite `casUrl`. ✅
- The 302 targets the legacy LFS blob route; the Xet client ignores it. The shim must **not** follow or collapse the redirect. ✅
- Absence of `X-Xet-Hash` ⇒ file is not Xet-backed (LFS/plain) ⇒ pure passthrough.
- Do not buffer the body.

### 3.2 Token  ✅ *(verified against live API 2026-08)*
`GET /api/{repo_type}s/{repo_id}/xet-read-token/{revision}` → proxy upstream, transform response **body**. This is the **only** rewrite point for the CAS url.
```json
{ "casUrl": "https://cas-server.xethub.hf.co", "exp": 1735689600, "accessToken": "xet_…" }
```
- **REWRITE** `casUrl` → your CAS relay. Pass `accessToken` + `exp` through unchanged.
- One token call serves all files of a `(repo, revision)`; the client reuses it across reconstructions.
- Only `read` tokens are relevant (uploads are out of scope). If a `write` token is requested, pass through untransformed — writes go direct to upstream.

---

## 4. Component B — CAS relay

Serves the reconstruction API to workers; this is where the immutable manifest is cached and xorb URLs are redirected to your store.

### 4.1 Reconstruction  ✅
`GET /v2/reconstructions/{file_id}` (fall back to v1 on 404/501), optional `Range: bytes={start}-{end}` (inclusive), `Authorization: Bearer xet_…`.

**Upstream response shape (v2):**
```json
{
  "offset_into_first_range": 0,
  "terms": [
    { "hash": "<xorb hash>", "unpacked_length": 263873, "range": { "start": 0, "end": 4 } }
  ],
  "xorbs": {
    "<xorb hash>": [
      { "url": "https://us.aws.cdn.hf.co/xorbs/default/<hash>?<CDN-signature params>",
        "ranges": [ { "chunks": { "start": 0, "end": 4 }, "bytes": { "start": 0, "end": 131071 } } ] }
    ]
  }
}
```
> ✅ *Verified:* url host is `us.aws.cdn.hf.co` (region CDN), path `/xorbs/default/{hash}`.
> The signature has **no** `X-Xet-Signed-Range` param — the client sends the byte
> window as a **`Range` header**, authorized up to `ranges[].bytes` (end-exclusive).
> A request beyond that window → **403**; within it → **206**.

**Relay logic:**
1. **Manifest cache** — key `file_id` (64-hex, byte-reversed-per-8 encoding, see §8). Store only the immutable part: `terms[]` + the xorbs→chunk-range mapping. **Never store the signed `url`s** (they expire). Immutable ⇒ no TTL, LRU only.
2. On miss, fetch upstream with the Bearer token; persist the immutable part.
3. **Rewrite every `xorbs[*][].url`** → `https://<your-xorb-store>/xorbs/default/{hash}`. Drop the CDN signature params — your store authorizes by being inside the LAN, or issues its own signature for defense-in-depth. Keep the client's `Range` header semantics intact.
4. Return transformed manifest.

**Servicing misses:** the client fetches xorb ranges *right after* reconstruction, sending a `Range` header. Keep a short-TTL side map `(hash, byte-range) → upstream signed CDN url` (≤ token lifetime) so the xorb store (§5) can replay that authorized range on miss without re-requesting reconstruction. The signed url authorizes up to the declared `bytes` window, so the store must fetch within it.

`offset_into_first_range` and `Range` handling: pass through to the client unchanged; sub-chunk alignment is the client's problem, not the cache's.

---

## 5. Component C — Xorb store

`GET /xorbs/default/{hash}` with a client `Range` header — the only route. Because
there is **no whole-xorb download** (verified — see §2 banner), both tiers cache at
**(hash, byte-range)** granularity. The tiers differ in *how ranges are keyed and
assembled*, not in a range-vs-whole split.

### Tier 1 — range passthrough cache (**SHIP THIS** — the product)

> The empirical study (`xet-cache-findings.md`, 2026-08) settled the tier
> question: for model serving, **all real dedup is whole-file identity** and
> Tier 1 captures it. Tier 2 (below) is **experimental/optional**.

- **Cache key:** `hash + Range` (the byte window), **ignoring** the CDN signature query params (they rotate per token). Normalize the Range header into the key. ✅
- **Miss:** replay the authorized range against the upstream signed CDN url from the relay side-map (§4.1); store, serve.
- **Hit profile:** N fresh identical workers (empty local `HF_XET_CACHE`) request byte-identical `(hash, range)` → hits. Kills scale-out tax.
- **Cross-revision dedup — yes, partially, *for free*:** an unchanged file re-resolves to the *same* xorb hash and *same* byte range across revisions, so `(hash, range)` collapses across revisions **once the ephemeral signature is normalized out of the key** — which is exactly what the shim does and a naive signed-URL cache cannot. Changed files get new xorbs (no reuse, correct).
- **Impl:** nginx/envoy with `proxy_cache_key` = normalized `hash` + `Range`, ignoring `Expires/Policy/Signature/Key-Pair-Id`.

### Tier 2 — coverage-map store (⚠️ EXPERIMENTAL / OPTIONAL — not for model serving)
> **Do not enable for stock vLLM model pulls.** The 2026-08 study
> (`xet-cache-findings.md`) measured its payoff at **zero** for this workload:
> weight files are immutable across revisions (~90 transitions / 6 repos → 100%
> identical) and share ~0% chunks across fine-tunes, so partial-overlap
> coalescing never fires. Kept behind `CACHE_TIER=2` (off by default) for
> **iterative-artifact** workloads — training checkpoints, appended datasets,
> GGUF requant of one base — where a large file changes incrementally. Revisit
> with `tools/fsck.py --dedup` before enabling.

Adds two things Tier 1 can't do: coalescing overlapping ranges, and integrity.
- **Cache key:** `hash`, with a **coverage map** of which byte ranges are present.
- **Miss fetch:** there is no whole-object GET (CAS-server GET → 401 ✅). Accumulate authorized ranges from the CDN url as clients request them; serve sub-ranges from the map, fetch-through gaps.
- **⚠️ The whole compressed xorb is NOT fully fetchable.** Verified 2026-08: every xorb has a **~42 KB chunk-index footer** beyond the last authorized range (`HEAD_size` − max `bytes.end` ≈ 42 KB across all sampled xorbs), and the CDN 403s any request past the signed window. So coverage **never reaches `[0, HEAD_size)`** and there is no "assembled whole xorb." `HEAD` size is informational only.
- **Integrity (fail-secure):** per-range **self-consistency** — BLAKE3 each fetched range at store time, re-hash on demand to catch on-disk corruption. This works for any cached bytes and needs no completeness. Verifying against the xet-core **MerkleHash** content-address was investigated (`xet_verify/`) and found **not achievable via the client API** (`X-Xet-Hash` is a server-side per-repo HMAC; range-verification anchors ship only in shards read tokens don't grant; xorb-whole needs the unreachable footer). See `xet-cache-findings.md §2`. Per-range self-consistency + end-to-end round-trip byte-identity are the integrity story.
- **Dedup gain over Tier 1:** when two files share a xorb but request *different* ranges, Tier 1 caches them as separate keys; Tier 2 coalesces. For whole-file reuse (the common revision-bump case) both dedup identically.
- **Eviction:** LRU by hash (whole xorb = `.data` + `.meta`). No invalidation (immutable).

---

## 6. Auth / token lifecycle  ✅
- Token is opaque `xet_…` bearer, **per-repo + per-revision**, short-lived (`exp`).
- Relay reuses the token the client presents for upstream fetches.
- Refresh via `X-Xet-Refresh-Route` with a **30s buffer** before `exp` (matches xet-core). Cache tokens keyed `(repo, revision)`.
- Cached xorbs serve **without** a live upstream token → offline / WAN-outage resilience for the hot set.

---

## 7. Reuse `xet-core`, don't reimplement  ✅/⚠️
HF's CAS **server is not open source** — you're first. But the client crates are public; wrap them:
- `cas_types` — reconstruction/response structs, avoids hand-rolling the JSON schema. ✅
- `cas_client` — reconstruction fetch + xorb range GET logic; use for the upstream-miss path so LZ4 framing, range packing, and retries come for free. ✅
- MerkleHash / chunking crate — for §8 verification. ✅
- ⚠️ Exact crate names/APIs drift; pin a commit and vendor. Verify `cas_client` exposes a whole-xorb fetch (else assemble from range GETs covering the full xorb).

---

## 8. Hash encoding gotcha  ✅
BLAKE3 MerkleHash = 32 bytes. The hex string in URL paths / `file_id` is **not** a straight byte→hex dump: each 8-byte group is **byte-reversed (little-endian u64)** before hex encoding. Replicate xet-core's encoder exactly or cache keys won't match upstream paths. Applies to file_id, xorb hash, chunk hash.

---

## 9. Deployment / env injection (no image change)
Inject at the worker/platform layer:
```
HF_ENDPOINT=https://<hub-shim>            # redirects resolve + token
# HF_HUB_DISABLE_XET must be UNSET/0      # opposite of current Strategy B
HF_XET_CACHE=/local/nvme/xet             # keep worker-local chunk cache on NVMe, never NFS
```
- The CAS url is rewritten in-band via the token-route `casUrl` (§3.2) — resolve carries no CAS url (§3.1) — so **no `HF_XET_CAS_URL` env override is needed**, good, because none is confirmed to exist. ⚠️
- Place all three components co-resident in-DC (ideally on/near the machines) to minimize the LAN hop.

---

## 10. Failure modes & fallback
| Failure | Behavior |
|---|---|
| Relay/store down | Hub shim passes the token-route `casUrl` **untouched** → client goes direct to upstream CAS. Degrades to today, no outage. |
| Xorb integrity mismatch | Reject, refetch once, then 502 → client retries / falls back. Never serve unverified bytes. |
| Token expired mid-fetch | Refresh via `X-Xet-Refresh-Route`; retry once. |
| Xet protocol version bump | Pin vendored `xet-core`; monitor `/v2` → `/v3`; v1 fallback path already wired. |
| Repo is LFS-only (non-Xet) | No `X-Xet-*` headers present → shim is a passthrough; existing Strategy B still applies. |

---

## 11. Milestones
1. **M1 — Skeleton (A + B) + Tier 1 store.** Passthrough-safe, rewrites CAS/xorb URLs, nginx range cache. Kills scale-out cold-start tax. *Exit: N-worker scale-out pulls once from WAN, rest from LAN.*
2. **M2 — Tier 2 coverage-map store (built; now EXPERIMENTAL/OPTIONAL).** Range coalescing + coverage map, per-range integrity, LRU. *Result: measured dedup payoff = 0 for model serving (`xet-cache-findings.md`) — weights are immutable across revisions and share ~0% chunks across fine-tunes. Off by default (`CACHE_TIER=2`); retained for iterative-artifact workloads only.*
3. **M3 — Hardening.** Token cache, offline serve, metrics (hit rate by hash, WAN bytes saved, p50/p99 cold start), placement-locality dashboard.

---

## 12. Open items

**Resolved by live probe (2026-08, via `study/scrape_xorb_map.py`):**
- ✅ **No whole-xorb GET.** CAS-server `GET /v1/xorbs/default/{hash}` → 401; `HEAD` → 200 with total size. Xorb bytes come only from the CDN signed url as `Range` requests. M2 must assemble via range coalescing (§5 Tier 2), not a single GET.
- ✅ **CDN host = `us.aws.cdn.hf.co`** (region-specific), path `/xorbs/default/{hash}`. Not `transfer.xethub.hf.co`.
- ✅ **Signature carries no `X-Xet-Signed-Range`;** the client sends a `Range` header, authorized up to `ranges[].bytes`. Beyond → 403, within → 206.
- ✅ **CAS url + token come from the `xet-read-token` route, not resolve** (§3.1/§3.2).

**Also resolved:**
- ✅ **No assembled whole-xorb.** Every xorb has a ~42 KB chunk-index footer past the last authorized range; the CDN 403s beyond the signed window, so coverage never reaches `HEAD_size`. Tier 2 integrity is therefore per-range self-consistency, not whole-object hashing (§5).

**Still open:**
- ⚠️ `cas_client` public surface for range fetch + LZ4 decode boundaries (still worth wrapping vs. hand-rolling the CDN Range GET).
- ⚠️ MerkleHash content-address verification over chunks (the footer is excluded) — the real upstream-integrity check; needs xet-core's CDC chunker.
- ⚠️ TLS/cert pinning: interception is header/URL-rewrite based (not DNS MITM), so pinning on `us.aws.cdn.hf.co` is **not** a blocker for the rewrite approach — but confirm the client honors the rewritten CDN host from the manifest.
- ⚠️ Whether the region CDN host varies by caller region (e.g. `eu.*`); the relay should treat the manifest url host as opaque and rewrite whatever it sees.
- Measure scheduler **placement locality** (see `study/placement-locality-measurement.md`) — host-local hit rate depends on new workers landing on hosts already serving the model.

---

## Sources
- CAS API: https://huggingface.co/docs/xet/en/api
- Download protocol: https://huggingface.co/docs/xet/en/download-protocol
- Auth: https://huggingface.co/docs/xet/en/auth
- File ID / resolve headers: https://huggingface.co/docs/xet/en/file-id
- Hashing / dedup: https://huggingface.co/docs/xet/hashing · https://huggingface.co/docs/xet/en/deduplication
- Client: https://github.com/huggingface/xet-core · https://github.com/huggingface/huggingface_hub/blob/main/src/huggingface_hub/constants.py
