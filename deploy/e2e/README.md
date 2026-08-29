# Cross-DC peering end-to-end test

Boots three real shim containers (`node-a`, `node-b`, `node-c`) that peer with
each other, and drives real `hf_hub_download`s through them against the live
HuggingFace CDN to prove the peering behavior end-to-end.

## Run

From the repo root (needs Docker + network):

```bash
uv run deploy/e2e/run_e2e.py
```

Override the model (default `SmolLM2-135M-Instruct`, ~270 MB, downloaded a few
times):

```bash
SMOKE_REPO=org/model SMOKE_REV=main SMOKE_PATH=model.safetensors \
  uv run deploy/e2e/run_e2e.py
```

The script builds the image, starts the stack, runs the scenarios, prints a
PASS/FAIL summary, and tears the stack down.

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
