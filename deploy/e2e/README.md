# Cross-DC peering end-to-end test

Boots three real shim containers (`node-a`, `node-b`, `node-c`) that peer with
each other, and drives real `hf_hub_download`s through them against the live
HuggingFace CDN to prove the cache behavior end-to-end — cross-node peering and
cross-revision reuse.

## Run

From the repo root (needs Docker + network):

```bash
uv run deploy/e2e/run_e2e.py
```

Override the model (default `SmolLM2-1.7B-Instruct`, ~3 GB, downloaded a few
times):

```bash
SMOKE_REPO=org/model SMOKE_REV=main SMOKE_PATH=model.safetensors \
  uv run deploy/e2e/run_e2e.py
```

The script builds the image, starts the stack, runs the scenarios, prints a
PASS/FAIL summary, and tears the stack down.

## Architecture

Three shim containers peer over the Docker bridge; the driver on the host pulls
models *through* them and reads each node's `/metrics` to prove where the bytes
came from. Two URL spaces are in play at once (see "Networking model" below):
the client follows **host-facing** rewritten URLs, while the shims reach each
other by **container-facing** service name.

```mermaid
flowchart TB
    subgraph host["Host (macOS / CI runner)"]
        driver["driver: run_e2e.py<br/>runs hf_hub_download in a<br/>fresh subprocess per pull<br/>+ reads each node's /metrics"]
    end

    subgraph bridge["Docker bridge network"]
        na["node-a :8001<br/>xetcache shim"]
        nb["node-b :8002<br/>xetcache shim"]
        nc["node-c :8003<br/>xetcache shim"]
        na <-->|"PEERS / SELF_URL<br/>http://node-x:8000<br/>(HEAD probe + range GET)"| nb
        nb <--> nc
        nc <-->|peer channel| na
    end

    cdn["HuggingFace<br/>Xet CAS + CDN (WAN)"]

    ca[("caches/a")]
    cb[("caches/b")]
    cc[("caches/c")]

    driver -->|"HF_ENDPOINT +<br/>PUBLIC_BASE 127.0.0.1:800x"| na
    driver --> nb
    driver --> nc

    na -.->|"miss ⇒ WAN"| cdn
    nb -.->|"miss ⇒ WAN"| cdn
    nc -.->|"miss ⇒ WAN"| cdn

    na --- ca
    nb --- cb
    nc --- cc

    classDef node fill:#bcd8ff,stroke:#1f6feb,stroke-width:1.5px,color:#0b1f33;
    classDef ext fill:#ffd9a8,stroke:#bf6a00,stroke-width:1.5px,color:#3d2600;
    class na,nb,nc node;
    class cdn ext;
```

On a miss a shim first probes its peers over the private bridge (cheap LAN hop);
only if no peer has the xorb does it fall back to the WAN CDN. Per-node caches
are host bind-mounts (`caches/{a,b,c}`) so the driver can reset a node to "cold"
between scenarios.

## How the scenarios run

Each scenario reshapes cache/peer state, does one pull, and asserts on the
`/metrics` delta. State carries forward: scenario 2's peer hit works *because*
scenario 1 left `node-c` warm.

```mermaid
sequenceDiagram
    autonumber
    participant D as driver (host)
    participant A as node-a
    participant C as node-c
    participant W as HF CDN (WAN)

    Note over A,C: all caches reset → cold
    D->>W: ground-truth DIRECT pull (no shim) → record sha256

    Note over D,W: ── Scenario 1 — WAN fallback (node-c, peers cold) ──
    D->>C: pull model.safetensors
    C->>A: peer probe (HEAD) → miss
    C->>W: fetch xorbs
    W-->>C: bytes
    C-->>D: bytes (sha == truth)
    Note right of C: wan_bytes↑, peer_misses↑, peer_bytes 0<br/>node-c now WARM

    Note over D,C: ── Scenario 2 — peer warm hit (node-a cold, node-c warm) ──
    D->>A: pull model.safetensors
    A->>C: peer probe + range GET
    C-->>A: bytes over LAN
    A-->>D: bytes (sha == truth)
    Note right of A: peer_bytes↑, wan_bytes 0 (peer displaced WAN)

    Note over D,W: ── Scenario 3 — resilience (node-b/c stopped, node-a reset cold) ──
    D->>A: pull model.safetensors
    A-->>A: peer probe → peers unreachable
    A->>W: fall back to CDN
    W-->>A: bytes
    A-->>D: bytes (sha == truth)
    Note right of A: wan_bytes↑, peer_bytes 0<br/>node-a now WARM

    Note over D,A: ── Scenario 4 — cross-revision dedup (peers still down, node-a warm) ──
    D->>A: pull SAME file at a different, byte-identical revision
    A-->>A: reconstruct from cached xorbs (same Xet identity)
    A-->>D: bytes (sha == truth)
    Note right of A: hits↑, wan_bytes 0, peer_bytes 0
```

## What it asserts

1. **WAN fallback** — a download through `node-c` while every peer is cold pulls
   from the CDN (`wan_bytes` up, `peer_misses` up, `peer_bytes` == 0) and the
   bytes match a direct download.
2. **Peer warm hit** — a download through cold `node-a` once `node-c` is warm is
   served from the peer (`peer_bytes` up, and `peer_bytes >= wan_bytes`), still
   byte-identical.
3. **Resilience** — with `node-b`/`node-c` stopped and `node-a` cold, the same
   download still completes via the CDN (`wan_bytes` up, `peer_bytes` == 0):
   peering is an accelerator, never a dependency.
4. **Cross-revision dedup** — with peers still down and `node-a` now warm, a pull
   of the *same file at a different, byte-identical revision* is served from
   `node-a`'s own cache (`hits` up, `wan_bytes` == 0, `peer_bytes` == 0):
   unchanged files across model versions reconstruct from the same xorbs, so
   re-pulls at a pinned-but-unchanged revision cost zero WAN.

## Networking model

Two deliberately different URL spaces (see `docker-compose.yml`):

- **`PUBLIC_BASE`** is host-facing (`http://127.0.0.1:800x`) — the client (the
  driver on the host) follows the rewritten `cas`/`xorb` URLs, so they must
  resolve from the host.
- **`PEERS` / `SELF_URL`** are container-network-facing (`http://node-x:8000`) —
  the shims reach each other by compose service name over the bridge network.

Per-node caches are host bind-mounts under `caches/` (git-ignored) so the driver
can reset them between scenarios — the distroless image has no shell to `rm`
from inside.
