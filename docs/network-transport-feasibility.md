# Network transport feasibility: HTTP/3 QUIC peering & client↔shim tuning

_2026-09-28. Feasibility study + measured recommendations. Companion to
[`xet-cache-findings.md`](xet-cache-findings.md) (settled dead-ends)._

Two questions were asked:

1. Should peer↔peer transport move to **HTTP/3 QUIC**?
2. What **client↔shim** networking optimizations are worth employing?

Short answers: **(1) No** for the intra-DC peer path (weak-maybe only for a
lossy cross-DC path, which already has a cheaper mitigation); **(2) Yes — stream
cache hits**, a measured, network-independent win, plus host-level TCP tuning for
the peer path. QUIC was deliberately **not** built.

---

## Q1 — HTTP/3 QUIC for peer↔peer: no-go (intra-DC)

QUIC's advantages are already solved, irrelevant, or actively inverted by the
existing peer transport (`shim-go/peer_transport.go`).

| QUIC feature | Value for this workload | Why |
|---|---|---|
| 0-RTT / 1-RTT handshake | ~0 | The peer transport keeps a fat warm pool (`MaxIdleConnsPerHost=64`, `IdleConnTimeout=90s`) + optional keepalive. Connections are already warm → per-request handshake cost is already ~0. |
| No cross-stream head-of-line blocking | ~0 intra-DC | HoL blocking needs packet loss over a *multiplexed* connection. The design deliberately spreads N ranges across N **independent TCP connections** (`peer_transport.go` disables HTTP/2 on purpose), so ranges already can't HoL-block each other; a clean LAN has ~0 loss anyway. |
| One connection, many streams | **negative** | The design wants N independent congestion windows for aggregate throughput on a fat link. QUIC multiplexes onto **one** connection with **one** congestion controller → loses the parallel-flow ramp. Replicating with N QUIC connections discards QUIC's point and keeps only its costs. |
| Connection migration | 0 | Fixed DC peers. |

**Costs QUIC would add:** higher CPU/byte (userspace-over-UDP vs kernel
sendfile/GSO/TSO offload); **forced TLS** onto a component that deliberately runs
plaintext on the trusted LAN, plus fleet cert distribution; `quic-go` as the
first heavy third-party dep (the shim today depends only on `x/sync`); a new
QUIC/UDP server listener on every peer; and UDP buffer/GSO tuning to avoid
underperforming TCP.

**The one scenario QUIC helps** — a genuinely lossy, high-RTT cross-DC peer link
— is already handled more cheaply by the adaptive hedge (`peer_hedge.go`), which
races the CDN when a peer lags; and CDN egress is paid by HF, so hedged CDN bytes
are ~free to the operator.

**Verdict: do not build QUIC.** The better peer-path wins are TCP-level config
(next section), not a protocol rewrite.

## Q2 — client↔shim: stream cache hits (measured win)

The shim buffered every response body fully then wrote it in one shot
(`writeXorbBytes`: `os.ReadFile` → single `w.Write`). A throwaway loopback
micro-benchmark compared that against `os.Open` + `io.Copy` + a header flush,
serving a xorb-sized blob under a concurrent burst:

| conc × size | TTFB buffered → streamed | total buffered → streamed | **peak heap** buffered → streamed |
|---|---|---|---|
| 8 × 16 MiB | 5.4 ms → **0.19 ms** | 17.6 → 19.6 ms | 258 MiB → **2.8 MiB** |
| 8 × 64 MiB | 19.2 ms → **0.19 ms** | 72.3 → 75.5 ms | 1026 MiB → **3.3 MiB** |
| 32 × 16 MiB | 26.7 ms → **12.3 ms** | 76.5 → 82.6 ms | 932 MiB → **5.1 MiB** |
| 32 × 64 MiB | 98 ms (p95 **320 ms**) → **12.7 ms** (p95 33 ms) | 382 → **303 ms** | **3780 MiB** → **5.5 MiB** |

- **Peak heap ~200–700× lower**, and it's the DC-scale headline: buffered RSS
  scales as `MAX_INFLIGHT × range size` (the `server.go` worst-case note),
  measured 3.8 GB at 32×64 MiB; streamed stays flat ~5 MiB. **Network-independent.**
- **TTFB O(range size) → O(1)** — buffered must read the whole blob before the
  first byte; streamed emits after the `open`. On a real network hop this matters
  more, not less (earlier first byte → better pipelining).
- **Total wall doesn't regress**, and improves ~20% under load (buffered's
  multi-GB alloc/GC churn steals CPU/bandwidth).

**Shipped (default on):** `STREAM_CACHE_HITS` streams the **disk-HIT path** — the
warm case, the cache's whole point; a complete cached file is safe to `io.Copy`.
It defaults **on** (set `STREAM_CACHE_HITS=0` to force the legacy buffered path).
Both paths set `Content-Length` explicitly so wire framing is identical (identity,
not chunked). The MISS path (which would need a write-to-client-while-writing-to-
cache tee, interacting with the atomic-write invariant) is deliberately out of
scope. Validated end-to-end: the `make test-e2e` peering scenarios (local-cache-
hit and peer-served hit) are byte-identical through real `hf_xet` with streaming
enabled.

**Other client↔shim items:** add inbound server timeouts (`http.Server` sets
none — a slowloris/hardening gap); h2c/UDS are low-value (loopback is already
memory-fast; stock `huggingface_hub` won't speak them without client changes);
compression is N/A (weights are incompressible; `identity` is forced).

## TCP-level tuning for the peer path

The measurable peer-path wins are host/pod-level, not a protocol change:

- **Larger socket buffers** on high-BDP cross-DC links — the knob already exists
  (`PEER_SOCKET_BUFFER_BYTES`, `peer_transport.go`), defaulted off because a
  wrong static value *caps* throughput and modern autotuning beats a bad guess.
  On a fat, distant link it can matter; measure per-link before enabling.
- **BBR congestion control** — a host sysctl, not a shim change. Helps on lossy /
  high-RTT paths where CUBIC under-fills the pipe.

Both need host access (NET_ADMIN / sysctl); they cannot be applied from an
unprivileged serverless worker. The self-contained `netem_tcp.py` experiment
(`runpod_testbed/experiments/`) quantifies the socket-buffer × {cubic, bbr}
payoff on a synthesized high-BDP link.

## Instrumentation added

- `xet_heap_inuse_bytes` gauge (`prometheus.go`) — per-scrape Go heap, so a
  burst's peak RSS is observable. The report's "Peak shim heap" is the max across
  scrape samples (coarse: GC between 5 s scrapes can understate the true peak;
  use it as an A/B signal, not an absolute).
- Testbed A/B seam: `STREAM_CACHE_HITS` forwarded to pods; since streaming is now
  the default, compare a `STREAM_CACHE_HITS=0` (buffered) run against the default
  and diff peak heap.

## Live confirmations (Runpod, 2026-09-28 – 09-29)

Both runs on real Runpod hardware; all resources torn down and verified clear
(no lingering pods/volumes).

### At-scale streaming A/B (3 peered CPU pods, burst=8, 1 s scrape)

| run | peak `xet_heap_inuse_bytes` | workload realized |
|---|---|---|
| `STREAM_CACHE_HITS` off (buffered) | **4.0 GB** | 97/69/100% hit, 2.8 GB WAN, 7 cold jobs |
| `STREAM_CACHE_HITS` on (streamed) | **2.2 GB** | 88/78/81% hit, 8.4 GB WAN, 20 cold jobs |

Streaming cut peak heap **4.0 GB → 2.2 GB even though the streamed run did ~3× the
WAN miss-fetching** — the direction is confirmed on real hardware. Two honest
caveats: (1) the gauge captures the whole shim heap, so it also includes the
**MISS-path `io.ReadAll` buffers, which this change does not touch** — that
residual is why the streamed run still peaked at 2.2 GB, and it fingers the MISS
tee as the next memory win; (2) Flash cold-start counts vary run-to-run, so the
hit/miss mix isn't identical between the two runs. The *isolated* magnitude of the
serve-path effect is the loopback benchmark above (3.8 GB → 5.5 MB), where only
the serve path varied.

### Repeat demo A/B on the merged default-on build (2026-09-29)

A second same-binary A/B on the shipped `netfeas` image (streaming toggled
explicitly per arm), 3 peered CPU pods, burst=8, 1 s scrape:

| run | peak `xet_heap_inuse_bytes` | steady-state warm (exec-only) | workload realized |
|---|---|---|---|
| `STREAM_CACHE_HITS=0` (buffered) | **4.0 GB** | 0.39 s (39 warm) | ~100% peer-served, ~0 WAN, 12 cold |
| `STREAM_CACHE_HITS=1` (streamed) | **2.9 GB** | 0.38 s (48 warm) | 32% peer / 9.7 GB WAN, 15 cold |

The peer/WAN split (32% peer / 9.7 GB WAN vs ~100% peer) is **not caused by
streaming** — `STREAM_CACHE_HITS` only changes how a local disk-HIT is written to
the caller, which is downstream of the miss/peer/CDN decision in `getXorb` →
`fetchFromPeer` → the hedge. Peer% is governed by cross-pod cache-warmth timing:
a miss is peer-served only if a sibling already cached that `(hash, range)`,
otherwise the hedge's CDN pull wins and counts as WAN. The streamed arm had more
cold workers (15 vs 12) and more populate churn (6 vs 5 jobs), so more of its
misses fired before peers warmed → more WAN. Run-to-run Flash variance, not a
regression. (If anything streaming *helps* peering: a peer serving over the
streamed path emits its first byte in O(1), so it beats the hedged CDN more often.)

Corroborates the first run: **4.0 GB → 2.9 GB** peak heap, and again the streamed
arm carried the *heavier* miss load (9.7 GB WAN vs ~0) that inflates the residual,
so the true serve-path saving is larger than the 28 % headline. **Steady-state
latency is unchanged (0.38 vs 0.39 s)** — confirming streaming is a
time-to-first-byte and memory win, not a steady-state throughput one. (The
end-to-end "warm" median of 16–19 s in both arms is Flash queue + scale-out
overhead on ~48 concurrent tiny jobs, not the cache; the like-for-like number is
the 0.38 s steady-state.) The two on-hardware runs (4.0→2.2 GB, 4.0→2.9 GB) agree
on direction; the confound (whole-heap gauge + Flash variance) keeps the isolated
magnitude in the loopback benchmark.

### netem TCP-knobs

The probe pod could not shape its link: **`tc` returned `RTNETLINK answers:
Operation not permitted` — NET_ADMIN is denied inside a Runpod pod.** That denial
is itself the finding: socket-buffer sizing and BBR **cannot be applied from
inside the container** (shim or serverless worker). They are host/DC-operator
knobs. So the peer-path TCP-tuning recommendation stands, but it is an
infrastructure/host-config action, not a shim change — and it can only be
benchmarked where NET_ADMIN is granted (a bare-metal host or a privileged
environment), which `netem_tcp.py` is ready for.
