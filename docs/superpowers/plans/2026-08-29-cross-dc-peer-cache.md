# Cross-DC Peer Cache ("Tier 1.5") Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a cold datacenter's xet-cache pull warm xorb ranges from a sibling cache over a private backbone instead of HF's WAN CDN, cutting first-pull cold-start latency.

**Architecture:** Peering slots in as "Tier 1.5" — a best-effort accelerator between the local disk HIT and the CDN fallback, inside the existing `getXorb` singleflight closure. It reuses the existing `GET /xorb/xorbs/default/{hash}` serve endpoint (plus an `X-Xet-Peer: 1` "hit-or-404, no recursion" marker) and the existing `SHIM_AUTH_TOKEN` bearer for peer-to-peer auth. Discovery is broadcast-on-miss: a tight parallel `HEAD` probe across a static peer list, with a sticky-peer pointer so a burst pull probes ~once instead of per-range.

**Tech Stack:** Go stdlib `net/http` + `context`; existing `httpDoer` interface for injection; no new dependencies.

## Global Constraints

- **Peering is strictly an accelerator, never a dependency.** Every peer failure mode (timeout, 404, connection refused, non-206, short read, content-length mismatch) MUST fall through to the existing CDN path. A request can be at most `PEER_PROBE_TIMEOUT` slower than baseline; it must never *fail* a request the CDN would have served.
- **Hit-or-404, no recursion.** A request carrying `X-Xet-Peer: 1` is served from local disk or gets an immediate 404. It MUST NEVER trigger a CDN fetch or an outbound peer fetch. Fetch-chain depth is capped at one hop.
- **Feature-flag-by-absence.** Empty/unset `PEERS` = peering fully disabled; behavior MUST be byte-identical to today. Matches existing `SHIM_AUTH_TOKEN` / `MAX_INFLIGHT_FETCHES` convention.
- **On-disk format is unchanged.** Peering adds no new key or file format. Probe `HEAD` and fetch `GET` use the same `Range` string so they map to the identical `cachePath` key.
- **Metric accounting.** A peer-served range counts as a local `misses` (it was not a local-disk hit) and increments `peer_hits` + `peer_bytes`, but MUST NOT increment `wan_bytes` (no WAN was used). `served_bytes` is still counted per-caller outside the singleflight closure. This makes `wan_bytes_saved = served_bytes - wan_bytes` correctly credit peer serves.
- **Self-exclusion.** A cache must never probe/fetch itself; `SELF_URL` (when set) is filtered from the peer list.
- **Style:** one file per concern, `*_test.go` alongside; table tests; `go test -race ./...` and `go vet ./...` clean. Errors as `*httpError` values where they cross into the handler.

---

## File Structure

- **Create `shim-go/peers.go`** — `PeerSet` interface + static impl (`parsePeers`), and the `stickyPeer` state holder. Pure, no I/O.
- **Create `shim-go/peer_fetch.go`** — `fetchFromPeer` orchestration (sticky → fan-out probe → peer GET), `headProbe`, `peerGet`. Uses `s.doer`.
- **Modify `shim-go/server.go`** — add `peers`, `sticky`, `peerProbeTimeout` fields + doc comments.
- **Modify `shim-go/xorb.go`** — add the `HEAD`/hit-or-404 serve branch; wire `fetchFromPeer` into the miss path before the CDN fetch.
- **Modify `shim-go/middleware.go`** — add a `peer` field to the request log line.
- **Modify `shim-go/prometheus.go`** — add 4 new series.
- **Modify `shim-go/main.go`** — parse `PEERS`/`SELF_URL`/`PEER_PROBE_TIMEOUT_MS`/`PEER_STICKY_TTL_SECONDS`, construct and wire.
- **Modify** `CLAUDE.md`, `deploy/README.md`, `deploy/xet-dc-cache.env.example` — config + trust-boundary docs.
- **Create** `shim-go/peers_test.go`, `shim-go/peer_fetch_test.go`, `shim-go/peer_serve_test.go`.

---

## Task 1: Peer set config (`PeerSet` + `parsePeers`)

**Files:**
- Create: `shim-go/peers.go`
- Test: `shim-go/peers_test.go`

**Interfaces:**
- Produces: `type PeerSet interface { Peers() []string }`; `func parsePeers(raw, self string) []string`; `type staticPeers struct { list []string }` with `func (s staticPeers) Peers() []string`.
- Consumes: `trimSlash` (from `main.go`).

- [ ] **Step 1: Write the failing test**

```go
package main

import "testing"

func TestParsePeers(t *testing.T) {
	cases := []struct {
		name, raw, self string
		want            []string
	}{
		{"empty", "", "", nil},
		{"single", "https://a:8000", "", []string{"https://a:8000"}},
		{"trims spaces and slashes", " https://a:8000/ , https://b:8000 ", "",
			[]string{"https://a:8000", "https://b:8000"}},
		{"drops empties", "https://a:8000,,https://b:8000,", "",
			[]string{"https://a:8000", "https://b:8000"}},
		{"excludes self", "https://a:8000,https://b:8000", "https://b:8000/",
			[]string{"https://a:8000"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := parsePeers(c.raw, c.self)
			if !equalSlice(got, c.want) {
				t.Fatalf("parsePeers(%q,%q) = %v, want %v", c.raw, c.self, got, c.want)
			}
		})
	}
}

func TestStaticPeersImplementsPeerSet(t *testing.T) {
	var ps PeerSet = staticPeers{list: []string{"https://a:8000"}}
	if got := ps.Peers(); !equalSlice(got, []string{"https://a:8000"}) {
		t.Fatalf("Peers() = %v", got)
	}
}
```

Note: `equalSlice` already exists in `xorb_test.go`; treat `nil` and `[]string{}` as equal there is NOT guaranteed — `equalSlice` compares by length so `nil` vs `nil` is fine; ensure `parsePeers` returns `nil` (not `[]string{}`) for the empty case.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd shim-go && go test -run 'TestParsePeers|TestStaticPeers' ./...`
Expected: FAIL (undefined: parsePeers, staticPeers, PeerSet)

- [ ] **Step 3: Write minimal implementation**

```go
package main

import "strings"

// PeerSet is the set of sibling caches this host may pull warm ranges from.
// Static today (parsed from PEERS at startup); an interface so a future source
// (e.g. cloud service discovery) can replace it without touching the fetch path.
type PeerSet interface {
	Peers() []string // base URLs, self already excluded
}

type staticPeers struct{ list []string }

func (s staticPeers) Peers() []string { return s.list }

// parsePeers splits a comma-separated peer list, trimming whitespace and
// trailing slashes, dropping empties and any entry equal to self. Returns nil
// when no usable peers remain (peering disabled).
func parsePeers(raw, self string) []string {
	self = trimSlash(strings.TrimSpace(self))
	var out []string
	for _, p := range strings.Split(raw, ",") {
		p = trimSlash(strings.TrimSpace(p))
		if p == "" || p == self {
			continue
		}
		out = append(out, p)
	}
	return out
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd shim-go && go test -run 'TestParsePeers|TestStaticPeers' ./...`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add shim-go/peers.go shim-go/peers_test.go
git commit -m "feat(peers): static peer set config parsing"
```

---

## Task 2: Hit-or-404 serving mode (server side)

**Files:**
- Modify: `shim-go/xorb.go` (the `getXorb` handler)
- Modify: `shim-go/middleware.go` (add `peer` log field)
- Test: `shim-go/peer_serve_test.go`

**Interfaces:**
- Produces: `func isPeerRequest(r *http.Request) bool` (returns `r.Header.Get("X-Xet-Peer") == "1"`); modified `getXorb` behavior for `HEAD` and for `X-Xet-Peer` GET misses.
- Consumes: existing `cachePath`, `os.Stat`, `os.ReadFile`, `s.lru.Touch`, `writeXorbBytes`.

**Behavior added to `getXorb`** (after computing `path`/`name`, keep the existing Range-required 400 check):
1. If `r.Method == http.MethodHead`: existence probe. `os.Stat(path)` ok → `s.lru.Touch(name)`, set `Accept-Ranges: bytes`, `200`, no body. Miss → `404`. Never fetch.
2. GET local-disk HIT: unchanged (this already serves a peer's GET correctly).
3. GET local miss **and** `isPeerRequest(r)`: `404` immediately, return. Do NOT enter singleflight/CDN.
4. GET local miss, not a peer request: existing singleflight/CDN path (Task 5 modifies its interior).

- [ ] **Step 1: Write the failing test**

```go
package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"sync/atomic"
	"testing"
)

func doReq(s *Server, method, hash, rng string, peer bool) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, "/xorb/xorbs/default/"+hash, nil)
	req.SetPathValue("xorb_hash", hash)
	if rng != "" {
		req.Header.Set("Range", rng)
	}
	if peer {
		req.Header.Set("X-Xet-Peer", "1")
	}
	rec := httptest.NewRecorder()
	s.getXorb(rec, req)
	return rec
}

// A peer GET for a range we DON'T have must 404 immediately and never touch
// the CDN doer (no recursion).
func TestPeerGetMissIs404NoCDN(t *testing.T) {
	d := &countingDoer{}
	s := newXorbTestServer(t, d)
	s.signed.Set("h", []string{"http://cdn/x"}) // would authorize a CDN fetch if reached

	rec := doReq(s, "GET", "h", "bytes=0-4", true)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("peer miss code = %d, want 404", rec.Code)
	}
	if n := atomic.LoadInt32(&d.n); n != 0 {
		t.Fatalf("CDN doer called %d times on a peer miss; must be 0 (no recursion)", n)
	}
}

// A peer GET for a range we DO have serves it (206) like any local HIT.
func TestPeerGetHitServes(t *testing.T) {
	s := newXorbTestServer(t, &countingDoer{})
	path := cachePath(s.cacheDir, "h", "bytes=0-4")
	if err := os.WriteFile(path, []byte("BYTES"), 0o644); err != nil {
		t.Fatal(err)
	}
	rec := doReq(s, "GET", "h", "bytes=0-4", true)
	if rec.Code != 206 || rec.Body.String() != "BYTES" {
		t.Fatalf("peer hit code=%d body=%q", rec.Code, rec.Body.String())
	}
}

// A HEAD probe: 200 when cached, 404 when not, never a body, never the CDN.
func TestHeadProbeExistence(t *testing.T) {
	d := &countingDoer{}
	s := newXorbTestServer(t, d)
	path := cachePath(s.cacheDir, "h", "bytes=0-4")
	if err := os.WriteFile(path, []byte("BYTES"), 0o644); err != nil {
		t.Fatal(err)
	}
	if rec := doReq(s, "HEAD", "h", "bytes=0-4", true); rec.Code != 200 || rec.Body.Len() != 0 {
		t.Fatalf("HEAD hit code=%d bodylen=%d, want 200/0", rec.Code, rec.Body.Len())
	}
	if rec := doReq(s, "HEAD", "missing", "bytes=0-4", true); rec.Code != 404 {
		t.Fatalf("HEAD miss code=%d, want 404", rec.Code)
	}
	if n := atomic.LoadInt32(&d.n); n != 0 {
		t.Fatalf("CDN doer called %d times during HEAD probes; must be 0", n)
	}
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd shim-go && go test -run 'TestPeerGet|TestHeadProbe' ./...`
Expected: FAIL (peer miss currently falls into singleflight/CDN or 400/500; HEAD currently reads whole file)

- [ ] **Step 3: Implement**

In `shim-go/xorb.go`, add near the other helpers:

```go
// isPeerRequest reports whether this request came from a sibling cache. Such
// requests are served hit-or-404: local disk or an immediate 404, never a CDN
// or onward-peer fetch (see the peering design — one-hop, no recursion).
func isPeerRequest(r *http.Request) bool {
	return r.Header.Get("X-Xet-Peer") == "1"
}
```

In `getXorb`, immediately after the existing `if byteRange == "" { ... 400 ... }` block and the `path`/`name` computation, insert:

```go
	// HEAD is an existence probe (used by peer discovery): stat, no body.
	if r.Method == http.MethodHead {
		if _, err := os.Stat(path); err == nil {
			s.lru.Touch(name)
			w.Header().Set("Accept-Ranges", "bytes")
			w.WriteHeader(http.StatusOK)
			return
		}
		w.WriteHeader(http.StatusNotFound)
		return
	}
```

Then, after the existing local-disk HIT block (the `if body, err := os.ReadFile(path); err == nil { ... }`), before the `s.sf.Do(...)` call, insert:

```go
	// Hit-or-404: a peer's GET never triggers a CDN or onward-peer fetch.
	if isPeerRequest(r) {
		w.WriteHeader(http.StatusNotFound)
		return
	}
```

In `shim-go/middleware.go`, add `"peer", r.Header.Get("X-Xet-Peer") == "1"` to the `slog.Info("request", ...)` argument list in `withLogging`.

- [ ] **Step 4: Run to verify it passes**

Run: `cd shim-go && go test -run 'TestPeerGet|TestHeadProbe' ./... && go test ./...`
Expected: PASS (and no regression in existing xorb tests)

- [ ] **Step 5: Commit**

```bash
git add shim-go/xorb.go shim-go/middleware.go shim-go/peer_serve_test.go
git commit -m "feat(peers): hit-or-404 serving + HEAD existence probe"
```

---

## Task 3: Sticky-peer state

**Files:**
- Modify: `shim-go/peers.go`
- Test: `shim-go/peers_test.go`

**Interfaces:**
- Produces: `type stickyPeer struct{...}`; `func newStickyPeer(ttl time.Duration, clock func() time.Time) *stickyPeer`; `func (s *stickyPeer) get() (string, bool)`; `func (s *stickyPeer) set(url string)`.
- Design: `clock` is injectable for tests (mirrors `NewTTLMap`'s clock param). `nil` clock defaults to `time.Now`. Thread-safe.

- [ ] **Step 1: Write the failing test**

```go
func TestStickyPeerSetGetExpiry(t *testing.T) {
	now := time.Unix(1000, 0)
	sp := newStickyPeer(60*time.Second, func() time.Time { return now })

	if _, ok := sp.get(); ok {
		t.Fatal("empty sticky should report not-live")
	}
	sp.set("https://a:8000")
	if url, ok := sp.get(); !ok || url != "https://a:8000" {
		t.Fatalf("get after set = (%q,%v)", url, ok)
	}
	now = now.Add(59 * time.Second)
	if _, ok := sp.get(); !ok {
		t.Fatal("should still be live at 59s (ttl 60s)")
	}
	now = now.Add(2 * time.Second) // 61s total
	if _, ok := sp.get(); ok {
		t.Fatal("should be expired past ttl")
	}
}
```

Add `"time"` to the `peers_test.go` imports.

- [ ] **Step 2: Run to verify it fails**

Run: `cd shim-go && go test -run TestStickyPeer ./...`
Expected: FAIL (undefined: newStickyPeer)

- [ ] **Step 3: Implement** (append to `shim-go/peers.go`; add `"sync"` and `"time"` imports)

```go
// stickyPeer remembers the peer that most recently served us a range, so a
// burst model-pull (thousands of ranges over seconds) probes ~once instead of
// per-range. A short TTL lets it lapse between pulls. Thread-safe.
type stickyPeer struct {
	mu     sync.Mutex
	url    string
	expiry time.Time
	ttl    time.Duration
	now    func() time.Time
}

func newStickyPeer(ttl time.Duration, clock func() time.Time) *stickyPeer {
	if clock == nil {
		clock = time.Now
	}
	return &stickyPeer{ttl: ttl, now: clock}
}

func (s *stickyPeer) get() (string, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.url == "" || !s.now().Before(s.expiry) {
		return "", false
	}
	return s.url, true
}

func (s *stickyPeer) set(url string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.url = url
	s.expiry = s.now().Add(s.ttl)
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd shim-go && go test -run TestStickyPeer ./...`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add shim-go/peers.go shim-go/peers_test.go
git commit -m "feat(peers): sticky-peer TTL state"
```

---

## Task 4: Peer fetch orchestration (`fetchFromPeer`)

**Files:**
- Create: `shim-go/peer_fetch.go`
- Modify: `shim-go/server.go` (add fields)
- Test: `shim-go/peer_fetch_test.go`

**Interfaces:**
- Consumes: `PeerSet` (Task 1), `*stickyPeer` (Task 3), `httpDoer` (`s.doer`), `s.authToken`, `parseRange` (`util.go`), `xorbResult` (`xorb.go`), `s.metrics.Incr`.
- Produces: `func (s *Server) fetchFromPeer(ctx context.Context, hash, byteRange string) (xorbResult, bool)`; helpers `headProbe`, `peerGet`, `fanoutProbe`.
- Server fields added: `peers PeerSet`, `sticky *stickyPeer`, `peerProbeTimeout time.Duration`.

**Semantics:**
- Returns `(result, true)` only on a verified 206 peer body whose length matches the requested range; else `(xorbResult{}, false)` and the caller falls through to the CDN.
- Order: (1) if a sticky peer is live, `HEAD`-probe it under the probe budget; on 200, `peerGet` it. (2) else/on sticky failure, parallel `HEAD` fan-out across all peers under one shared budget; first 200 wins; `peerGet` the winner. (3) success sets sticky; total peer-miss increments `peer_misses`.
- `peerGet` sets `Range`, `X-Xet-Peer: 1`, and `Authorization: Bearer <authToken>` when `authToken != ""`.
- Length check: expected = `hi - lo` from `parseRange` (note `parseRange` returns a half-open `[lo, hi)`), so expected byte count is `hi - lo`. Mismatch → treat as peer failure.

- [ ] **Step 1: Write the failing test** (`shim-go/peer_fetch_test.go`)

```go
package main

import (
	"context"
	"io"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// peerDoer simulates a fleet: `have` maps peer-base-URL -> set of hashes it has.
// HEAD returns 200/404; GET returns 206 with `body` when present, else 404.
// `hang` (peer base URLs) makes HEAD block until ctx is cancelled (timeout test).
type peerDoer struct {
	have map[string]map[string]bool
	body string
	hang map[string]bool
	gets int32
}

func (d *peerDoer) Do(req *http.Request) (*http.Response, error) {
	base := req.URL.Scheme + "://" + req.URL.Host
	hash := strings.TrimPrefix(req.URL.Path, "/xorb/xorbs/default/")
	if d.hang[base] {
		<-req.Context().Done()
		return nil, req.Context().Err()
	}
	has := d.have[base][hash]
	resp := func(code int, body string) *http.Response {
		return &http.Response{StatusCode: code, Body: io.NopCloser(strings.NewReader(body)), Header: http.Header{}}
	}
	if req.Method == http.MethodHead {
		if has {
			return resp(200, ""), nil
		}
		return resp(404, ""), nil
	}
	atomic.AddInt32(&d.gets, 1)
	if has {
		return resp(206, d.body), nil
	}
	return resp(404, ""), nil
}

func newPeerFetchServer(peers []string, d httpDoer) *Server {
	return &Server{
		metrics:          NewMetrics(),
		doer:             d,
		peers:            staticPeers{list: peers},
		sticky:           newStickyPeer(60*time.Second, nil),
		peerProbeTimeout: 200 * time.Millisecond,
	}
}

func TestFetchFromPeerFanoutHit(t *testing.T) {
	d := &peerDoer{
		have: map[string]map[string]bool{"https://b:8000": {"h": true}},
		body: "BYTES",
	}
	s := newPeerFetchServer([]string{"https://a:8000", "https://b:8000"}, d)

	res, ok := s.fetchFromPeer(context.Background(), "h", "bytes=0-4")
	if !ok || string(res.body) != "BYTES" {
		t.Fatalf("fanout hit = (%q,%v), want BYTES,true", res.body, ok)
	}
	if snap := s.metrics.Snapshot(); snap["peer_hits"].(int64) != 1 || snap["peer_bytes"].(int64) != 5 {
		t.Fatalf("metrics = %+v", snap)
	}
	// The winner should now be sticky.
	if url, live := s.sticky.get(); !live || url != "https://b:8000" {
		t.Fatalf("sticky = (%q,%v), want b live", url, live)
	}
}

func TestFetchFromPeerStickyReused(t *testing.T) {
	d := &peerDoer{have: map[string]map[string]bool{"https://b:8000": {"h": true, "h2": true}}, body: "BYTES"}
	s := newPeerFetchServer([]string{"https://a:8000", "https://b:8000"}, d)
	s.sticky.set("https://b:8000")

	res, ok := s.fetchFromPeer(context.Background(), "h", "bytes=0-4")
	if !ok || string(res.body) != "BYTES" {
		t.Fatalf("sticky hit = (%q,%v)", res.body, ok)
	}
}

func TestFetchFromPeerAllMiss(t *testing.T) {
	d := &peerDoer{have: map[string]map[string]bool{}, body: "BYTES"}
	s := newPeerFetchServer([]string{"https://a:8000", "https://b:8000"}, d)

	if _, ok := s.fetchFromPeer(context.Background(), "h", "bytes=0-4"); ok {
		t.Fatal("all-miss should return false (fall to CDN)")
	}
	if snap := s.metrics.Snapshot(); snap["peer_misses"].(int64) != 1 {
		t.Fatalf("peer_misses = %v, want 1", snap["peer_misses"])
	}
}

func TestFetchFromPeerNoPeers(t *testing.T) {
	s := newPeerFetchServer(nil, &peerDoer{})
	if _, ok := s.fetchFromPeer(context.Background(), "h", "bytes=0-4"); ok {
		t.Fatal("no peers should return false")
	}
}

func TestFetchFromPeerTimeoutFallsThrough(t *testing.T) {
	d := &peerDoer{
		have: map[string]map[string]bool{"https://b:8000": {"h": true}},
		body: "BYTES",
		hang: map[string]bool{"https://a:8000": true, "https://b:8000": true},
	}
	s := newPeerFetchServer([]string{"https://a:8000", "https://b:8000"}, d)
	s.peerProbeTimeout = 50 * time.Millisecond

	start := time.Now()
	_, ok := s.fetchFromPeer(context.Background(), "h", "bytes=0-4")
	if ok {
		t.Fatal("hanging peers should time out -> false")
	}
	if elapsed := time.Since(start); elapsed > 500*time.Millisecond {
		t.Fatalf("took %v; probe budget must bound it near 50ms", elapsed)
	}
	if snap := s.metrics.Snapshot(); snap["peer_probe_timeouts"].(int64) < 1 {
		t.Fatalf("expected a probe-timeout count, got %v", snap["peer_probe_timeouts"])
	}
}

func TestFetchFromPeerShortReadRejected(t *testing.T) {
	d := &peerDoer{have: map[string]map[string]bool{"https://b:8000": {"h": true}}, body: "AB"} // 2 bytes, range wants 5
	s := newPeerFetchServer([]string{"https://b:8000"}, d)
	if _, ok := s.fetchFromPeer(context.Background(), "h", "bytes=0-4"); ok {
		t.Fatal("short read (2 != 5 bytes) must be rejected -> false")
	}
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd shim-go && go test -run TestFetchFromPeer ./...`
Expected: FAIL (undefined: fetchFromPeer; Server has no peers/sticky/peerProbeTimeout fields)

- [ ] **Step 3: Add Server fields** (`shim-go/server.go`, in the `Server` struct)

```go
	// Peering ("Tier 1.5"): pull a warm range from a sibling cache over the
	// private backbone before falling back to the CDN. nil peers = disabled.
	peers            PeerSet
	sticky           *stickyPeer
	peerProbeTimeout time.Duration
```

Add `"time"` to `server.go` imports.

- [ ] **Step 4: Implement `fetchFromPeer`** (`shim-go/peer_fetch.go`)

```go
package main

import (
	"context"
	"io"
	"net/http"
	"sync"
)

// fetchFromPeer tries to serve (hash, byteRange) from a warm sibling cache.
// Returns (result, true) only on a verified 206 whose body length matches the
// requested range; otherwise (zero, false) and the caller falls back to the
// CDN. Best-effort: any failure is a false return, never an error to the client.
func (s *Server) fetchFromPeer(ctx context.Context, hash, byteRange string) (xorbResult, bool) {
	if s.peers == nil {
		return xorbResult{}, false
	}
	peers := s.peers.Peers()
	if len(peers) == 0 {
		return xorbResult{}, false
	}

	// 1. Sticky peer: probe just it under the budget; on hit, fetch it.
	if url, live := s.sticky.get(); live {
		if s.probeOK(ctx, url, hash, byteRange) {
			if res, ok := s.peerGet(ctx, url, hash, byteRange); ok {
				s.sticky.set(url)
				s.metrics.Incr("peer_hits", 1)
				s.metrics.Incr("peer_bytes", int64(len(res.body)))
				return res, true
			}
		}
	}

	// 2. Fan-out probe; first responder wins.
	if winner, ok := s.fanoutProbe(ctx, peers, hash, byteRange); ok {
		if res, ok := s.peerGet(ctx, winner, hash, byteRange); ok {
			s.sticky.set(winner)
			s.metrics.Incr("peer_hits", 1)
			s.metrics.Incr("peer_bytes", int64(len(res.body)))
			return res, true
		}
	}

	s.metrics.Incr("peer_misses", 1)
	return xorbResult{}, false
}

// probeOK issues one budgeted HEAD to a single peer; true iff it 200s in time.
func (s *Server) probeOK(ctx context.Context, base, hash, byteRange string) bool {
	pctx, cancel := context.WithTimeout(ctx, s.peerProbeTimeout)
	defer cancel()
	return s.headProbe(pctx, base, hash, byteRange)
}

// fanoutProbe HEAD-probes all peers in parallel under one shared budget and
// returns the first that has the range. Cancels the rest once a winner is found.
func (s *Server) fanoutProbe(ctx context.Context, peers []string, hash, byteRange string) (string, bool) {
	pctx, cancel := context.WithTimeout(ctx, s.peerProbeTimeout)
	defer cancel()

	found := make(chan string, len(peers))
	var wg sync.WaitGroup
	for _, base := range peers {
		wg.Add(1)
		go func(base string) {
			defer wg.Done()
			if s.headProbe(pctx, base, hash, byteRange) {
				select {
				case found <- base:
				default:
				}
			}
		}(base)
	}
	go func() { wg.Wait(); close(found) }()

	select {
	case base, ok := <-found:
		if !ok {
			return "", false // all probed, none had it
		}
		return base, true
	case <-pctx.Done():
		s.metrics.Incr("peer_probe_timeouts", 1)
		return "", false
	}
}

func (s *Server) headProbe(ctx context.Context, base, hash, byteRange string) bool {
	req, err := http.NewRequestWithContext(ctx, http.MethodHead, base+"/xorb/xorbs/default/"+hash, nil)
	if err != nil {
		return false
	}
	s.setPeerHeaders(req, byteRange)
	resp, err := s.doer.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

// peerGet fetches the range from base and verifies the body length matches the
// request. Returns (result, true) only on a clean 206 of the expected size.
func (s *Server) peerGet(ctx context.Context, base, hash, byteRange string) (xorbResult, bool) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, base+"/xorb/xorbs/default/"+hash, nil)
	if err != nil {
		return xorbResult{}, false
	}
	s.setPeerHeaders(req, byteRange)
	resp, err := s.doer.Do(req)
	if err != nil {
		return xorbResult{}, false
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusPartialContent && resp.StatusCode != http.StatusOK {
		return xorbResult{}, false
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return xorbResult{}, false
	}
	if lo, hi, perr := parseRange(byteRange); perr == nil && int64(len(body)) != hi-lo {
		return xorbResult{}, false // short/over read — don't trust it
	}
	return xorbResult{body: body, contentRange: resp.Header.Get("Content-Range")}, true
}

func (s *Server) setPeerHeaders(req *http.Request, byteRange string) {
	req.Header.Set("Range", byteRange)
	req.Header.Set("X-Xet-Peer", "1")
	req.Header.Set("Accept-Encoding", "identity")
	if s.authToken != "" {
		req.Header.Set("Authorization", "Bearer "+s.authToken)
	}
}
```

- [ ] **Step 5: Run to verify it passes**

Run: `cd shim-go && go test -run TestFetchFromPeer ./... && go test -race -run TestFetchFromPeer ./...`
Expected: PASS (including the race detector — `fanoutProbe` uses goroutines)

- [ ] **Step 6: Commit**

```bash
git add shim-go/peer_fetch.go shim-go/server.go shim-go/peer_fetch_test.go
git commit -m "feat(peers): peer fetch orchestration (sticky + fan-out probe)"
```

---

## Task 5: Wire peer fetch into the miss path + metrics

**Files:**
- Modify: `shim-go/xorb.go` (the `getXorb` singleflight closure)
- Modify: `shim-go/prometheus.go` (4 new series)
- Test: `shim-go/peer_fetch_test.go` (integration test with two in-process servers)

**Interfaces:**
- Consumes: `fetchFromPeer` (Task 4), existing `writeCacheFileAtomic`, `s.lru.Record`, `s.metrics`.
- The peer attempt runs at the top of the singleflight closure, **before** `s.acquire(...)` — a peer serve must not consume a CDN concurrency slot.

- [ ] **Step 1: Write the failing integration test** (append to `shim-go/peer_fetch_test.go`)

```go
// Two in-process servers: A (cold) peers to B (warm). A must serve B's bytes
// via the peer path, count peer_bytes (not wan_bytes), and cache locally.
func TestGetXorbServesFromPeerEndToEnd(t *testing.T) {
	warmDir := t.TempDir()
	// Seed B's disk with the range.
	if err := os.WriteFile(cachePath(warmDir, "h", "bytes=0-4"), []byte("BYTES"), 0o644); err != nil {
		t.Fatal(err)
	}
	b := &Server{
		cacheDir: warmDir, metrics: NewMetrics(), lru: newLRU(0, func(string) {}),
		signed: NewTTLMap(3600e9, 100, nil), doer: &countingDoer{}, signedCandidates: 8,
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /xorb/xorbs/default/{xorb_hash}", b.getXorb)
	ts := httptest.NewServer(mux)
	defer ts.Close()

	// A is cold; its only peer is B. A.doer is a real client so it can reach the
	// httptest peer. A has no signed URLs, so any CDN fallback would 409 —
	// a 206 therefore proves the peer path served it.
	a := &Server{
		cacheDir: t.TempDir(), metrics: NewMetrics(), lru: newLRU(0, func(string) {}),
		signed: NewTTLMap(3600e9, 100, nil), doer: http.DefaultClient, signedCandidates: 8,
		peers: staticPeers{list: []string{ts.URL}}, sticky: newStickyPeer(time.Minute, nil),
		peerProbeTimeout: time.Second,
	}
	a.signed.Set("h", nil)

	rec := doGetXorb(a, "h", "bytes=0-4")
	if rec.Code != 206 || rec.Body.String() != "BYTES" {
		t.Fatalf("A code=%d body=%q, want 206/BYTES from peer", rec.Code, rec.Body.String())
	}
	snap := a.metrics.Snapshot()
	if snap["peer_bytes"].(int64) != 5 {
		t.Fatalf("peer_bytes = %v, want 5", snap["peer_bytes"])
	}
	if snap["wan_bytes"].(int64) != 0 {
		t.Fatalf("wan_bytes = %v, want 0 (peer served, no WAN)", snap["wan_bytes"])
	}
	// Second request to A is now a local HIT.
	if rec2 := doGetXorb(a, "h", "bytes=0-4"); rec2.Header().Get("X-Cache") != "HIT" {
		t.Fatalf("second A request X-Cache = %s, want HIT", rec2.Header().Get("X-Cache"))
	}
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd shim-go && go test -run TestGetXorbServesFromPeer ./...`
Expected: FAIL (peer path not wired; A returns 409/502 or calls CDN)

- [ ] **Step 3: Implement — wire into `getXorb`**

In `shim-go/xorb.go`, inside the `s.sf.Do(name, func() (any, error) { ... })` closure, add at the very top (before the `s.acquire(...)` call):

```go
		// Tier 1.5: try a warm peer before the CDN. Peer serves don't consume
		// a CDN slot and don't count as WAN.
		if s.peers != nil {
			if res, ok := s.fetchFromPeer(r.Context(), hash, byteRange); ok {
				if werr := writeCacheFileAtomic(s.cacheDir, name, path, res.body); werr != nil {
					return nil, &httpError{500, "cache write: " + werr.Error()}
				}
				s.lru.Record(name, int64(len(res.body)))
				s.metrics.Incr("misses", 1)
				return res, nil
			}
		}
```

The existing CDN block (acquire → fetchAuthorized → read → write → `misses`/`wan_bytes`) stays exactly as-is below this, unchanged, as the fallback.

- [ ] **Step 4: Add Prometheus series** (`shim-go/prometheus.go`, inside `prometheusText`, after the existing `xet_served_bytes_total` block)

```go
	promMetric(&b, "xet_peer_hits_total", "counter",
		"Xorb ranges served by a sibling peer cache.", snap["peer_hits"])
	promMetric(&b, "xet_peer_misses_total", "counter",
		"Misses where peers were tried but none had the range (went to CDN).", snap["peer_misses"])
	promMetric(&b, "xet_peer_bytes_total", "counter",
		"Bytes pulled from peers (WAN/CDN transfer displaced).", snap["peer_bytes"])
	promMetric(&b, "xet_peer_probe_timeouts_total", "counter",
		"Peer discovery probes that exceeded PEER_PROBE_TIMEOUT.", snap["peer_probe_timeouts"])
```

Counter keys are absent from the snapshot map until first incremented, so both `promMetric`'s type switch and any test doing `snap[k].(int64)` (e.g. Task 5's end-to-end test reading `wan_bytes` after a peer serve that never touched the CDN) need the base and peer counters to always be present. Seed all eight unconditionally in `Metrics.Snapshot` (`shim-go/metrics.go`), before `return out`:

```go
	for _, k := range []string{
		"hits", "misses", "wan_bytes", "served_bytes",
		"peer_hits", "peer_misses", "peer_bytes", "peer_probe_timeouts",
	} {
		if _, ok := out[k]; !ok {
			out[k] = int64(0)
		}
	}
```

This keeps `wan_bytes_saved` and `hit_rate` correct (they read `m.c[...]` directly, not `out`), makes all series present for scrapers, satisfies `TestPrometheusEveryMetricHasType`, and prevents nil type-assertion panics in tests.

- [ ] **Step 5: Run the full suite (with race)**

Run: `cd shim-go && go test -race ./... && go vet ./...`
Expected: PASS, vet clean. If `TestPrometheusEveryMetricHasType` fails, apply the `Snapshot` seeding above for whichever series it reports missing.

- [ ] **Step 6: Commit**

```bash
git add shim-go/xorb.go shim-go/prometheus.go shim-go/metrics.go shim-go/peer_fetch_test.go
git commit -m "feat(peers): wire peer fetch into miss path + peer metrics"
```

---

## Task 6: Config wiring + documentation

**Files:**
- Modify: `shim-go/main.go`
- Modify: `CLAUDE.md`, `deploy/README.md`, `deploy/xet-dc-cache.env.example`
- Test: `shim-go/main_test.go` (if it exercises construction) — otherwise the acceptance path; no new unit test required beyond a parse smoke test already covered in Task 1.

**Interfaces:**
- Consumes: `parsePeers`, `staticPeers`, `newStickyPeer` (Tasks 1, 3); `env`, `envInt`, `trimSlash` (`main.go`).

- [ ] **Step 1: Wire construction in `main.go`**

After the `sem` block and before building `s`, add:

```go
	// Peering ("Tier 1.5"): PEERS empty = disabled. Self is filtered out so a
	// cache never probes itself.
	peerList := parsePeers(env("PEERS", ""), env("SELF_URL", ""))
	var peers PeerSet
	if len(peerList) > 0 {
		peers = staticPeers{list: peerList}
	}
	stickyTTL := time.Duration(envInt("PEER_STICKY_TTL_SECONDS", 60)) * time.Second
	probeTimeout := time.Duration(envInt("PEER_PROBE_TIMEOUT_MS", 200)) * time.Millisecond
```

Add these fields to the `Server` literal:

```go
		peers:            peers,
		sticky:           newStickyPeer(stickyTTL, nil),
		peerProbeTimeout: probeTimeout,
```

Extend the startup `slog.Info` with `"peers", len(peerList)`.

- [ ] **Step 2: Build**

Run: `cd shim-go && go build ./... && go vet ./...`
Expected: builds clean.

- [ ] **Step 3: Run the full suite**

Run: `cd shim-go && go test -race ./...`
Expected: PASS.

- [ ] **Step 4: Update docs**

`CLAUDE.md` — in the Configuration section env list, add:
`PEERS` (comma-separated sibling base URLs; empty = peering off), `SELF_URL` (filtered from `PEERS`), `PEER_PROBE_TIMEOUT_MS` (default 200), `PEER_STICKY_TTL_SECONDS` (default 60). Add a one-paragraph "Tier 1.5 peering" note: on a miss the shim probes peers (`HEAD` + `X-Xet-Peer: 1`, hit-or-404, one hop) and pulls from a warm one before the CDN; peer serves count as `peer_bytes`, not `wan_bytes`; the decision metric is `xet_peer_bytes_total` vs `xet_wan_bytes_total`.

`deploy/xet-dc-cache.env.example` — add commented entries:

```bash
# Cross-DC peering (Tier 1.5): comma-separated sibling cache base URLs.
# Empty = disabled. Must be reachable over the private backbone.
# PEERS=https://dc1-host:8000,https://dc2-host:8000
# SELF_URL=https://this-host:8000
# PEER_PROBE_TIMEOUT_MS=200
# PEER_STICKY_TTL_SECONDS=60
```

`deploy/README.md` — under Observability add the four `xet_peer_*` series; under Security/trust boundary add: peer-to-peer requests carry the same `SHIM_AUTH_TOKEN` bearer; a peer request (`X-Xet-Peer: 1`) is served hit-or-404 and never triggers a CDN or onward-peer fetch; peering is fleet-shared (single trust domain) so any cache may serve any other — do not enable across a trust boundary you don't control.

- [ ] **Step 5: Commit**

```bash
git add shim-go/main.go CLAUDE.md deploy/README.md deploy/xet-dc-cache.env.example
git commit -m "feat(peers): config wiring + docs (Tier 1.5 peering)"
```

---

## Self-Review Notes

- **Spec coverage:** PeerSet/config (T1), hit-or-404 + HEAD probe serving (T2), sticky state (T3), fetch orchestration with sticky→fan-out→CDN + all unhappy paths (T4), miss-path wiring + metrics + Prometheus (T5), env + docs (T6). Every design section maps to a task.
- **Metric accounting** (Global Constraint) is exercised by `TestGetXorbServesFromPeerEndToEnd` (`wan_bytes == 0`, `peer_bytes == 5`) and unit metrics in T4.
- **Safety-rail unhappy paths** (timeout, all-miss, short-read, no-peers) each have a dedicated test in T4; the no-recursion invariant is proven in T2 (`TestPeerGetMissIs404NoCDN`, `TestHeadProbeExistence` assert the CDN doer is never called).
- **Type consistency:** `fetchFromPeer` returns the existing `xorbResult`; `parseRange` half-open `[lo,hi)` means expected length is `hi-lo` (used consistently in T4). `peerDoer`/`countingDoer`/`equalSlice` reuse existing test helpers.
- **Reversibility:** with `PEERS` empty, `s.peers == nil` and every new branch is skipped — behavior is byte-identical to today (asserted by keeping all existing tests green in T2/T5).
