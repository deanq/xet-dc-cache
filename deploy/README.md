# Deploying the shim to a DC host

Run the shim on one linux DC host as a supervised systemd service. This is
Step 1 of `docs/deployment-readiness-handoff.md` — the single-host canary. It
does **not** decide fleet topology (per-host vs shared); that's Step 3, gated
by the Step 2 locality study.

## What you get

- `../Makefile` target `make build-linux` → `xetcache-linux-amd64` (static,
  CGO-free, portable linux/amd64 ELF).
- `xet-dc-cache.service` — systemd unit: `Restart=always`, non-root user,
  `EnvironmentFile`, sandboxed.
- `xet-dc-cache.env.example` — annotated config template.
- `Dockerfile` — optional distroless image if the fleet is containerized.

## systemd install (bare-metal / VM host)

Run as root on the DC host. Build the binary on any machine with Go
(`make build-linux`) and copy `xetcache-linux-amd64` over, or build on the host.

```bash
# 1. Service account (no login, no home)
useradd --system --no-create-home --shell /usr/sbin/nologin xetcache

# 2. Binary
install -m 0755 xetcache-linux-amd64 /usr/local/bin/xetcache

# 3. Config — set PUBLIC_BASE to this host's LAN address:port (see below)
mkdir -p /etc/xet-dc-cache
install -m 0644 xet-dc-cache.env.example /etc/xet-dc-cache/xet-dc-cache.env
$EDITOR /etc/xet-dc-cache/xet-dc-cache.env      # PUBLIC_BASE is mandatory

# 4. Unit (StateDirectory/CacheDirectory are auto-created + chowned by systemd)
install -m 0644 xet-dc-cache.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now xet-dc-cache.service

# 5. Verify
systemctl status xet-dc-cache.service
curl -s localhost:8000/healthz          # {"status":"ok"}
curl -s localhost:8000/metrics | jq
```

Logs: `journalctl -u xet-dc-cache -f`.

### ⚠️ PUBLIC_BASE must be LAN-reachable

`PUBLIC_BASE` is baked verbatim into the URLs the shim hands back to clients
(the rewritten `casUrl` and every xorb URL). If it's `localhost`, a remote
worker will try to fetch xorbs from *its own* localhost and fail. Set it to the
address workers use to reach this host, including the port —
e.g. `http://10.0.0.5:8000`. Confirm from a worker: `curl http://10.0.0.5:8000/healthz`.

## Point workers at it

On each worker (no image changes needed):

```bash
export HF_ENDPOINT=http://<cache-host>:8000    # == PUBLIC_BASE
# leave HF_HUB_DISABLE_XET unset
```

## Docker (optional)

```bash
docker build -f deploy/Dockerfile -t xet-dc-cache .        # from repo root
docker run -d --name xet-dc-cache -p 8000:8000 \
  -e PUBLIC_BASE=http://<cache-host>:8000 \
  -e XORB_CACHE_MAX_GIB=100 \
  -v /var/cache/xet-dc-cache:/cache \
  xet-dc-cache
```

A per-host DaemonSet (if per-host wins in Step 2) wraps this image; that
manifest is deferred to Step 3 so it's shaped by the topology decision.

## Definition of done (Step 1)

- [ ] Binary runs under systemd on a linux DC host (`systemctl status` = active).
- [ ] Survives a kill: `systemctl kill -s SIGKILL xet-dc-cache` → auto-restarts.
- [ ] A worker with `HF_ENDPOINT` set downloads a model; a second pull shows a
      hit in `/metrics` (`hits` up, `wan_bytes_saved` > 0).

## Observability

- `GET /metrics` — JSON (human/debug; used by `make metrics` and acceptance).
- `GET /metrics/prometheus` — Prometheus text exposition (`xet_hits_total`,
  `xet_misses_total`, `xet_wan_bytes_total`, `xet_served_bytes_total`,
  `xet_wan_bytes_saved`, `xet_hit_rate`, `xet_cache_bytes`, …). Point a scraper
  here; both `/metrics*` and `/healthz` are exempt from auth. Cross-DC peering
  (Tier 1.5) adds: `xet_peer_hits_total`, `xet_peer_misses_total`,
  `xet_peer_bytes_total`, `xet_peer_probe_timeouts_total`; plus the adaptive-hedge
  series `xet_peer_hedge_fired_total`, `xet_peer_hedge_peer_won_total`,
  `xet_peer_hedge_cdn_won_total`, `xet_peer_bytes_wasted_total`, and the gauge
  `xet_peer_throughput_bytes_per_ms`. Compare `xet_peer_bytes_total` against
  `xet_wan_bytes_total` to judge whether peering is paying for itself. To tune
  `PEER_HEDGE_FACTOR`, watch the ratio
  `xet_peer_hedge_peer_won_total / xet_peer_hedge_fired_total` — the fraction of
  fired hedges the peer went on to win anyway (i.e. the CDN pull was wasted
  effort); a high ratio means the head start is too short. `xet_peer_bytes_wasted_total`
  reports the actual CDN bytes transferred before those losing pulls were
  cancelled (the real cost of the insurance), not the requested range size.
- Logs are structured JSON on stderr (slog): one `request` line per request with
  `status`, `x_cache`, `bytes`, `dur_ms`. Under systemd they land in the journal
  (`journalctl -u xet-dc-cache -o cat | jq`).

Suggested alerts: `xet_hit_rate` dropping, disk pressure on `CACHE_DIR`, and a
rising upstream error rate (4xx/5xx in the request logs).

## Security / trust boundary

**The shim sees and forwards client HF tokens** to the CAS server and hub, and
speaks plaintext HTTP. Treat it as a **trusted-LAN component**: only workers and
your scraper should be able to reach its port. Do not expose it to the internet.

Two knobs harden it within that boundary:

- `SHIM_AUTH_TOKEN` — optional shared secret that authenticates the **peer
  channel**. When set, peer-to-peer requests (`X-Xet-Peer: 1`) must carry
  `Authorization: Bearer <token>`; an unauthorized node cannot pull from or
  probe the fleet cache. It deliberately does **not** gate client traffic: under
  transparent interception a stock HF client only ever sends its own HF token in
  `Authorization` (which the shim forwards upstream), so it can never present the
  shim secret — gating client paths on it would 401 every real download. Client
  traffic is protected by network isolation, **not** this token. `/healthz` and
  `/metrics*` stay open.
- `MAX_INFLIGHT_FETCHES` — caps concurrent upstream miss fetches so a burst of
  distinct cold ranges can't exhaust host memory or hammer upstreams.

For TLS, terminate at a reverse proxy (nginx/Caddy) in front of the shim rather
than in the shim itself.

Cross-DC peering (`PEERS`) does not add a new trust boundary but does widen
the existing one: peer-to-peer requests carry the same `SHIM_AUTH_TOKEN`
bearer as client requests, a peer request (`X-Xet-Peer: 1`) is served
hit-or-404 and never triggers a CDN fetch or an onward peer fetch, and
peering is fleet-shared — a single trust domain where any listed cache may
serve any other. Do not enable it across a boundary you don't control (e.g.
across DCs owned by different teams or tenants).

## Config reference

All vars, defaults, and sizing notes are in `xet-dc-cache.env.example` and
`../CLAUDE.md` (Configuration).
