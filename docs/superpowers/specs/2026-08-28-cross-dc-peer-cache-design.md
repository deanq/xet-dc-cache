# Cross-DC Peer Cache ("Tier 1.5") Design

**Date:** 2026-08-28
**Status:** Design — not yet planned or implemented
**Author:** brainstormed with Claude

## Problem

Each xet-cache host today runs an independent Tier 1 disk cache keyed on
`(xorb_hash, byte-range)` with the CDN signature normalized out. When a
serverless endpoint scales horizontally and a worker lands in a
**datacenter that has never pulled this model**, that first worker pays a
full cold pull from HF's Xet CDN over WAN — even though a *sibling* DC
already has every one of those xorbs warm on disk.

The idea: make caches **peer-aware** so a cold DC can pull warm bytes from a
sibling cache instead of the CDN.

## What this is (and isn't) worth

The cost being attacked is **cold-start latency**, not egress dollars (HF's
CDN egress is on HF's bill, not the operator's; moving bytes onto the private
backbone could even *add* cost). The bet is therefore narrow and precise:

> A sibling peer, reachable over the **private backbone**, delivers a xorb
> range to a cold DC *faster* than HF's Xet CDN serves that same DC.

This is credible **only** because sibling DCs share a low-RTT,
high-bandwidth private backbone (regime (a): peer ≈ LAN, CDN ≈ WAN). Absent
that backbone, a single peer host loses the throughput race to a global CDN
and the mesh is net-negative.

**The value is structurally capped** to the *first pull per (model, DC)*.
Tier 1 already serves every subsequent worker in a DC locally, so the mesh
only accelerates the one cold worker that first brings a model into a DC.
This is a one-time tax per (model, DC), not a recurring one.

Because the payoff is capped and backbone-dependent, the design **ships with
the metric that proves or kills it in production**:
`xet_peer_bytes_total` vs `xet_wan_bytes_total` = the fraction of cold-miss
bytes the mesh caught instead of the CDN. If that fraction is small, turn
peering off (it's a config flag). The frequency of cold (model, DC)
first-touches is the same quantity the placement-locality study (Step 2)
estimates; this design does not depend on that study, but the study would
size the upside in advance.

## Trust model

**Fleet-shared, single trust domain.** All fleet workers are one trust
pool. Any warm cache may serve any cold cache. There is no per-tenant
scoping and no entitlement handshake between peers. Model gating for
private/gated repos remains HF's responsibility, exactly as it already is
for a local Tier 1 HIT (which also serves cached bytes without re-checking
HF entitlement). Cross-DC peering widens the blast radius of that existing
property from one host to the fleet; it introduces no new *class* of risk
under this trust model.

Integrity is unchanged from today: the cache key is content-addressed, so
any peer's copy of a key is byte-identical to any other's — there is no
staleness or coherence problem. No content-address verification of xorb
bytes is possible via the client API (settled dead-end; see project
memory), so integrity rests on TLS / trusted backbone + the content-address
key, same as the CDN path.

## Architecture: peering as "Tier 1.5"

Peering slots in as a middle tier between local disk and the CDN.

Today:

```
local disk HIT  ->  serve
       miss     ->  singleflight -> CDN fetch (signed URL) -> cache -> serve
```

With peering:

```
local disk HIT  ->  serve                                    (unchanged)
       miss     ->  singleflight:
                      1. sticky peer GET, else fan-out probe  (new, best-effort)
                      2. on peer miss/timeout -> CDN fetch     (unchanged fallback)
                      -> cache to disk -> serve
```

### Three load-bearing principles

1. **Peering is strictly an accelerator, never a dependency.** Every peer
   failure mode — timeout, 404, connection refused, short read, wrong
   content-length — collapses to the CDN path. The CDN remains the
   correctness guarantee. Worst case, a request is `PEER_PROBE_TIMEOUT`
   slower than baseline; it can never *fail* a request the CDN would have
   served.

2. **Hit-or-404, no recursion.** A peer request carries `X-Xet-Peer: 1`,
   meaning "serve from your disk or 404 immediately — do **not** go to the
   CDN or to *your* peers on my behalf." Fetch-chain depth is capped at one
   hop. A cache never probes itself (self is filtered from the peer list).

3. **Reuse everything.** Serving is the existing `GET /xorb/xorbs/default/
   {xorb_hash}` handler + the peer header. Peer-to-peer auth is the existing
   `SHIM_AUTH_TOKEN` bearer (fleet-shared secret). No new serving protocol.

## Components

### Peer set (config)

- New `PEERS` env var: comma-separated base URLs
  (`https://dc1-host:8000,https://dc2-host:8000`). Absent/empty = peering
  disabled (feature-flag-by-absence, matching `SHIM_AUTH_TOKEN` /
  `MAX_INFLIGHT_FETCHES`).
- Parsed once at startup behind a small `PeerSet` interface
  (`Peers() []string`) so the source (static now, cloud service discovery
  later) can change without touching fetch logic.
- Self-exclusion: an optional `SELF_URL` is filtered from the list so a
  cache never probes itself.

### Fetch decision (miss path, inside the singleflight closure)

Only the flight winner probes/fetches; concurrent losers for the same range
wait on the one flight, as today.

1. **Sticky peer first.** One lightweight piece of state: the peer that most
   recently served us successfully, with a short TTL (`PEER_STICKY_TTL`,
   default ~60s — a model pull is a burst of thousands of ranges over
   seconds, so one preferred pointer covers the whole burst). If a live
   sticky peer exists, `GET` the range from it directly (peer header set);
   `206` -> serve + cache + refresh stickiness. Hot path during a cold
   pull: ~1 peer GET per range, zero probing.

2. **Fan-out probe on sticky miss.** No sticky peer (first range of a pull,
   or it went cold): parallel `HEAD` probes to all peers
   (`Range` + `X-Xet-Peer: 1` -> `200` has-it / `404` doesn't). First `200`
   wins; `GET` the bytes from it; set it sticky.

3. **CDN fallback.** No peer has it, or the probe budget was exceeded: the
   existing signed-URL CDN fetch, unchanged.

### Latency budget — the safety rail

- `PEER_PROBE_TIMEOUT` (default ~200ms, configurable) time-boxes the
  probe / sticky-GET attempt. Miss the budget -> abandon peer, go CDN.
- Bounds the worst case: a total peer-miss adds at most one probe-timeout to
  a request already facing a multi-second CDN pull. `PEER_PROBE_TIMEOUT`
  must stay << typical CDN fetch time.
- A peer transfer that stalls mid-body is aborted and the range is
  re-fetched cleanly from the CDN. The existing atomic-write logic
  (`writeCacheFileAtomic`) guarantees a partial peer body never lands on
  disk as a false HIT.

### Serving side (hit-or-404 mode)

- The existing `getXorb` handler gains a branch: when `X-Xet-Peer: 1` is
  set, on a local miss it returns `404` immediately and **never** invokes
  the CDN doer (no recursion, no signed-URL requirement — a peer won't have
  the signed URLs anyway).
- Serving a peer is otherwise identical to a local HIT (disk read).

## Observability

New series (both JSON `/metrics` and hand-rolled Prometheus text):

- `xet_peer_hits_total` — ranges served by a peer (win condition).
- `xet_peer_misses_total` — misses where no peer had it -> went CDN.
- `xet_peer_bytes_total` — bytes pulled from peers (CDN traffic displaced;
  the number that proves/kills the premise).
- `xet_peer_probe_timeouts_total` — probes that blew the budget (used to
  tune `PEER_PROBE_TIMEOUT`).

The decision metric is `xet_peer_bytes_total` / `xet_wan_bytes_total`.

Peer-origin requests are already logged by `withLogging` (they're normal
`GET`s); add a field so peer-origin serves are distinguishable in logs.

## Error handling

Errors are values; every peer error path returns to the CDN fallback and is
counted, never propagated to the client. Explicitly handled unhappy paths:

- Peer connection refused / DNS failure -> CDN.
- Peer probe/GET timeout (budget exceeded) -> CDN, increment
  `xet_peer_probe_timeouts_total`.
- Peer returns non-206 to a range GET -> CDN.
- Peer returns short read / content-length mismatch mid-body -> abort, CDN,
  nothing partial persisted.
- Empty `PEERS` -> peering path fully bypassed; behavior byte-identical to
  today.

## Testing

Matches the existing `httpDoer`-injection + table-test style; `go test
-race`.

**Unit — peer selection:**
- sticky-hit path serves from the sticky peer without probing.
- sticky-miss -> fan-out -> first-responder-wins.
- all-peers-404 -> CDN.
- self-exclusion (self URL never probed).
- empty `PEERS` -> peering fully bypassed, byte-identical to today.

**Unit — safety rail (unhappy paths):**
- peer timeout -> CDN fallback (fake peer `httpDoer` that hangs).
- peer short-read / mid-body abort -> CDN fallback + nothing partial on disk.
- probe-budget exceeded -> CDN.

**Unit — hit-or-404 serving:**
- `GET` with `X-Xet-Peer: 1` on a cached range -> 206.
- `GET` with `X-Xet-Peer: 1` on an uncached range -> 404 fast, asserting the
  CDN doer is **not** called (no recursion).

**Integration:**
- Two in-process `Server`s: A cold + B warm, A's `PEERS`=[B]. Assert A
  serves B's bytes, `xet_peer_bytes_total` moves, and killing B mid-flight
  falls A back to CDN.

## Configuration summary

| Env var | Default | Meaning |
|---|---|---|
| `PEERS` | (empty) | Comma-separated peer base URLs; empty = peering off |
| `SELF_URL` | (empty) | This cache's own base URL, filtered from `PEERS` |
| `PEER_PROBE_TIMEOUT` | ~200ms | Time box for probe / sticky-GET before CDN fallback |
| `PEER_STICKY_TTL` | ~60s | Lifetime of the preferred-peer pointer |

## Out of scope (deliberate, YAGNI)

- **Central tracker / registry** (approach B) and **gossiped Bloom filters**
  (approach C): later optimizations for large fleets. Both preserve this
  fetch path, so choosing broadcast-on-miss now is fully reversible.
- **Serve-side concurrency cap for peer requests:** peer serving reads whole
  bodies into memory like a local HIT; a warm peer hammered by many cold DCs
  could see memory pressure. Flagged for hardening, not v1.
- **Hedged requests** (racing peer against CDN): a possible latency
  refinement; not needed for v1.
- **Per-tenant scoping:** excluded by the fleet-shared trust model.
