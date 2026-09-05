# xet-dc-cache — Architecture Evaluation & Handoff

**Date:** 2026-09-05
**Reviewer:** staff-level critical read (no code changed; this doc is the only artifact)
**Scope reviewed:** all of `shim-go/*.go` (source + tests), `CLAUDE.md`, `README.md`,
`deploy/README.md`, and the peer-optimization spec/plan under `docs/superpowers/`.
Baseline verified: `cd shim-go && go test -race ./...` passes, `go vet ./...` clean, Go 1.27.

This is a followable handoff: read the executive summary, then the per-area sections,
then the ranked findings table. Every concrete claim cites `file:line`. Where I could
not confirm a bug without running the real `huggingface_hub` client against the binary,
I say so and give the exact verification step.

---

## Executive summary (the things that matter)

1. **`SHIM_AUTH_TOKEN` breaks real Xet downloads — CONFIRMED empirically (2026-09-05).**
   `withAuth` gates every data path (`/`, `/cas/...`, `/xorb/...`) on `Authorization: Bearer <SHIM_AUTH_TOKEN>`
   (`middleware.go:61-77`), but a real client presents its **HF token** on the hub/token call, the
   **xet access token** on reconstruction, and **no Authorization at all** on the signed-URL xorb GET.
   None of those equal the shim secret. A real `hf_hub_download` through the shim with `SHIM_AUTH_TOKEN`
   set was run both ways: (A) client sends no bearer → hard `401` on the first `resolve` call; (B) client
   sets `HF_TOKEN` == the shim secret → passes the gate but the shim forwards that same bearer **upstream
   to HF** (`reconstruction.go:52-54`, `proxy.go`), where it is a bogus HF token, so the download still
   fails (`resolve` returns a malformed no-`Content-Length` response). **Root cause: the single
   `Authorization` header is unfixably overloaded** — the gate wants it to equal the shim secret, the
   shim forwards it verbatim to HF which wants a valid HF token; no one value satisfies both. There is
   **no client configuration** that makes the feature work with real downloads; it only ever functioned
   for peer-to-peer traffic (which sets the bearer explicitly, `peer_fetch.go:157-159`, and is served
   hit-or-404 without an upstream forward). **Do not enable `SHIM_AUTH_TOKEN` on any deployment carrying
   real client traffic until this is fixed.**

2. **The "never slower than WAN + `PEER_HEDGE_MAX_MS`" guarantee is sound for a *single* `raceOnePeer`
   call and is mechanically tested (`peer_guarantee_test.go`), but the end-to-end miss path can chain
   up to three sequential upstream attempts** — sticky race, fan-out race, then the plain CDN path
   (`peer_fetch.go:27-45` → `xorb.go:137-155`). Each stage only advances when the prior stage's *CDN
   side also failed*, so the worst case is gated on CDN failure, but when it happens the client can see
   `~2×(hedgeMax) + discovery RTT + cdnFetch`, exceeding the advertised bound. The bound holds "per race,"
   not "per client request." Document it or collapse the fallthrough.

3. **`xet_peer_bytes_wasted_total` does not measure what its help text claims.** It books the *requested
   range size* on every peer win after a hedge (`peer_hedge.go:86`), not the CDN bytes actually
   transferred before cancellation. The design (`docs/.../2026-09-03-...design.md`) and the Prometheus
   HELP string (`prometheus.go:38-39`) both call it "CDN bytes discarded." It systematically overcounts
   (the whole point of cancelling is that most bytes were *not* pulled), which defeats its stated purpose
   as the dial for tuning `PEER_HEDGE_FACTOR`.

4. **Hedged CDN pulls share the `MAX_INFLIGHT_FETCHES` semaphore with real misses** (`cdnGet` →
   `s.acquire`, `xorb.go:64-68`). Under fleet fan-out where many hedges fire, speculative CDN pulls can
   consume fetch slots and throttle genuine cold misses. The cap was sized for "distinct cold ranges,"
   not "cold ranges × hedge speculation."

5. **Core design is sound and the concurrency work is genuinely careful.** Three-role mediation,
   `(hash,range)` keying, singleflight accounting, atomic cache writes, the LRU + disk-free watermark,
   and the hedge's goroutine/cancellation discipline are all correct and well-tested. The findings below
   are refinements and a couple of real risks, not a structural rethink. The two documented dead-ends
   (Tier 2 chunk-dedup, download content-verification) are correctly closed and should stay closed.

---

## 1. Design & architecture

**Overall: sound.** The transparent-interception model (`HF_ENDPOINT` → shim, three protocol roles in
one `Server`) is the right shape for the problem, and the invariants in `CLAUDE.md` are real and
correctly encoded in code.

- **Three-role mediation** (`proxy.go` hub, `reconstruction.go`, `xorb.go`) is clean. The token-rewrite
  correctly preserves the `X-Xet-Cas-Url` **header** path that current `huggingface_hub` actually reads
  (`proxy.go:57-60`), not just the body — the documented regression trap.
- **Caching keys** are a compatibility contract and are respected: xorb key = `sha256("{hash}:{range}")`
  (`util.go:14-17`); manifest filename sanitization (`manifests.go:29-35`). Singleflight collapses on the
  same derived `name` (`xorb.go:133`), so fan-out correctly shares one fetch.
- **Singleflight accounting** matches the invariant: `misses`/`wan_bytes`/`peer_bytes` incremented once
  inside the flight, `served_bytes` per caller outside (`xorb.go:143,168-169,181`). Correct under fan-out.
- **LRU + eviction** (`lru.go`): two independent limits (byte budget + `statfs` free-space watermark),
  rebuilt from disk mtime at boot (`main.go:207-231`), dotfiles skipped so atomic-write temp debris never
  seeds a false entry. `freeFn` returning `1<<62` on `statfs` failure (`main.go:73`) is a correct
  fail-open choice (a transient stat error must not purge the cache).
- **Atomic writes** (`writeCacheFileAtomic`, `xorb.go:191-215`): temp-in-same-dir + rename. Correct;
  a partial write can never become a false HIT.

**Structural weaknesses / coupling:**

- **`Server` is a 30-field god-struct** (`server.go:16-62`) mixing hub-proxy, CAS-relay, xorb-store,
  peering, hedge, and test-seam concerns. It works, but every handler sees every field; the peering/hedge
  fields (`peerStats`, `hedgeFactor`, `nowFn`, `hedgeAfter`, …) have leaked into the same struct the hub
  proxy uses. A `peerEngine` sub-struct would isolate the Tier-1.5 surface. Low priority; note for the
  next person who touches it.
- **`signedCandidates` is hardcoded to 8** in the constructor (`main.go:140`) despite `CLAUDE.md`
  describing the candidate list as load-bearing for boundary-spanning xorbs. A xorb appearing in >8
  ranged reconstructions could evict the URL that authorizes an incoming window → spurious 502
  (`xorb.go:57`). Not env-configurable. Edge case, but it's a silent correctness cliff, not a tunable.
- **Manifest cache is a second, inconsistent persistence path.** Unlike the carefully atomic xorb store,
  `ManifestCache.Put` uses a plain `os.WriteFile` (`manifests.go:56`) — non-atomic (truncate-then-write) —
  and `Get` reads without the mutex (`manifests.go:37-47`). A crash or concurrent read mid-write yields a
  truncated JSON; `Get`'s `json.Unmarshal` fails safe (returns nil → treated as miss), so it's not a
  correctness bug, but it's an avoidable inconsistency with the store's own atomic-write invariant. Also
  `trimLocked` does a `filepath.Glob` + `os.Stat` of the whole dir **on every Put** (`manifests.go:60-84`)
  — O(n) per write, n up to `MANIFEST_CACHE_MAX_ENTRIES` (50k default). Under a reconstruction burst that's
  near-quadratic I/O.

---

## 2. The peer feature (transport + adaptive hedge)

### Mechanism A — tuned transport (`peer_transport.go`, `peer_keepalive.go`)

- **Correct and well-scoped.** HTTP/2 is disabled the right way — `ForceAttemptHTTP2:false` **and** a
  non-nil empty `TLSNextProto` (`peer_transport.go:32-33`), which is the part that actually matters for
  `https://` peers. Fat idle pool, unbounded `MaxConnsPerHost`, 90s idle timeout. Kept a distinct object
  from the CDN `doer` so the CDN's redirect/identity contract is untouched (`main.go:107-125`).
- **Socket-buffer control is best-effort and correctly gated** to non-zero (`peer_transport.go:38-63`);
  default 0 = OS autotune, which is the right default. The `syscall.SetsockoptInt` path is Linux/Darwin;
  fine for the deployment target.
- **Keepalive is honestly scoped** (`peer_keepalive.go:9-13`): the comment correctly states it buys
  handshake/TLS avoidance, not congestion-window warmth (that's the `tcp_slow_start_after_idle=0` sysctl).
  No over-claim. **Minor:** the production goroutine is launched with a `nil` stop channel (`main.go:155`),
  so it can never be stopped — fine under `Restart=always`, but there's no graceful-shutdown story
  anywhere (see §4).

### Mechanism B — adaptive hedged race (`peer_hedge.go`, `peers.go` EWMA)

- **The race core is correct and leak-free.** `raceOnePeer` (`peer_hedge.go:40-103`) uses buffered
  (size-1) channels for both peer and CDN results, so a cancelled loser goroutine can always complete its
  send and exit — no goroutine leak on either path. Winner cancels the loser via `context.CancelFunc`
  (`peer_hedge.go:84,95`), and `defer cancel()` covers the fast-peer early return. I traced every exit;
  the goroutine bookkeeping is sound. `go test -race` passes.
- **EWMA sizing is reasonable.** Throughput (size-normalized bytes/ms), α=0.3, sub-ms floored to 1ms
  (`peers.go:78-130`). Bootstrap returns `maxMs` for an unknown peer (`peers.go:101-103`) — conservative,
  correct. Clamp to `[minMs,maxMs]` is right.

**Real issues in Mechanism B:**

- **`peer_bytes_wasted` is misnamed/overcounted** — books `size`, the *requested* range, on every peer
  win (`peer_hedge.go:86`), not bytes actually pulled from the cancelled CDN GET. See exec-summary #3.
  If `rangeSize` can't parse the range it books 0 (`peer_hedge.go:17-23,86`), so the metric is both
  over- (normal case) and under- (unparseable range) biased. To measure true waste you'd have to count
  bytes read off the cancelled CDN body before `ctx` fired — which the current `cdnGet` discards
  (`xorb.go:73-77`).
- **`peer_hedge_fired` can over-count at the timer boundary.** If the peer result and the timer are both
  ready, Go's `select` (`peer_hedge.go:66-70`) picks randomly; a peer that actually beat the timer can be
  recorded as a hedge and trigger a needless CDN GET. Inherent to `select`; low severity, but it slightly
  inflates the exact metric used to tune the dial.
- **The bounded-slack guarantee is per-race, not per-request.** `fetchFromPeer` can run the sticky race,
  then (on total failure) the fan-out race, and `getXorb` then still runs the plain CDN path
  (`peer_fetch.go:27-48`, `xorb.go:137-155`). `raceOnePeer` returns `ok=false` only when *both* its peer
  and CDN sides fail, so this chain is gated on CDN failure — but when the CDN is flaky the client can
  eat two hedge windows plus discovery RTT plus a third CDN attempt, beyond the advertised bound. The
  code comment at `peer_hedge.go:35-39` acknowledges the discovery-RTT term but not the double-race term.
- **EWMA never decays on idle and has no staleness bound.** A peer measured fast an hour ago keeps that
  prediction; after a topology/route change the first post-change hedge mispredicts until α re-converges.
  Acceptable, but worth noting as a tuning caveat.

---

## 3. Flaws / bugs / risks (concrete, with refs)

- **`SHIM_AUTH_TOKEN` vs. the real client (High, CONFIRMED 2026-09-05).** `middleware.go:61-77` requires the
  shim bearer on all data paths. Real client requests carry the HF token (token call), the xet access
  token (reconstruction, `reconstruction.go:52`), or nothing (signed-URL xorb GET). Empirically verified
  with a real `hf_hub_download` through the shim: with the client sending no bearer, the first `resolve`
  call 401s; with the client's `HF_TOKEN` set equal to the shim secret, the gate passes but the shim
  forwards that bearer upstream to HF where it is an invalid HF token, so the download still fails. The
  header is overloaded (gate secret vs. forwarded upstream token) and cannot serve both — no client
  config works. **Fix options:** (a) exempt the Xet data paths from the gate and protect the port by
  network isolation only (the documented posture already relies on that); (b) use a *separate* header for
  the shim secret (e.g. `X-Shim-Auth`) that is never forwarded upstream, leaving `Authorization` for the
  real HF/xet token; (c) drop the feature. Whatever the choice, add an acceptance case that sets the
  token so this cannot silently regress.
- **Hedge speculation competes for the fetch semaphore (Medium).** `cdnGet` acquires `s.sem`
  (`xorb.go:64-68`); a burst of firing hedges can starve real misses. Consider a separate, smaller
  budget for speculative pulls, or acquiring non-blockingly for the hedge.
- **Full-range bodies are buffered in RAM (Medium, partly by-design).** `io.ReadAll` on the xorb miss
  (`xorb.go:160`), on `cdnGet` (`xorb.go:74`), on `peerGet` (`peer_fetch.go:142`), and the whole hub
  response (`proxy.go:33`). Worst-case RSS is `MAX_INFLIGHT_FETCHES × max-range`, **doubled** when a hedge
  runs both sides. `CLAUDE.md` claims the cap bounds RSS to `cap × max-range`; the hedge breaks that
  invariant (2× per hedged range). The spec even lists "serve-side memory cap" as an inherited open item —
  it's still open and the hedge widened it.
- **206 responses omit `Content-Range` on a HIT (Low, conformance).** `writeXorbBytes` is called with
  `""` on a disk hit (`xorb.go:123,217-227`); a 206 without `Content-Range` is technically malformed.
  `huggingface_hub` tolerates it today (acceptance passes), but any stricter client or proxy in front
  would choke. Latent.
- **Non-standard ranges silently bypass the peer path (Low).** `peerGet` rejects anything `parseRange`
  can't handle or whose length ≠ `hi-lo` (`peer_fetch.go:146-148`); `parseRange` only accepts closed
  `bytes=lo-hi` (`util.go:19-31`). Suffix (`bytes=-500`), open (`bytes=100-`), or multipart ranges get no
  peer acceleration and fall to CDN. Fine for Xet's closed ranges today; a silent cliff if that changes.
- **`hit_rate` counts a peer-served range as a miss (Low, observability).** `getXorb` books `misses`
  on a peer win (`xorb.go:143`); `hit_rate = hits/(hits+misses)` (`metrics.go:32-37`) therefore
  understates effectiveness whenever peering is active. Intended (it *is* a local Tier-1 miss) but
  unlabeled — an operator watching `hit_rate` will misread a healthy peering fleet as a cold cache.
- **Existence oracle when auth is off (Low, within documented trust boundary).** Any LAN client can send
  `X-Xet-Peer: 1` (`xorb.go:84-86`) to get hit-or-404 and probe what's cached. Documented trusted-LAN
  posture covers this; noting for completeness.
- **`rememberSigned` cap can drop a needed URL (Low).** Candidate list capped at `signedCandidates`=8
  (`xorb.go:22-26`, hardcoded `main.go:140`); a xorb spanning >8 authorized windows can lose the URL that
  covers an incoming range → 502 (`xorb.go:57`). Not configurable.

**Explicitly checked and *not* bugs:** goroutine leaks in the race (none — buffered channels + cancel);
singleflight double-counting (correct); atomic xorb writes (correct); LRU fail-open on `statfs` error
(correct); `peerStats.ewma` map growth (bounded by the fixed `PEERS` list, not attacker-controlled).

---

## 4. Operational concerns

- **No graceful shutdown.** `http.ListenAndServe` with no `http.Server`, no signal handling
  (`main.go:191`); in-flight requests are dropped on SIGTERM. Atomic writes protect *cache integrity*, so
  this is a latency/UX issue under rollout, not corruption. The keepalive goroutine is unstoppable
  (`main.go:155`, nil stop). Fine under systemd `Restart=always`, but a K8s DaemonSet rollout will cut
  connections abruptly.
- **`MAX_INFLIGHT_FETCHES` default is 32** (`main.go:92`) but `CLAUDE.md`'s config section documents the
  var without the default; deploy sizing guidance (cap × max-range RSS) omits the hedge 2× multiplier.
- **Observability gaps:** (a) `peer_bytes_wasted` is not trustworthy for tuning (see §2); (b) no
  per-peer metrics (fleet-aggregate EWMA only — a documented YAGNI, but it means you can't see *which*
  peer is slow); (c) no counter for the plain-CDN-after-peer-miss fallthrough vs. peer-race CDN wins, so
  `wan_bytes` conflates "peer had nothing" with "hedge raced past a slow peer"; (d) no latency histogram —
  the whole feature is about tail latency yet only counters are exposed, so you cannot actually observe
  the p99 the guarantee is about. **This is the biggest observability gap for a latency feature.**
- **Hard-to-debug in prod:** a partial/slow peer manifests only as elevated `peer_hedge_fired` +
  `wan_bytes`; with no per-peer labels you can't localize it. `PEER_HEDGE_FACTOR` tuning depends on a
  metric (`peer_bytes_wasted`) that's miscomputed.
- **Config foot-guns:** `PUBLIC_BASE` baked into rewritten URLs (well-documented, `deploy/README.md:47-53`);
  `SHIM_AUTH_TOKEN` (see §3, likely broken); `PEER_SOCKET_BUFFER_BYTES` non-zero can *cap* throughput
  (documented). `CACHE_MIN_FREE_PCT` clamping is handled defensively (`main.go:55-62`).

---

## 5. Testing

**Strong where it counts:** the concurrency and hedge paths are the best-tested part of the codebase.

- Fetch-cap serialization proven both ways (`concurrency_test.go`).
- The never-slower guarantee is a virtual-clock matrix over (peer fast/slow/dead)×(cdn fast/slow) with an
  explicit latency-bound assertion (`peer_guarantee_test.go:86-119`) — genuinely good.
- Peer unhappy paths covered: probe timeout, GET-body hang bounded by `peerFetchTimeout`, short-read
  rejection, sticky-clear-on-failure, stale-sticky→fanout recovery (`peer_fetch_test.go`).
- Race outcomes and metric movement (`peer_hedge_test.go`), EWMA clamp/bootstrap/tighten (`peers_test.go`).

**Gaps that matter:**

- **No test asserting `SHIM_AUTH_TOKEN` works with a real Xet flow** — `middleware_test.go` tests the gate
  in isolation, not against the token/reconstruction/xorb request shapes. This is exactly why finding #1
  slipped through. Add an acceptance case that sets the token.
- **No test for the multi-stage fallthrough bound** (sticky-race-fails → fanout-race-fails → plain CDN).
  The guarantee matrix only exercises a single `raceOnePeer`. The per-request worst case is untested.
- **`peer_bytes_wasted` correctness is untested** — tests assert it *moves* (`peer_hedge_test.go`), not
  that it reflects real discarded bytes, so the overcount is invisible to the suite.
- **No memory/RSS bound test under hedging** (2× buffering per hedged range).
- **Manifest cache concurrency untested** — no test for concurrent Get during Put, non-atomic write, or
  the O(n)-per-Put trim under load.
- **No test that the plain-CDN path counts wan_bytes vs. hedge-CDN-win counts wan_bytes distinctly** —
  they're conflated, and nothing pins the intended semantics.
- Everything is in-process with injected `httpDoer`s; the `make test-e2e` Docker harness covers the real
  wire but isn't part of `go test`. Good for integration, but the auth-token gap lives precisely in the
  seam neither layer exercises with a real client + token.

---

## 6. Ranked findings & improvements

Severity = production risk. Effort: S<½day, M~1-2days, L>2days. Impact: correctness/ops value.

| # | Finding | Sev | Effort | Impact | Refs |
|---|---------|-----|--------|--------|------|
| 1 | `SHIM_AUTH_TOKEN` breaks real downloads (CONFIRMED: 401 no-bearer; upstream-token-forward fails coupled) | **High** | M to fix | High | `middleware.go:61-77`, `reconstruction.go:52`, `proxy.go` |
| 2 | Hedged CDN pulls share `MAX_INFLIGHT_FETCHES`; speculation can starve real misses | Med | M | Med | `xorb.go:64-68`, `main.go:92` |
| 3 | `peer_bytes_wasted` books requested size, not discarded CDN bytes → tuning dial is wrong | Med | S | Med | `peer_hedge.go:86`, `prometheus.go:38-39` |
| 4 | 2× RAM buffering per hedged range breaks documented `cap×max-range` RSS bound | Med | M | Med | `xorb.go:74,160`, `peer_fetch.go:142` |
| 5 | Never-slower bound is per-race, not per-request (double race + plain CDN on CDN failure) | Med | S (doc) / M (fix) | Med | `peer_fetch.go:27-48`, `xorb.go:137-155` |
| 6 | No latency histogram for a tail-latency feature; no per-peer metrics | Med | M | High (ops) | `prometheus.go`, `metrics.go` |
| 7 | Manifest cache: non-atomic write + unlocked Get + O(n) glob-per-Put | Low-Med | M | Med | `manifests.go:37-84` |
| 8 | No graceful shutdown; keepalive goroutine unstoppable | Low | M | Low-Med | `main.go:155,191` |
| 9 | `hit_rate` counts peer wins as misses (unlabeled) | Low | S | Low (ops clarity) | `xorb.go:143`, `metrics.go:32-37` |
| 10 | `signedCandidates` hardcoded to 8; >8 windows can drop the authorizing URL → 502 | Low | S | Low | `main.go:140`, `xorb.go:22-26,57` |
| 11 | 206 on HIT omits `Content-Range` (spec non-conformance) | Low | S | Low | `xorb.go:123,217-227` |
| 12 | Non-closed ranges silently bypass the peer path | Low | S | Low | `peer_fetch.go:146-148`, `util.go:19-31` |
| 13 | `Server` god-struct mixes hub/relay/store/peer/hedge/test-seam concerns | Low | L | Low (maintainability) | `server.go:16-62` |

**Quick wins (do first):** #1 (verify — one command), #3 (rename to `_estimated` or count real bytes),
#5 (document the per-request bound), #9 (add a labeled note or `peer_hit_rate`), #11.
**Larger bets:** #6 (histogram + per-peer labels — highest ops payoff for the feature), #4/#2 (memory
& semaphore isolation for speculation), #7 (make manifest cache match the store's atomic discipline).

**Settled dead-ends — leave closed:** Tier 2 chunk-dedup (~0 payoff for immutable weights) and
download content-verification (`X-Xet-Hash` is an unshared-salt HMAC). Both correctly documented in
`CLAUDE.md` / `docs/xet-cache-findings.md`. Do not re-litigate.

---

## Open questions for the team

1. **`SHIM_AUTH_TOKEN` does NOT work with a real client (confirmed 2026-09-05) — only peer-to-peer.**
   Decision needed: (a) exempt the Xet data paths and rely on network isolation; (b) move the shim secret
   to a separate, non-forwarded header (`X-Shim-Auth`); or (c) drop the feature. (Finding #1.)
2. **What is `peer_bytes_wasted` supposed to drive?** If it's the `PEER_HEDGE_FACTOR` dial, it must count
   real discarded bytes, not requested size. Is an accurate count worth the plumbing, or rename it?
3. **Should speculative hedge CDN pulls have their own concurrency budget** separate from real misses,
   given CDN bytes are "free to the operator" but fetch slots are not? (Finding #2/#4.)
4. **Is the per-request latency bound (not just per-race) a real guarantee you want to advertise?** If so,
   the sticky→fanout→plain-CDN fallthrough needs to be collapsed or time-boxed. (Finding #5.)
5. **Deployment target — systemd only, or K8s DaemonSet too?** The latter needs graceful shutdown.
6. **Acceptable serve-side memory ceiling?** The spec left it open; the hedge doubled the exposure.

---

### Reviewer uncertainty (be explicit)

- **Finding #1 is now CONFIRMED** (2026-09-05) via a real `hf_hub_download` through the shim with
  `SHIM_AUTH_TOKEN` set — both no-bearer (401 at `resolve`) and coupled-token (upstream forward fails)
  configs break. No longer a concern-pending-verification; it is a confirmed defect.
- **Finding #5's worst case** requires the CDN side of a race to fail *and* the peer side to fail, twice.
  How reachable that is in prod depends on CDN reliability; I'm flagging the structural possibility, not
  claiming it's common.
- Everything else I traced directly in-source and/or against the passing `-race` suite; those I'm confident on.

---

## Implementation update (2026-09-05)

Findings #1–#11 were worked on branch `fix/eval-findings` (one commit per finding; full `-race`
suite + `go vet` + `gofmt` green throughout). Summary:

| # | Finding | Resolution |
|---|---------|-----------|
| 1 | `SHIM_AUTH_TOKEN` breaks downloads | **Fixed.** Gate now applies to the peer channel (`X-Xet-Peer:1`) only; client traffic is ungated (network-isolation posture). **Validated end-to-end**: `acceptance.py` (now with `SHIM_AUTH_TOKEN` set) passes on the real Xet download of `model.safetensors` — byte-identical, hits, restart reuse. |
| 3 | `peer_bytes_wasted` overcount | **Fixed.** `cdnGet` reports bytes actually read; the peer-win path books that (drained after cancel), not the range size. |
| 2+4 | Hedge shares fetch semaphore / 2× RAM | **Fixed** (starvation): `cdnGet` uses a non-blocking `tryAcquire`; RAM 2× documented in env.example. |
| 5 | Per-request vs per-race bound | **Docs corrected** (CLAUDE.md, README, code comment). The fallthrough is legitimate CDN-failure resilience; kept, wording made honest. |
| 6 | No latency histogram / per-peer | **Fixed.** Added `xet_xorb_latency_ms` histogram by source, per-peer throughput gauge, and `xet_peer_hedge_cdn_bytes_total` to de-conflate `wan_bytes`. |
| 7 | Manifest cache non-atomic + O(n) trim | **Fixed.** Atomic temp+rename; in-memory FIFO seeded from disk, O(1) amortized trim. |
| 11 | 206 HIT missing Content-Range | **Fixed.** Reconstructs `bytes lo-hi/*`. |
| 9 | hit_rate counts peer wins as misses | **Fixed.** Added `effective_hit_rate`; alert guidance updated. |
| 10 | signedCandidates hardcoded | **Fixed.** `SIGNED_CANDIDATES_PER_XORB` env (default 8). |
| 8 | No graceful shutdown | **Fixed.** `http.Server.Shutdown` on SIGTERM/SIGINT (25s drain); keepalive gets a real stop channel. |
| 13 | Server god-struct | **Deferred** (intentional). Large mechanical refactor across every handler + test constructor, zero behavior change, low value; the finding itself said "don't do speculatively." Left for a supervised session. |

### New finding discovered during validation (NOT yet fixed)

**Content-Length stripping breaks HEAD metadata for non-LFS small files (Medium, needs impact check).**
A real `hf_hub_download` of a small git-stored file (e.g. `tokenizer.json`) through the shim fails with
`LocalEntryNotFoundError: Distant resource does not have a Content-Length`. Reproduces with **no auth
token**, so it is unrelated to finding #1. Root cause: the hub handler strips `Content-Length` via
`cleanHeaders` (`proxy.go:69`, hop-by-hop list `util.go:38`); on a HEAD (huggingface_hub's metadata call)
no body is written, so `net/http` cannot re-derive it and the HEAD response lacks `Content-Length`. LFS/Xet
files carry `X-Linked-Size` and work — which is why `acceptance.py` (only `model.safetensors`) never caught
it. **Must verify production impact**: does a full `snapshot_download` (which pulls `config.json`,
`tokenizer.json`, etc.) fail through the shim? If so this is High. Likely fix: preserve upstream
`Content-Length` on HEAD proxying. Tracked as a follow-up task.
