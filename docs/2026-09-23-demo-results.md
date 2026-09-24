# Demo results — 2026-09-23

Captured for the Friday demo. Two independent proofs that the DC-local cache
works end-to-end: a local Docker peering harness (`deploy/e2e/`) and a live
Runpod pods + Flash-serverless run (`runpod_testbed/`).

## 1. Local peering e2e — `make test-e2e`

- Model: `HuggingFaceTB/SmolLM2-1.7B-Instruct@main` :: `model.safetensors`
- File size: 2.78 GiB (2,981,435,345 bytes), sha256 `f55217be716b6a99…`
- Stack: 3 peered shim containers (`node-a/b/c`) vs the live HF CDN
- **Result: PASS (all 4 scenarios)**

| Scenario | Assertion evidence | Time |
|---|---|---|
| 1 — WAN fallback (node-c, peers cold) | `wan_bytes +2,981,435,345`, `peer_misses +57`, `peer_bytes 0`, bytes == truth | 42.5s (70 MB/s) |
| 2 — peer warm hit (node-a cold, node-c warm) | `peer_bytes +2,981,435,345`, `wan_bytes 0` (peer displaced WAN), bytes == truth | 16.8s (178 MB/s) |
| 3 — resilience (node-b/c down, node-a cold) | completed via CDN, `wan_bytes +2,981,435,345`, `peer_bytes 0` | — |
| 4 — cross-revision dedup (peers down, node-a warm) | rev `57aa3c6599` byte-identical, `wan_bytes 0`, `hits +57`, `peer_bytes 0` | 11.3s (263 MB/s) |

Timing summary (same 2.78 GiB file each pull):

```
WAN cold pull (s1):    42.5s (70 MB/s)
peer warm hit (s2):    16.8s (178 MB/s)   -> 2.5x faster than WAN
local cache hit (s4):  11.3s (263 MB/s)   -> 3.7x faster than WAN
```

> Local loopback overstates absolute LAN speed vs a real DC backbone; the
> direction — cache/peer >> WAN — is the point.

## 2. Live serverless run — Runpod pods + Flash endpoints

Full cold + warm-burst workload driven through 3 CPU cache pods via 3 Flash
CPU serverless endpoints (`xet-dl-A/B/C`). Overlap matrix `A={a,b}`, `B={b,c}`,
`C={c,a}` so every model lives on two pods (creates cross-pod peer hits).

Per-pod cache stats after the full workload (`drive` exit 0):

| Pod | req_xorb | served | wan | wan_saved | hits | misses | peer_bytes | effective_hit_rate |
|---|---|---|---|---|---|---|---|---|
| A | 189 | 9.369 GB | 0 | 9.369 GB | 189 | 0 | 0 | 1.00 |
| B | 75 | 2.930 GB | 2.657 GB | 0.273 GB | 9 | 59 | 0 | 0.13 |
| C | 320 | 5.146 GB | 0 | 5.146 GB | 9 | 98 | 4.636 GB | 1.00 |

- Pod A: fully warm-served (~9.4 GB from its own cache, hit_rate 1.0).
- Pod C: **4.636 GB pulled from a peer** over the private channel, zero WAN
  (cross-pod peering working on real infra).
- ~14.8 GB of WAN eliminated across the fleet.

### Serverless-bypass fix confirmation

Flash's base image ships `hf_xet 1.3.2`, which fetches Xet xorbs directly from
the CAS and bypasses the shim. The worker force-upgrades on first invocation;
a job returning the live versions + shim counters confirmed the fix:

```
status COMPLETED
versions {'hf_xet': '1.6.0', 'huggingface_hub': '1.32.0'}
req_* {'req_hub': 24, 'req_reconstruction': 9, 'req_xorb': 3,
       'served_bytes': 1245766, 'wan_bytes': 1245766, 'misses': 3}
```

`req_xorb > 0` means xorb bytes now flow through the shim (with 1.3.2 it was
`req_xorb 0`). See `runpod_testbed/README.md` (Architecture) and the memory note
`serverless-worker-bypasses-shim` (RESOLVED, commit 771a4a8).
