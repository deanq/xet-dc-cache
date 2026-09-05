# Peer Transfer Optimization — "Never Slower Than WAN"

**Date:** 2026-09-03
**Status:** Design — not yet planned or implemented
**Author:** brainstormed with Claude
**Supersedes:** the "Hedged requests (racing peer against CDN)" out-of-scope
line in `2026-08-28-cross-dc-peer-cache-design.md` ("not needed for v1"). This
design promotes that refinement to a v1 goal and specifies it.

## Problem

Tier 1.5 peering (the cross-DC peer cache) commits to a peer on the miss path
and only reaches for the CDN if the peer **misses or errors**
(`xorb.go` — sticky/fan-out peer GET, then CDN fallback). The `HEAD` probe
validates that a peer *has* the bytes; it says nothing about whether that peer
can *deliver* them faster than the CDN.

Two holes follow:

1. **A warm-but-slow peer wins the bet.** A cross-DC peer that accepts the
   connection then trickles the body can burn up to `PEER_FETCH_TIMEOUT_MS`
   (10 s default) before the shim gives up and falls to the CDN — *serially*.
   A CDN PoP in the client's own metro could have finished in a fraction of
   that. This is exactly the "WAN would have been faster" case, and today's
   code has no defense against it: the peer path can make a request **slower**
   than baseline, not just fail to accelerate it.

2. **Every peer transfer starts cold.** The shared `http.Transport`
   (`main.go`) leaves `MaxIdleConnsPerHost` at Go's default of **2** and does
   nothing to socket buffers. Under fan-out (many workers × many xorbs, plus
   the parallel fan-out `HEAD` probes) the shim opens fresh TCP connections
   that begin in slow-start. On a high-BDP cross-DC link a 64 MB range over a
   cold single stream never leaves slow-start — throughput dies on the
   connection setup, not on the wire.

## Goal & the honest guarantee

Maximize peer transfer throughput on a **mixed fleet** (some peers same-DC,
sub-ms RTT; some cross-DC over the private backbone, 10–80 ms RTT) and make it
**provably impossible for the peer path to leave the client slower than a
direct WAN pull**, beyond a bounded, configurable slack.

"Peer is *always physically faster* than the CDN" is **not achievable** and the
spec does not claim it: a CDN PoP in the client's metro beats a
cross-continent peer by speed-of-light, and no socket tuning changes that. The
achievable — and in practice stronger — guarantee is:

> **client latency ≤ WAN latency + `PEER_HEDGE_MAX_MS`**

because whenever the peer is losing, the CDN path is hedged in and raced. We
never *bet* on the peer; we give it a head start and race it.

## Cost model — corrected, and it matters

The Tier 1.5 design is explicit that the cost being attacked is **cold-start
latency, not egress dollars**: HF's Xet CDN egress is on **HF's** bill, not the
operator's, and moving bytes onto the private backbone can *add* operator cost.

This inverts the naive "don't double your egress" intuition:

- **Hedged CDN bytes are ~free to the operator** (HF pays CDN egress).
- **Peer bytes traverse the private backbone**, which the operator *may* pay
  for — the peer pull is the side with a marginal dollar cost, not the CDN
  hedge.

So the real brakes on firing a hedge are **not** operator egress dollars. They
are:

1. **HF-CDN load / good-citizenship** — redundant CDN requests we cancel still
   cost HF a signed-URL fetch and partial transfer; hammering their CDN with
   speculative doubles is antisocial and can trip rate limits.
2. **Peer serving effort** — a hedge that the peer ultimately loses still cost
   the peer a disk read + partial send.

The dial (`PEER_HEDGE_FACTOR`, `PEER_HEDGE_MAX_MS`) is therefore an
**HF-load-and-peer-effort vs latency** dial, not a dollar dial. Because CDN
bytes are free to the operator, the design can afford to hedge more
aggressively than a pure egress-cost model would suggest — the only reason not
to hedge on every request is (1) and (2) above.

The existing decision metric is unchanged: `xet_peer_bytes_total` vs
`xet_wan_bytes_total` still answers "is peering catching cold-miss bytes at
all." This design adds metrics for the *quality* of the race (below).

## Two independent mechanisms

The two pieces are separable and independently valuable. Mechanism A is a pure
throughput win with no behavior change to the fallback logic; mechanism B is
the latency guarantee. They ship together but are reviewed as distinct tasks.

### A. Maxed-out peer transport — the edge a CDN structurally cannot match

The one thing we can do that a CDN fundamentally cannot, because **we own both
ends**: never start cold.

- **Dedicated `http.Transport` for peer traffic**, separate from the CDN doer
  (`s.doer`). The CDN transport carries a fragile contract
  (`CheckRedirect → ErrUseLastResponse`, `DisableCompression`,
  `Accept-Encoding: identity`); peer tuning must not perturb it. Two
  transports, one `Server`.
- **HTTP/1.1 forced** on the peer transport: `ForceAttemptHTTP2: false` **and**
  `TLSNextProto: map[string]func(...)http.RoundTripper{}` (the empty non-nil
  map is what actually disables H2 upgrade). Rationale: HTTP/2 multiplexes
  every range onto **one** TCP connection = one congestion window + head-of-
  line blocking = a throughput disaster on a fat pipe. `https://` peer URLs
  (the env-example default) would otherwise silently negotiate H2. With H1.1,
  N concurrent ranges ride N independent congestion windows.
- **Fat idle pool:** `MaxIdleConnsPerHost: PEER_MAX_IDLE_CONNS_PER_HOST`
  (default 64), `MaxConnsPerHost: 0` (unbounded), `IdleConnTimeout: 90s`. A
  model pull is thousands of ranges; the pool must not thrash connections.
- **BDP-sized socket buffers (optional):** a custom `DialContext` sets
  `SO_RCVBUF`/`SO_SNDBUF` to `PEER_SOCKET_BUFFER_BYTES` when non-zero.
  **Default 0 = OS autotune.** Modern Linux `tcp_rmem`/`tcp_wmem` autotuning is
  excellent and a wrong static value *caps* throughput; the knob exists only
  for environments where autotuning is disabled. This is deliberately a knob,
  not a computed BDP — we do not know per-link RTT at dial time.
- **Connection keepalive (optional):** `PEER_KEEPALIVE_INTERVAL_MS` (0 =
  disabled) drives a background goroutine that issues a lightweight request
  (e.g. `HEAD /healthz` with the fleet bearer) to each peer to keep pooled
  connections open.

  **Honest limit of keepalive — do not over-claim it:** keeping a connection
  *open* avoids the TCP handshake + TLS on the next fetch. It does **not** keep
  the congestion window warm. Linux collapses `cwnd` back to the initial window
  after an idle RTO unless `net.ipv4.tcp_slow_start_after_idle=0`. So the
  goroutine's real, honest benefit is **handshake/TLS avoidance**; true window
  warmth is a **sysctl** (`tcp_slow_start_after_idle=0`), documented in the
  deploy notes, not something application code can force. We ship keepalive for
  the handshake win and document the sysctl for the window win.

**Mixed-fleet safety:** every item above is a **no-op on same-DC** (a handful
of idle sockets, OS-default buffers, cheap pings) and only uncorks the cross-DC
case. Nothing here degrades a same-DC peer.

### B. Adaptive hedged fetch — the never-slower-than-WAN guarantee

Replaces "abandon the peer at timeout, then serially try the CDN" with "give
the peer a head start, then race the CDN and take the winner."

**Drop the sticky-path HEAD probe.** `getXorb` already serves peer requests
**hit-or-404** (`X-Xet-Peer: 1` → local disk or immediate 404, no recursion).
So a bare peer `GET` that returns 404 *is* the miss signal — the separate
`HEAD` on the sticky path is a redundant round trip on an 80 ms link. Fold it
into the GET. The **fan-out** `HEAD` probe stays: it serves *discovery* (which
of N peers has the range) and firing N speculative GETs would multiply peer
load.

**The race, inside the singleflight closure (winner only):**

1. Fire the peer `GET` on the warm tuned transport (sticky peer, or fan-out
   winner).
2. Start an **adaptive timer**:
   ```
   predictedPeerMs = rangeSizeBytes / peerThroughputEWMA(peer)   // ms
   hedgeDelayMs    = clamp(predictedPeerMs * PEER_HEDGE_FACTOR,
                           PEER_HEDGE_MIN_MS, PEER_HEDGE_MAX_MS)
   ```
3. **Peer completes before the timer** → serve it. Zero CDN request, zero WAN
   cost, peer effort only. This is the steady-state common case once the
   transport is warm and the peer is healthy.
4. **Timer fires first** → launch the CDN `GET` (`fetchAuthorized`) **in
   parallel** with the still-running peer GET. **Take whichever completes
   first; cancel the loser** via `context.CancelFunc`. A cancelled peer body is
   discarded; a cancelled CDN body is discarded (counted as
   `peer_bytes_wasted`).

**Bootstrap (cold peer, no EWMA sample yet):** seed `peerThroughputEWMA` with a
conservative default so the first-ever fetch to an unknown peer hedges at
roughly `PEER_HEDGE_MAX_MS`; each completed peer transfer updates the EWMA and
tightens subsequent predictions. Never block waiting for a sample.

**Pathological peer (connects, then stalls mid-body):** the timer fires
regardless of peer state, the CDN GET joins, and — because the stalled peer
produces no bytes — the CDN wins. Worst-case client latency is
`hedgeDelayMs + cdnFetchMs ≤ PEER_HEDGE_MAX_MS + cdnFetchMs`. That
`PEER_HEDGE_MAX_MS` term is precisely the bounded slack in the guarantee, and
it is what replaces today's up-to-10 s serial stall.

**EWMA state:** per-peer `throughputEWMA` (bytes/ms), updated on every
completed peer GET: `ewma = α·sample + (1-α)·ewma`. Lives beside the sticky
pointer (`peers.go`), thread-safe, `α` a small constant (not user-facing).
Throughput (size-normalized), not raw latency, so a 64 MB range and a 1 MB
range on the same link produce comparable samples.

**Interaction with `singleflight`:** the race lives entirely inside the
existing flight closure. Concurrent callers for the same `(hash, range)` still
wait on the one flight; `misses`/`wan_bytes` are counted once inside,
`served_bytes` per caller outside, exactly as today. If the CDN wins the race,
it increments `wan_bytes` (a real WAN pull happened); if the peer wins,
`peer_bytes`, as today.

**Atomicity unchanged:** whichever body wins is written via
`writeCacheFileAtomic`; a cancelled/partial loser never touches disk. The
existing content-addressed key guarantees peer and CDN bodies for the same key
are byte-identical, so "take the winner" is safe — there is no coherence
question about *which* copy landed.

## Configuration (all new; defaults preserve current behavior)

| Env var | Default | Meaning |
|---|---|---|
| `PEER_MAX_IDLE_CONNS_PER_HOST` | 64 | Idle keep-alive conns kept warm per peer (was Go default 2) |
| `PEER_SOCKET_BUFFER_BYTES` | 0 | `SO_RCVBUF`/`SO_SNDBUF` on peer dials; 0 = OS autotune (recommended) |
| `PEER_KEEPALIVE_INTERVAL_MS` | 0 | Background ping interval to keep pooled conns open; 0 = disabled |
| `PEER_HEDGE_FACTOR` | 1.5 | Multiplier on predicted peer time before the CDN is hedged in |
| `PEER_HEDGE_MIN_MS` | 50 | Floor on the hedge delay (don't hedge instantly on tiny ranges) |
| `PEER_HEDGE_MAX_MS` | 1000 | Cap on the hedge delay = the guarantee's bounded slack |

Setting `PEER_HEDGE_MAX_MS` very large recovers today's "peer gets the whole
`PEER_FETCH_TIMEOUT_MS` before CDN" behavior; setting `PEER_HEDGE_FACTOR` → 0
approaches always-race (max latency insurance, max HF-CDN load). The defaults
target: a healthy peer never triggers the CDN; a peer running >1.5× its own
recent time, or slower than 1 s absolute, gets raced.

`PEER_FETCH_TIMEOUT_MS` is retained as the **hard ceiling** on any single peer
GET (belt-and-suspenders with the hedge).

## Observability (extends `metrics.go` counter bag + `prometheus.go`)

New series, in both JSON `/metrics` and Prometheus text (matching the
`xet_*_total` convention):

- `xet_peer_hedge_fired_total` — races where the CDN was hedged in (timer fired).
- `xet_peer_hedge_peer_won_total` / `xet_peer_hedge_cdn_won_total` — race outcomes.
- `xet_peer_bytes_wasted_total` — CDN body bytes discarded because the peer won
  after the CDN GET fired. The **true cost of the latency insurance** — watch
  this against `xet_peer_hedge_fired_total` to tune `PEER_HEDGE_FACTOR`.
- `xet_peer_throughput_bytes_per_ms` (gauge, per-peer if label support is
  added; else fleet-aggregate) — the EWMA, exposed for tuning.

Decision metric unchanged: `xet_peer_bytes_total` / `xet_wan_bytes_total`.

## Explicitly rejected (so it is not re-litigated)

- **QUIC / HTTP-3:** userspace stack, no kernel TCP offload; peaks **slower**
  than tuned kernel TCP on a clean private backbone, and a single QUIC
  connection is one congestion window unless you also multi-stream — at which
  point warm tuned HTTP/1.1 has already won. QUIC's edge (loss recovery,
  HoL-free multiplexing) is a loss-and-congestion story a clean backbone
  doesn't have.
- **RDMA / RoCE:** assumes backbone NIC + switch support, which **breaks the
  same-DC / mixed half of the fleet**. A second data path and a large lift for
  marginal gain over a warm tuned TCP stream.
- **Custom framed TCP/UDP bulk protocol:** reinvents congestion control,
  discards hit-or-404 semantics, the `SHIM_AUTH_TOKEN` gate, and all HTTP
  observability — for ~nothing over a pre-warmed HTTP/1.1 connection.
- **Splitting a single range across N parallel streams:** redundant. A model
  pull is already thousands of ranges saturating the warm pool; per-range
  striping adds complexity and coordination for a workload that is already
  wide.
- **Computing BDP and auto-sizing socket buffers:** RTT is unknown at dial time
  and varies per peer on a mixed fleet; OS autotuning does this better than a
  static guess. Left as a manual knob for autotune-disabled environments only.

## Testing

Matches the existing `httpDoer`-injection + table-test style; `go test -race`
mandatory (this is concurrency-heavy).

**Unit — transport construction:**
- peer transport is HTTP/1.1: `ForceAttemptHTTP2` false and `TLSNextProto`
  non-nil empty; `MaxIdleConnsPerHost` == configured value.
- peer transport is a *distinct* object from the CDN doer (tuning isolation).
- `PEER_SOCKET_BUFFER_BYTES=0` installs no custom buffer sizing; a non-zero
  value installs a `DialContext` that sets it (assert via a dial hook).

**Unit — adaptive hedge (the core, unhappy paths first):**
- fast peer finishes before hedge → CDN doer **never called**, `peer_bytes`
  moves, `xet_peer_hedge_fired_total` stays 0.
- slow peer (fake `httpDoer` that trickles past `hedgeDelay`) → CDN fired,
  CDN wins, `xet_peer_hedge_cdn_won_total` moves, peer GET cancelled.
- peer wins a race it triggered (finishes just after hedge fired, before CDN)
  → `xet_peer_hedge_peer_won_total` and `xet_peer_bytes_wasted_total` both move,
  CDN GET cancelled.
- stalled peer (connects, zero body) → CDN wins within
  `PEER_HEDGE_MAX_MS + cdnMs`; assert wall-clock bound holds (fake clock).
- bootstrap: first fetch to a peer with no EWMA hedges at ~`PEER_HEDGE_MAX_MS`.
- EWMA update: after a fast sample, predicted delay for the next same-size
  range drops.
- clamp: tiny range never hedges below `PEER_HEDGE_MIN_MS`; huge/slow never
  above `PEER_HEDGE_MAX_MS`.

**Unit — guarantee:**
- for a matrix of (peer fast / slow / dead) × (CDN fast / slow), assert
  observed client latency ≤ `min(peerMs, PEER_HEDGE_MAX_MS + cdnMs)` using a
  fake clock — the never-slower-than-WAN property, mechanically checked.

**Integration:**
- two in-process `Server`s (A cold → B warm): a deliberately throttled B
  triggers A's hedge and A serves CDN bytes without exceeding the latency
  bound; a healthy B serves with zero CDN calls.

## Rollout

- Every knob defaults to current behavior (fat idle pool is the only
  always-on change and is strictly a throughput improvement; buffers autotune;
  keepalive off; hedge caps generous). Ship dark, then tune
  `PEER_HEDGE_FACTOR` down using `xet_peer_bytes_wasted_total` /
  `xet_peer_hedge_fired_total` as the guide.
- Deploy note: document `net.ipv4.tcp_slow_start_after_idle=0` as the
  companion sysctl for the cross-DC window-warmth win.

## Out of scope (deliberate, YAGNI)

- **Per-peer Prometheus labels** if the hand-rolled exposition can't cheaply
  carry them — fleet-aggregate EWMA is enough to tune the dial initially.
- **Serve-side memory cap for hedged/peer serving** — inherited open item from
  the Tier 1.5 design; unchanged here.
- **Dynamic per-peer `PEER_HEDGE_FACTOR`** — one global factor + per-peer EWMA
  is sufficient; per-peer factors add config surface for little gain.
