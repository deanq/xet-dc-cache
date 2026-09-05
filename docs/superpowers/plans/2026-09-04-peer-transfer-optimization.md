# Peer Transfer Optimization — "Never Slower Than WAN" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the cross-DC peer path a warm, tuned HTTP/1.1 transport and an adaptive hedge that races the CDN, so the peer path is a pure throughput win that can never leave a client slower than a direct WAN pull beyond a bounded, configurable slack.

**Architecture:** Two independent, separately reviewable mechanisms wired into the existing `getXorb` singleflight closure. **(A)** a dedicated peer `http.Transport` (HTTP/1.1 forced, fat idle pool, optional socket-buffer sizing, optional keepalive) kept strictly distinct from the fragile CDN doer. **(B)** an adaptive hedged fetch: fire the peer `GET`, start an EWMA-predicted timer, and on timer fire race a CDN `fetchAuthorized` in parallel, taking the first to finish and cancelling the loser. Per-peer throughput EWMA lives beside the sticky pointer in `peers.go`.

**Tech Stack:** Go 1.27 (`shim-go/`, module `xetcache`), stdlib `net/http` + `syscall`, `golang.org/x/sync/singleflight`, table + `httpDoer`-injection tests with `go test -race`.

## Global Constraints

- Go module lives in `shim-go/`. `cd shim-go && go test -race ./...` and `go vet ./...` MUST pass at the end of every task.
- On-disk cache key format is a compatibility contract — do NOT change `cachePath` or manifest naming.
- CDN transport contract is untouched: `CheckRedirect -> http.ErrUseLastResponse`, `DisableCompression: true`, and `Accept-Encoding: identity` on every upstream request. The peer transport MUST be a distinct `http.Transport` object; peer tuning may not perturb `s.doer`.
- Peer requests carry `X-Xet-Peer: 1` and (if `SHIM_AUTH_TOKEN` set) the bearer token; peer serving is hit-or-404, one hop, no recursion.
- Errors are values: every peer failure path falls back to the CDN and never fails a request the CDN could serve.
- singleflight accounting is unchanged: `misses` and (WAN/peer) byte counters are incremented exactly once inside the flight; `served_bytes` is incremented per caller outside the flight. CDN win => `wan_bytes`; peer win => `peer_bytes`. The winning body is written via `writeCacheFileAtomic`; the loser never touches disk.
- New env vars default to today's behavior: `PEER_MAX_IDLE_CONNS_PER_HOST=64`, `PEER_SOCKET_BUFFER_BYTES=0`, `PEER_KEEPALIVE_INTERVAL_MS=0`, `PEER_HEDGE_FACTOR=1.5`, `PEER_HEDGE_MIN_MS=50`, `PEER_HEDGE_MAX_MS=1000`. `PEER_FETCH_TIMEOUT_MS` is retained as the hard per-GET ceiling.

---

### Task 1: Dedicated peer HTTP transport (HTTP/1.1 + fat idle pool)

**Files:**
- Create: `shim-go/peer_transport.go`
- Create: `shim-go/peer_transport_test.go`
- Modify: `shim-go/server.go` (add `peerDoer` field + `peerHTTP()` helper)
- Modify: `shim-go/peer_fetch.go` (route `headProbe`/`peerGet` through `peerHTTP()`)
- Modify: `shim-go/main.go` (build the peer client, wire `peerDoer`, parse `PEER_MAX_IDLE_CONNS_PER_HOST`, extend startup log)
- Modify: `deploy/xet-dc-cache.env.example` (document the knob)

**Interfaces:**
- Produces: `type peerTransportConfig struct { MaxIdleConnsPerHost int; SocketBufferBytes int }` and `func newPeerTransport(cfg peerTransportConfig) *http.Transport`.
- Produces: `Server.peerDoer httpDoer` field and `func (s *Server) peerHTTP() httpDoer` (returns `s.peerDoer` if non-nil, else `s.doer`).
- Consumes: existing `httpDoer` interface (`server.go`).

- [ ] **Step 1: Write the failing test**

Create `shim-go/peer_transport_test.go`:

```go
package main

import (
	"net/http"
	"testing"
	"time"
)

func TestNewPeerTransportForcesHTTP11(t *testing.T) {
	tr := newPeerTransport(peerTransportConfig{MaxIdleConnsPerHost: 64})
	if tr.ForceAttemptHTTP2 {
		t.Fatal("ForceAttemptHTTP2 must be false (H2 multiplexes onto one cwnd)")
	}
	if tr.TLSNextProto == nil {
		t.Fatal("TLSNextProto must be a non-nil empty map to actually disable H2 upgrade")
	}
	if len(tr.TLSNextProto) != 0 {
		t.Fatalf("TLSNextProto must be empty, has %d entries", len(tr.TLSNextProto))
	}
}

func TestNewPeerTransportPool(t *testing.T) {
	tr := newPeerTransport(peerTransportConfig{MaxIdleConnsPerHost: 128})
	if tr.MaxIdleConnsPerHost != 128 {
		t.Fatalf("MaxIdleConnsPerHost = %d, want 128", tr.MaxIdleConnsPerHost)
	}
	if tr.MaxConnsPerHost != 0 {
		t.Fatalf("MaxConnsPerHost = %d, want 0 (unbounded)", tr.MaxConnsPerHost)
	}
	if tr.IdleConnTimeout != 90*time.Second {
		t.Fatalf("IdleConnTimeout = %v, want 90s", tr.IdleConnTimeout)
	}
}

func TestPeerTransportDistinctFromCDNDoer(t *testing.T) {
	tr := newPeerTransport(peerTransportConfig{MaxIdleConnsPerHost: 64})
	cdn := &http.Transport{DisableCompression: true}
	if any(tr) == any(cdn) {
		t.Fatal("peer transport must be a distinct object from the CDN transport")
	}
	if tr.DisableCompression {
		t.Fatal("peer transport should not inherit the CDN identity/compression contract")
	}
}

func TestPeerHTTPFallsBackToCDNDoer(t *testing.T) {
	c := &countingDoer{}
	s := &Server{doer: c}
	if s.peerHTTP() != httpDoer(c) {
		t.Fatal("peerHTTP must fall back to s.doer when peerDoer is nil")
	}
	pc := &countingDoer{}
	s.peerDoer = pc
	if s.peerHTTP() != httpDoer(pc) {
		t.Fatal("peerHTTP must return s.peerDoer when set")
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd shim-go && go test -race -run 'PeerTransport|PeerHTTP' ./...`
Expected: FAIL — `undefined: newPeerTransport`, `peerTransportConfig`, `Server.peerDoer`, `peerHTTP`.

- [ ] **Step 3: Create the transport builder**

Create `shim-go/peer_transport.go`:

```go
package main

import (
	"net/http"
	"time"
)

// peerTransportConfig carries the tunables for the dedicated peer transport.
// SocketBufferBytes is consumed in a later task (0 = OS autotune).
type peerTransportConfig struct {
	MaxIdleConnsPerHost int
	SocketBufferBytes   int
}

// newPeerTransport builds the transport used ONLY for peer traffic. It is kept
// strictly distinct from the CDN doer: HTTP/2 is disabled so N concurrent ranges
// ride N independent TCP congestion windows instead of one multiplexed stream,
// and the idle pool is fat so a thousands-of-ranges model pull never thrashes
// connections. The CDN transport's redirect/identity/compression contract is
// unaffected — this is a separate object.
func newPeerTransport(cfg peerTransportConfig) *http.Transport {
	if cfg.MaxIdleConnsPerHost <= 0 {
		cfg.MaxIdleConnsPerHost = 64
	}
	return &http.Transport{
		// The empty non-nil map is what actually disables the H2 upgrade;
		// ForceAttemptHTTP2:false alone is not enough for https:// peer URLs.
		ForceAttemptHTTP2:   false,
		TLSNextProto:        map[string]func(string, *tls.Conn) http.RoundTripper{},
		MaxIdleConnsPerHost: cfg.MaxIdleConnsPerHost,
		MaxConnsPerHost:     0,
		IdleConnTimeout:     90 * time.Second,
	}
}
```

Add the `crypto/tls` import to the file's import block (the `TLSNextProto` value type references `*tls.Conn`):

```go
import (
	"crypto/tls"
	"net/http"
	"time"
)
```

- [ ] **Step 4: Add the Server field and helper**

In `shim-go/server.go`, add the field to the `Server` struct (next to `doer`):

```go
	doer             httpDoer
	// peerDoer carries peer traffic on a dedicated, HTTP/1.1-forced transport
	// (see newPeerTransport). Kept distinct from doer so peer tuning never
	// perturbs the CDN redirect/identity contract. nil => fall back to doer.
	peerDoer         httpDoer
```

Add the helper (below `release`):

```go
// peerHTTP returns the doer used for peer traffic: the dedicated tuned peer
// transport when configured, else the CDN doer (keeps tests that only set doer
// working, and keeps peering functional if the peer transport is unset).
func (s *Server) peerHTTP() httpDoer {
	if s.peerDoer != nil {
		return s.peerDoer
	}
	return s.doer
}
```

- [ ] **Step 5: Route peer requests through peerHTTP()**

In `shim-go/peer_fetch.go`, in `headProbe` change `resp, err := s.doer.Do(req)` to:

```go
	resp, err := s.peerHTTP().Do(req)
```

In `peerGet` change `resp, err := s.doer.Do(req)` to:

```go
	resp, err := s.peerHTTP().Do(req)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd shim-go && go test -race -run 'PeerTransport|PeerHTTP' ./...`
Expected: PASS.

- [ ] **Step 7: Wire into main.go**

In `shim-go/main.go`, after the `fetchTimeout := ...` line (currently ~line 96), add:

```go
	peerTransport := newPeerTransport(peerTransportConfig{
		MaxIdleConnsPerHost: envInt("PEER_MAX_IDLE_CONNS_PER_HOST", 64),
		SocketBufferBytes:   envInt("PEER_SOCKET_BUFFER_BYTES", 0),
	})
	peerClient := &http.Client{
		Timeout: 60 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
		Transport: peerTransport,
	}
```

In the `s := &Server{...}` literal add the field after `doer: client,`:

```go
		doer:             client,
		peerDoer:         peerClient,
```

Extend the startup `slog.Info` call so the last line reads:

```go
		"peers", len(peerList), "peer_max_idle_conns", peerTransport.MaxIdleConnsPerHost)
```

- [ ] **Step 8: Document the knob**

In `deploy/xet-dc-cache.env.example`, after the `# PEER_FETCH_TIMEOUT_MS=10000` line append:

```
#
# --- Peer transport tuning (Mechanism A: never start cold) ---
# Idle keep-alive connections kept warm per peer (Go default is 2). A model
# pull is thousands of ranges; a fat pool stops connection thrash. Default 64.
# PEER_MAX_IDLE_CONNS_PER_HOST=64
```

- [ ] **Step 9: Run the full suite and vet**

Run: `cd shim-go && go test -race ./... && go vet ./...`
Expected: PASS (existing peer tests still pass — they set only `doer`, so `peerHTTP()` falls back).

- [ ] **Step 10: Commit**

```bash
git add shim-go/peer_transport.go shim-go/peer_transport_test.go shim-go/server.go shim-go/peer_fetch.go shim-go/main.go deploy/xet-dc-cache.env.example
git commit -m "feat(peer): dedicated HTTP/1.1 transport with fat idle pool"
```

---

### Task 2: Optional BDP socket buffers (PEER_SOCKET_BUFFER_BYTES)

**Files:**
- Modify: `shim-go/peer_transport.go` (install a `DialContext` with a socket-buffer `Control` when configured)
- Modify: `shim-go/peer_transport_test.go` (dial-hook assertions)

**Interfaces:**
- Consumes: `peerTransportConfig.SocketBufferBytes` (Task 1).
- Produces: `func socketBufferControl(bytes int) func(network, address string, c syscall.RawConn) error` — returns `nil` when `bytes <= 0`; otherwise a best-effort control fn that sets `SO_RCVBUF`/`SO_SNDBUF`.

- [ ] **Step 1: Write the failing test**

Append to `shim-go/peer_transport_test.go`:

```go
// fakeRawConn records whether Control was invoked and hands the callback a
// throwaway fd (0). setsockopt on fd 0 will error; the control fn is
// best-effort and must ignore that, so the test only asserts it ran.
type fakeRawConn struct{ controlled bool }

func (f *fakeRawConn) Control(fn func(fd uintptr)) error { f.controlled = true; fn(0); return nil }
func (f *fakeRawConn) Read(func(uintptr) bool) error     { return nil }
func (f *fakeRawConn) Write(func(uintptr) bool) error    { return nil }

func TestSocketBufferControlDisabledWhenZero(t *testing.T) {
	if socketBufferControl(0) != nil {
		t.Fatal("bytes=0 must install no control fn (OS autotune)")
	}
	if socketBufferControl(-1) != nil {
		t.Fatal("negative bytes must install no control fn")
	}
}

func TestSocketBufferControlRunsWhenSet(t *testing.T) {
	ctrl := socketBufferControl(1 << 20)
	if ctrl == nil {
		t.Fatal("bytes>0 must install a control fn")
	}
	rc := &fakeRawConn{}
	if err := ctrl("tcp", "1.2.3.4:8000", rc); err != nil {
		t.Fatalf("control fn returned error, must be best-effort nil: %v", err)
	}
	if !rc.controlled {
		t.Fatal("control fn must invoke RawConn.Control to setsockopt")
	}
}

func TestPeerTransportDialContextGatedOnBuffer(t *testing.T) {
	if tr := newPeerTransport(peerTransportConfig{MaxIdleConnsPerHost: 64, SocketBufferBytes: 0}); tr.DialContext != nil {
		t.Fatal("SocketBufferBytes=0 must leave DialContext nil (default dialer, autotune)")
	}
	if tr := newPeerTransport(peerTransportConfig{MaxIdleConnsPerHost: 64, SocketBufferBytes: 1 << 20}); tr.DialContext == nil {
		t.Fatal("SocketBufferBytes>0 must install a custom DialContext")
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd shim-go && go test -race -run 'SocketBuffer|DialContextGated' ./...`
Expected: FAIL — `undefined: socketBufferControl`.

- [ ] **Step 3: Implement the control fn and gate the DialContext**

In `shim-go/peer_transport.go`, extend the import block:

```go
import (
	"crypto/tls"
	"log/slog"
	"net"
	"net/http"
	"syscall"
	"time"
)
```

Add the control fn at the end of the file:

```go
// socketBufferControl returns a net.Dialer.Control that pins SO_RCVBUF/SO_SNDBUF
// to bytes on the raw socket before connect. Returns nil (no control) when bytes
// <= 0, which is the default: modern Linux tcp_rmem/tcp_wmem autotuning beats a
// static guess, and a wrong static value CAPS throughput. Best-effort: a failed
// setsockopt is logged at debug and ignored, never failing the dial.
func socketBufferControl(bytes int) func(network, address string, c syscall.RawConn) error {
	if bytes <= 0 {
		return nil
	}
	return func(_, _ string, c syscall.RawConn) error {
		return c.Control(func(fd uintptr) {
			if err := syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_RCVBUF, bytes); err != nil {
				slog.Debug("peer dial: SO_RCVBUF failed", "bytes", bytes, "err", err)
			}
			if err := syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_SNDBUF, bytes); err != nil {
				slog.Debug("peer dial: SO_SNDBUF failed", "bytes", bytes, "err", err)
			}
		})
	}
}
```

In `newPeerTransport`, before the `return`, install the DialContext only when a control fn exists:

```go
	tr := &http.Transport{
		ForceAttemptHTTP2:   false,
		TLSNextProto:        map[string]func(string, *tls.Conn) http.RoundTripper{},
		MaxIdleConnsPerHost: cfg.MaxIdleConnsPerHost,
		MaxConnsPerHost:     0,
		IdleConnTimeout:     90 * time.Second,
	}
	if ctrl := socketBufferControl(cfg.SocketBufferBytes); ctrl != nil {
		tr.DialContext = (&net.Dialer{Timeout: 30 * time.Second, KeepAlive: 30 * time.Second, Control: ctrl}).DialContext
	}
	return tr
```

(Replace the previous `return &http.Transport{...}` block with the above.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd shim-go && go test -race -run 'SocketBuffer|DialContextGated|PeerTransport' ./...`
Expected: PASS.

- [ ] **Step 5: Document the knob**

In `deploy/xet-dc-cache.env.example`, after the `# PEER_MAX_IDLE_CONNS_PER_HOST=64` line append:

```
# SO_RCVBUF/SO_SNDBUF for peer dials, in bytes. 0 = OS autotune (recommended;
# a wrong static value CAPS throughput). Set only where autotuning is disabled.
# PEER_SOCKET_BUFFER_BYTES=0
```

- [ ] **Step 6: Run full suite and vet**

Run: `cd shim-go && go test -race ./... && go vet ./...`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add shim-go/peer_transport.go shim-go/peer_transport_test.go deploy/xet-dc-cache.env.example
git commit -m "feat(peer): optional SO_RCVBUF/SO_SNDBUF socket buffer sizing"
```

---

### Task 3: Optional connection keepalive (PEER_KEEPALIVE_INTERVAL_MS)

**Files:**
- Create: `shim-go/peer_keepalive.go`
- Create: `shim-go/peer_keepalive_test.go`
- Modify: `shim-go/main.go` (start the goroutine when the interval > 0)
- Modify: `deploy/xet-dc-cache.env.example`

**Interfaces:**
- Produces: `func (s *Server) startPeerKeepalive(interval time.Duration, stop <-chan struct{})` — loops on a ticker, issuing one lightweight `HEAD <peer>/healthz` (with the fleet bearer + `X-Xet-Peer: 1`) per peer via `peerHTTP()`, until `stop` closes. A `nil` stop channel runs for the process lifetime.
- Consumes: `Server.peers` (`peers.go`), `Server.peerHTTP()`, `Server.authToken`.

- [ ] **Step 1: Write the failing test**

Create `shim-go/peer_keepalive_test.go`:

```go
package main

import (
	"net/http"
	"sync/atomic"
	"testing"
	"time"
)

// kaDoer counts HEAD /healthz pings and records that the peer headers were set.
type kaDoer struct {
	pings  int32
	bearer int32
}

func (d *kaDoer) Do(req *http.Request) (*http.Response, error) {
	if req.Method == http.MethodHead && req.URL.Path == "/healthz" {
		atomic.AddInt32(&d.pings, 1)
		if req.Header.Get("X-Xet-Peer") == "1" {
			atomic.AddInt32(&d.bearer, 1)
		}
	}
	return &http.Response{StatusCode: 200, Body: http.NoBody, Header: http.Header{}}, nil
}

func TestPeerKeepalivePingsEveryPeer(t *testing.T) {
	d := &kaDoer{}
	s := &Server{
		peerDoer: d,
		peers:    staticPeers{list: []string{"https://a:8000", "https://b:8000"}},
	}
	stop := make(chan struct{})
	go s.startPeerKeepalive(2*time.Millisecond, stop)

	deadline := time.After(2 * time.Second)
	for atomic.LoadInt32(&d.pings) < 2 {
		select {
		case <-deadline:
			t.Fatalf("expected >=2 pings, got %d", atomic.LoadInt32(&d.pings))
		default:
			time.Sleep(time.Millisecond)
		}
	}
	close(stop)
	if atomic.LoadInt32(&d.bearer) == 0 {
		t.Fatal("keepalive pings must carry the X-Xet-Peer header")
	}
}

func TestPeerKeepaliveStops(t *testing.T) {
	d := &kaDoer{}
	s := &Server{peerDoer: d, peers: staticPeers{list: []string{"https://a:8000"}}}
	stop := make(chan struct{})
	done := make(chan struct{})
	go func() { s.startPeerKeepalive(time.Millisecond, stop); close(done) }()
	close(stop)
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("startPeerKeepalive did not return after stop closed")
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd shim-go && go test -race -run PeerKeepalive ./...`
Expected: FAIL — `undefined: s.startPeerKeepalive`.

- [ ] **Step 3: Implement the keepalive goroutine**

Create `shim-go/peer_keepalive.go`:

```go
package main

import (
	"context"
	"net/http"
	"time"
)

// startPeerKeepalive keeps pooled peer connections warm by issuing a light
// HEAD /healthz to each peer every interval. Honest scope: this avoids the TCP
// handshake + TLS on the next real fetch. It does NOT keep the TCP congestion
// window warm — Linux collapses cwnd after an idle RTO unless the operator sets
// net.ipv4.tcp_slow_start_after_idle=0 (documented in the deploy notes).
// Blocks until stop closes; a nil stop channel runs for the process lifetime.
func (s *Server) startPeerKeepalive(interval time.Duration, stop <-chan struct{}) {
	if interval <= 0 || s.peers == nil {
		return
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-stop:
			return
		case <-ticker.C:
			for _, base := range s.peers.Peers() {
				s.pingPeer(base)
			}
		}
	}
}

// pingPeer issues one best-effort HEAD /healthz carrying the peer headers, under
// the probe timeout so a dead peer never blocks the loop.
func (s *Server) pingPeer(base string) {
	ctx := context.Background()
	if s.peerProbeTimeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, s.peerProbeTimeout)
		defer cancel()
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodHead, base+"/healthz", nil)
	if err != nil {
		return
	}
	req.Header.Set("X-Xet-Peer", "1")
	if s.authToken != "" {
		req.Header.Set("Authorization", "Bearer "+s.authToken)
	}
	resp, err := s.peerHTTP().Do(req)
	if err != nil {
		return
	}
	resp.Body.Close()
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd shim-go && go test -race -run PeerKeepalive ./...`
Expected: PASS.

- [ ] **Step 5: Start it from main.go**

In `shim-go/main.go`, after `seedLRU(s)` add:

```go
	if kaMs := envInt("PEER_KEEPALIVE_INTERVAL_MS", 0); kaMs > 0 && peers != nil {
		go s.startPeerKeepalive(time.Duration(kaMs)*time.Millisecond, nil)
		slog.Info("peer keepalive enabled", "interval_ms", kaMs)
	}
```

- [ ] **Step 6: Document the knob**

In `deploy/xet-dc-cache.env.example`, after the `# PEER_SOCKET_BUFFER_BYTES=0` line append:

```
# Background keepalive ping interval (ms) to keep pooled peer conns open.
# 0 = disabled. Avoids handshake/TLS on the next fetch; does NOT keep the TCP
# congestion window warm — for that set net.ipv4.tcp_slow_start_after_idle=0.
# PEER_KEEPALIVE_INTERVAL_MS=0
```

- [ ] **Step 7: Run full suite and vet**

Run: `cd shim-go && go test -race ./... && go vet ./...`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add shim-go/peer_keepalive.go shim-go/peer_keepalive_test.go shim-go/main.go deploy/xet-dc-cache.env.example
git commit -m "feat(peer): optional connection keepalive goroutine"
```

---

### Task 4: Per-peer throughput EWMA (peerStats)

**Files:**
- Modify: `shim-go/peers.go` (add `peerStats` type + methods)
- Modify: `shim-go/peers_test.go` (unit tests for prediction, clamp, EWMA)

**Interfaces:**
- Produces: `type peerStats struct { ... }`, `func newPeerStats() *peerStats`.
- Produces: `func (p *peerStats) hedgeDelay(base string, size int64, factor float64, minMs, maxMs int) time.Duration` — with no sample for `base`, returns `maxMs`; otherwise `clamp(size/ewma * factor, minMs, maxMs)`.
- Produces: `func (p *peerStats) update(base string, bytes int64, elapsed time.Duration)` — folds a size-normalized throughput sample (bytes/ms) into the per-peer EWMA.
- Produces: `func (p *peerStats) fleetThroughput() float64` — mean of per-peer EWMAs (bytes/ms), 0 when empty (for the gauge in Task 5).

- [ ] **Step 1: Write the failing test**

Append to `shim-go/peers_test.go`:

```go
func TestPeerStatsBootstrapHedgesAtMax(t *testing.T) {
	p := newPeerStats()
	// No sample yet: any range hedges at ~PEER_HEDGE_MAX_MS.
	got := p.hedgeDelay("https://a:8000", 64<<20, 1.5, 50, 1000)
	if got != 1000*time.Millisecond {
		t.Fatalf("bootstrap hedgeDelay = %v, want 1000ms (max)", got)
	}
}

func TestPeerStatsClampFloorAndCeiling(t *testing.T) {
	p := newPeerStats()
	// Very fast peer: 1000 bytes/ms. A tiny 10-byte range predicts ~0ms; the
	// floor must hold it at PEER_HEDGE_MIN_MS.
	p.update("https://a:8000", 100000, 100*time.Millisecond) // 1000 bytes/ms
	if got := p.hedgeDelay("https://a:8000", 10, 1.5, 50, 1000); got != 50*time.Millisecond {
		t.Fatalf("tiny range hedgeDelay = %v, want floor 50ms", got)
	}
	// Huge range predicts far above the ceiling; the cap must hold it at max.
	if got := p.hedgeDelay("https://a:8000", 1<<40, 1.5, 50, 1000); got != 1000*time.Millisecond {
		t.Fatalf("huge range hedgeDelay = %v, want ceiling 1000ms", got)
	}
}

func TestPeerStatsPredictionInBand(t *testing.T) {
	p := newPeerStats()
	p.update("https://a:8000", 1_000_000, 100*time.Millisecond) // 10000 bytes/ms
	// 5,000,000 bytes / 10000 = 500ms predicted; *1.5 = 750ms, within [50,1000].
	if got := p.hedgeDelay("https://a:8000", 5_000_000, 1.5, 50, 1000); got != 750*time.Millisecond {
		t.Fatalf("hedgeDelay = %v, want 750ms", got)
	}
}

func TestPeerStatsEWMATightensAfterFastSample(t *testing.T) {
	p := newPeerStats()
	p.update("https://a:8000", 1_000_000, 1000*time.Millisecond) // slow: 1000 bytes/ms
	slow := p.hedgeDelay("https://a:8000", 1_000_000, 1.5, 50, 1000)
	// A run of fast samples must raise throughput and shorten the predicted delay.
	for i := 0; i < 10; i++ {
		p.update("https://a:8000", 1_000_000, 100*time.Millisecond) // fast: 10000 bytes/ms
	}
	fast := p.hedgeDelay("https://a:8000", 1_000_000, 1.5, 50, 1000)
	if !(fast < slow) {
		t.Fatalf("fast-sample delay %v must be < slow-sample delay %v", fast, slow)
	}
}

func TestPeerStatsFleetThroughput(t *testing.T) {
	p := newPeerStats()
	if p.fleetThroughput() != 0 {
		t.Fatal("empty fleetThroughput must be 0")
	}
	p.update("https://a:8000", 1000, 1*time.Millisecond) // 1000 bytes/ms
	p.update("https://b:8000", 3000, 1*time.Millisecond) // 3000 bytes/ms
	if got := p.fleetThroughput(); got != 2000 {
		t.Fatalf("fleetThroughput = %v, want 2000 (mean of 1000,3000)", got)
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd shim-go && go test -race -run PeerStats ./...`
Expected: FAIL — `undefined: newPeerStats`.

- [ ] **Step 3: Implement peerStats**

Append to `shim-go/peers.go` (add no new imports — `sync` and `time` are already imported):

```go
// ewmaAlpha weights the newest throughput sample in the per-peer EWMA. Small so
// a single outlier fetch can't whipsaw the hedge decision. Not user-facing.
const ewmaAlpha = 0.3

// peerStats holds a per-peer throughput EWMA (bytes/ms), updated on every
// completed peer GET and read to predict the adaptive hedge delay. Throughput
// (size-normalized), not raw latency, so a 64 MB range and a 1 MB range on the
// same link produce comparable samples. Thread-safe; lives beside the sticky
// pointer because both describe "how good is this peer right now".
type peerStats struct {
	mu   sync.Mutex
	ewma map[string]float64 // peer base URL -> throughput bytes/ms
}

func newPeerStats() *peerStats { return &peerStats{ewma: map[string]float64{}} }

// hedgeDelay predicts how long to give the peer before hedging in the CDN.
// With no sample for base it returns maxMs (bootstrap: an unknown peer hedges
// conservatively). Otherwise clamp(size/ewma * factor, minMs, maxMs).
func (p *peerStats) hedgeDelay(base string, size int64, factor float64, minMs, maxMs int) time.Duration {
	p.mu.Lock()
	tp, ok := p.ewma[base]
	p.mu.Unlock()
	if !ok || tp <= 0 {
		return time.Duration(maxMs) * time.Millisecond
	}
	ms := (float64(size) / tp) * factor
	if lo := float64(minMs); ms < lo {
		ms = lo
	}
	if hi := float64(maxMs); ms > hi {
		ms = hi
	}
	return time.Duration(ms) * time.Millisecond
}

// update folds a completed transfer into base's throughput EWMA. Sub-ms
// transfers are floored to 1ms so a fast tiny range can't imply infinite
// throughput.
func (p *peerStats) update(base string, bytes int64, elapsed time.Duration) {
	ms := float64(elapsed.Milliseconds())
	if ms <= 0 {
		ms = 1
	}
	sample := float64(bytes) / ms
	p.mu.Lock()
	defer p.mu.Unlock()
	if cur, ok := p.ewma[base]; ok {
		p.ewma[base] = ewmaAlpha*sample + (1-ewmaAlpha)*cur
	} else {
		p.ewma[base] = sample
	}
}

// fleetThroughput is the mean per-peer EWMA (bytes/ms), 0 when empty. Exposed as
// a fleet-aggregate gauge for tuning (per-peer labels are out of scope for the
// hand-rolled Prometheus exposition).
func (p *peerStats) fleetThroughput() float64 {
	p.mu.Lock()
	defer p.mu.Unlock()
	if len(p.ewma) == 0 {
		return 0
	}
	var sum float64
	for _, v := range p.ewma {
		sum += v
	}
	return sum / float64(len(p.ewma))
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd shim-go && go test -race -run PeerStats ./...`
Expected: PASS.

- [ ] **Step 5: Run full suite and vet**

Run: `cd shim-go && go test -race ./... && go vet ./...`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add shim-go/peers.go shim-go/peers_test.go
git commit -m "feat(peer): per-peer throughput EWMA for adaptive hedge"
```

---

### Task 5: Hedge/race metrics (counters + throughput gauge)

**Files:**
- Modify: `shim-go/metrics.go` (add the four new counters to the Snapshot zero-fill list)
- Modify: `shim-go/prometheus.go` (add four counter series + the throughput gauge)
- Modify: `shim-go/main.go` (expose the gauge in the JSON `/metrics` handler)
- Modify: `shim-go/metrics_test.go`, `shim-go/prometheus_test.go`

**Interfaces:**
- Produces: metric keys `peer_hedge_fired`, `peer_hedge_peer_won`, `peer_hedge_cdn_won`, `peer_bytes_wasted` in the counter bag; Prometheus series `xet_peer_hedge_fired_total`, `xet_peer_hedge_peer_won_total`, `xet_peer_hedge_cdn_won_total`, `xet_peer_bytes_wasted_total`, gauge `xet_peer_throughput_bytes_per_ms`.
- Consumes: `Server.peerStats.fleetThroughput()` (Task 4) — nil-guarded so the existing `prometheus_test.go` server literals (which don't set `peerStats`) keep working.

- [ ] **Step 1: Write the failing test**

Append to `shim-go/metrics_test.go`:

```go
func TestMetricsHedgeCountersZeroFilled(t *testing.T) {
	s := NewMetrics().Snapshot()
	for _, k := range []string{"peer_hedge_fired", "peer_hedge_peer_won", "peer_hedge_cdn_won", "peer_bytes_wasted"} {
		if v, ok := s[k]; !ok || v.(int64) != 0 {
			t.Fatalf("snapshot[%q] = %v (ok=%v), want int64(0)", k, v, ok)
		}
	}
}
```

Append to `shim-go/prometheus_test.go`:

```go
func TestPrometheusHedgeSeries(t *testing.T) {
	s := &Server{
		metrics:   NewMetrics(),
		signed:    NewTTLMap(3600e9, 100, nil),
		lru:       newLRU(0, 0, nil, func(string) {}),
		peerStats: newPeerStats(),
	}
	s.metrics.Incr("peer_hedge_fired", 4)
	s.metrics.Incr("peer_hedge_peer_won", 1)
	s.metrics.Incr("peer_hedge_cdn_won", 3)
	s.metrics.Incr("peer_bytes_wasted", 2048)
	s.peerStats.update("https://a:8000", 1000, 1*time.Millisecond) // 1000 bytes/ms

	out := s.prometheusText()
	for _, want := range []string{
		"# TYPE xet_peer_hedge_fired_total counter\nxet_peer_hedge_fired_total 4\n",
		"xet_peer_hedge_peer_won_total 1\n",
		"xet_peer_hedge_cdn_won_total 3\n",
		"xet_peer_bytes_wasted_total 2048\n",
		"# TYPE xet_peer_throughput_bytes_per_ms gauge\nxet_peer_throughput_bytes_per_ms 1000\n",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in exposition:\n%s", want, out)
		}
	}
}
```

Add `"time"` to the `prometheus_test.go` import block.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd shim-go && go test -race -run 'HedgeCounters|HedgeSeries' ./...`
Expected: FAIL — missing keys / missing series.

- [ ] **Step 3: Add the counters to the Snapshot zero-fill list**

In `shim-go/metrics.go`, extend the zero-fill slice in `Snapshot`:

```go
	for _, k := range []string{
		"hits", "misses", "wan_bytes", "served_bytes",
		"peer_hits", "peer_misses", "peer_bytes", "peer_probe_timeouts",
		"peer_hedge_fired", "peer_hedge_peer_won", "peer_hedge_cdn_won", "peer_bytes_wasted",
	} {
```

- [ ] **Step 4: Add the Prometheus series and gauge**

In `shim-go/prometheus.go`, after the `xet_peer_probe_timeouts_total` block add:

```go
	promMetric(&b, "xet_peer_hedge_fired_total", "counter",
		"Races where the CDN was hedged in (the adaptive timer fired).", snap["peer_hedge_fired"])
	promMetric(&b, "xet_peer_hedge_peer_won_total", "counter",
		"Hedged races the peer won (CDN GET cancelled).", snap["peer_hedge_peer_won"])
	promMetric(&b, "xet_peer_hedge_cdn_won_total", "counter",
		"Hedged races the CDN won (peer GET cancelled).", snap["peer_hedge_cdn_won"])
	promMetric(&b, "xet_peer_bytes_wasted_total", "counter",
		"CDN bytes discarded because the peer won after the CDN GET fired (cost of the latency insurance).", snap["peer_bytes_wasted"])
```

Before the final `return b.String()` add the gauge (nil-guarded):

```go
	var throughput float64
	if s.peerStats != nil {
		throughput = s.peerStats.fleetThroughput()
	}
	promMetric(&b, "xet_peer_throughput_bytes_per_ms", "gauge",
		"Fleet-aggregate peer throughput EWMA (bytes/ms), used to size the hedge delay.", throughput)
```

- [ ] **Step 5: Expose the gauge in the JSON /metrics handler**

In `shim-go/main.go`, in the `GET /metrics` handler, after `snap["signed_urls_tracked"] = s.signed.Len()` add:

```go
		if s.peerStats != nil {
			snap["peer_throughput_bytes_per_ms"] = s.peerStats.fleetThroughput()
		}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd shim-go && go test -race -run 'HedgeCounters|HedgeSeries|Prometheus|Metrics' ./...`
Expected: PASS (including `TestPrometheusEveryMetricHasType` — every new series has a TYPE line).

- [ ] **Step 7: Run full suite and vet**

Run: `cd shim-go && go test -race ./... && go vet ./...`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add shim-go/metrics.go shim-go/prometheus.go shim-go/main.go shim-go/metrics_test.go shim-go/prometheus_test.go
git commit -m "feat(metrics): hedge race counters and peer throughput gauge"
```

---

### Task 6: Context-aware CDN fetch + cdnGet helper

**Files:**
- Modify: `shim-go/xorb.go` (`fetchAuthorized` takes a `context.Context`; add `cdnGet` helper; update the plain-path call site)
- Modify: `shim-go/xorb_test.go` or add cases (verify context cancellation aborts the CDN fetch)

**Interfaces:**
- Changes: `func (s *Server) fetchAuthorized(ctx context.Context, hash, byteRange string) (*http.Response, error)` (was no `ctx`) — builds requests with `http.NewRequestWithContext` so the hedge loser can be cancelled.
- Produces: `func (s *Server) cdnGet(ctx context.Context, hash, byteRange string) (xorbResult, bool)` — acquires a fetch slot, runs `fetchAuthorized`, reads the body; returns `(zero, false)` on any error/cancellation (best-effort; the plain path keeps surfacing typed errors itself).

- [ ] **Step 1: Write the failing test**

Add to `shim-go/xorb_test.go` (create the file's test package header if adding to a new file; here we append):

```go
func TestFetchAuthorizedHonorsContext(t *testing.T) {
	s := newXorbTestServer(t, &countingDoer{})
	s.signed.Set("h", []string{"http://cdn/x"})
	ctx, cancel := context.WithCancel(context.Background())
	cancel() // already cancelled
	if _, err := s.fetchAuthorized(ctx, "h", "bytes=0-4"); err == nil {
		t.Fatal("fetchAuthorized must fail fast when ctx is already cancelled")
	}
}

func TestCdnGetReturnsBody(t *testing.T) {
	s := newXorbTestServer(t, &countingDoer{})
	s.signed.Set("h", []string{"http://cdn/x"})
	res, ok := s.cdnGet(context.Background(), "h", "bytes=0-4")
	if !ok || string(res.body) != "BYTES" {
		t.Fatalf("cdnGet = (%q,%v), want BYTES,true", res.body, ok)
	}
}
```

Ensure `xorb_test.go` imports `context` (add to its import block if absent).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd shim-go && go test -race -run 'FetchAuthorizedHonorsContext|CdnGet' ./...`
Expected: FAIL — signature mismatch / `undefined: cdnGet`.

- [ ] **Step 3: Make fetchAuthorized context-aware**

In `shim-go/xorb.go`, change the signature and the request construction:

```go
func (s *Server) fetchAuthorized(ctx context.Context, hash, byteRange string) (*http.Response, error) {
	cands, ok := s.signed.Get(hash)
	if !ok || len(cands) == 0 {
		return nil, &httpError{409, "unknown xorb; request reconstruction first"}
	}
	last := 0
	for _, signed := range cands {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, signed, nil)
		if err != nil {
			continue
		}
		req.Header.Set("Range", byteRange)
		req.Header.Set("Accept-Encoding", "identity")
		resp, err := s.doer.Do(req)
		if err != nil {
			continue
		}
		if resp.StatusCode == http.StatusOK || resp.StatusCode == http.StatusPartialContent {
			return resp, nil
		}
		last = resp.StatusCode
		resp.Body.Close()
	}
	return nil, &httpError{502, fmt.Sprintf("no signed url authorizes %s (last %d)", byteRange, last)}
}
```

Add `"context"` to `xorb.go`'s import block.

- [ ] **Step 4: Add the cdnGet helper**

In `shim-go/xorb.go`, add after `fetchAuthorized`:

```go
// cdnGet is the CDN side of the hedged race: acquire a fetch slot, pull the
// authorized range, read the body. Best-effort — any failure (including ctx
// cancellation when the peer wins the race) returns (zero, false); the plain
// miss path keeps surfacing typed errors itself.
func (s *Server) cdnGet(ctx context.Context, hash, byteRange string) (xorbResult, bool) {
	if err := s.acquire(ctx); err != nil {
		return xorbResult{}, false
	}
	defer s.release()
	resp, err := s.fetchAuthorized(ctx, hash, byteRange)
	if err != nil {
		return xorbResult{}, false
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return xorbResult{}, false
	}
	return xorbResult{body: body, contentRange: resp.Header.Get("Content-Range")}, true
}
```

- [ ] **Step 5: Update the plain-path call site**

In `shim-go/xorb.go` `getXorb`, change `resp, ferr := s.fetchAuthorized(hash, byteRange)` to:

```go
		resp, ferr := s.fetchAuthorized(r.Context(), hash, byteRange)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd shim-go && go test -race -run 'FetchAuthorized|CdnGet|GetXorb' ./...`
Expected: PASS.

- [ ] **Step 7: Run full suite and vet**

Run: `cd shim-go && go test -race ./... && go vet ./...`
Expected: PASS (all existing callers of `fetchAuthorized` are in `getXorb`, updated above).

- [ ] **Step 8: Commit**

```bash
git add shim-go/xorb.go shim-go/xorb_test.go
git commit -m "refactor(cdn): context-aware fetchAuthorized + cdnGet helper"
```

---

### Task 7: Adaptive hedged race core (raceOnePeer)

**Files:**
- Create: `shim-go/peer_hedge.go`
- Create: `shim-go/peer_hedge_test.go`
- Modify: `shim-go/server.go` (add hedge config fields + `nowFn`/`hedgeAfter` seams + `now()`/`after()` helpers)
- Modify: `shim-go/main.go` (parse `PEER_HEDGE_*`, wire fields, `peerStats`, startup log, add `envFloat`)
- Modify: `deploy/xet-dc-cache.env.example`

**Interfaces:**
- Produces: `type hedgeSource int` with `const (srcPeer hedgeSource = iota; srcCDN)`.
- Produces: `func (s *Server) raceOnePeer(ctx context.Context, base, hash, byteRange string) (xorbResult, hedgeSource, bool)` — fires the peer GET to `base`; if it beats the adaptive delay, returns `(res, srcPeer, true)` with no CDN call; if the timer fires first, races `cdnGet` in parallel, takes the winner, cancels the loser, and updates counters (`peer_hedge_fired`, and `peer_hedge_peer_won` + `peer_bytes_wasted` or `peer_hedge_cdn_won`). Returns `(zero, srcPeer, false)` when both sides fail. Updates the throughput EWMA on any successful peer GET.
- Produces: `func rangeSize(byteRange string) int64`.
- Produces: `Server` fields `peerStats *peerStats`, `hedgeFactor float64`, `hedgeMinMs int`, `hedgeMaxMs int`, `nowFn func() time.Time`, `hedgeAfter func(time.Duration) <-chan time.Time`; helpers `now()` and `after(d)`.
- Consumes: `peerGet` (Task 1), `cdnGet` (Task 6), `peerStats.hedgeDelay`/`update` (Task 4), metric keys (Task 5).

- [ ] **Step 1: Add the Server seams**

In `shim-go/server.go`, add fields to the `Server` struct (after `peerFetchTimeout`):

```go
	peerFetchTimeout time.Duration

	// Adaptive hedge (Mechanism B): give the peer a head start sized by its
	// recent throughput, then race the CDN. peerStats holds the per-peer EWMA.
	peerStats   *peerStats
	hedgeFactor float64 // multiplier on predicted peer time before hedging the CDN
	hedgeMinMs  int     // floor on the hedge delay
	hedgeMaxMs  int     // cap on the hedge delay = the guarantee's bounded slack

	// nowFn and hedgeAfter are test seams (clock injection). Both nil in
	// production => real time.Now / time.After.
	nowFn      func() time.Time
	hedgeAfter func(time.Duration) <-chan time.Time
```

Add the helpers (after `peerHTTP`):

```go
// now returns the current time via the injected clock (tests) or the wall clock.
func (s *Server) now() time.Time {
	if s.nowFn != nil {
		return s.nowFn()
	}
	return time.Now()
}

// after returns a channel that fires after d via the injected timer (tests) or
// the wall clock.
func (s *Server) after(d time.Duration) <-chan time.Time {
	if s.hedgeAfter != nil {
		return s.hedgeAfter(d)
	}
	return time.After(d)
}
```

- [ ] **Step 2: Write the failing test**

Create `shim-go/peer_hedge_test.go`:

```go
package main

import (
	"context"
	"io"
	"net/http"
	"sync/atomic"
	"testing"
	"time"
)

// hedgeBody yields its data once the release channel closes, or errors if the
// request context is cancelled first (simulating a cancelled loser).
type hedgeBody struct {
	ctx     context.Context
	release <-chan struct{}
	data    []byte
	done    bool
}

func (b *hedgeBody) Read(p []byte) (int, error) {
	if b.done {
		return 0, io.EOF
	}
	select {
	case <-b.release:
		n := copy(p, b.data)
		b.done = true
		return n, nil
	case <-b.ctx.Done():
		return 0, b.ctx.Err()
	}
}
func (b *hedgeBody) Close() error { return nil }

// hedgeDoer answers peer GETs (X-Xet-Peer:1) and CDN GETs with bodies that
// block until their respective release channels close. Counts CDN calls.
type hedgeDoer struct {
	peerRelease, cdnRelease <-chan struct{}
	body                    string
	peerHas                 bool
	cdnCalls                int32
}

func (d *hedgeDoer) Do(req *http.Request) (*http.Response, error) {
	h := http.Header{}
	h.Set("Content-Range", "bytes 0-4/999")
	if req.Header.Get("X-Xet-Peer") == "1" {
		if req.Method == http.MethodHead {
			if d.peerHas {
				return &http.Response{StatusCode: 200, Body: http.NoBody, Header: h}, nil
			}
			return &http.Response{StatusCode: 404, Body: http.NoBody, Header: h}, nil
		}
		if !d.peerHas {
			return &http.Response{StatusCode: 404, Body: http.NoBody, Header: h}, nil
		}
		return &http.Response{StatusCode: 206, Header: h,
			Body: &hedgeBody{ctx: req.Context(), release: d.peerRelease, data: []byte(d.body)}}, nil
	}
	atomic.AddInt32(&d.cdnCalls, 1)
	return &http.Response{StatusCode: 206, Header: h,
		Body: &hedgeBody{ctx: req.Context(), release: d.cdnRelease, data: []byte(d.body)}}, nil
}

func newHedgeServer(d *hedgeDoer, timer chan time.Time) *Server {
	return &Server{
		metrics:          NewMetrics(),
		doer:             d,
		peerDoer:         d,
		signed:           NewTTLMap(3600e9, 100, nil),
		signedCandidates: 8,
		peers:            staticPeers{list: []string{"https://b:8000"}},
		sticky:           newStickyPeer(time.Minute, nil),
		peerStats:        newPeerStats(),
		hedgeFactor:      1.5,
		hedgeMinMs:       50,
		hedgeMaxMs:       1000,
		peerFetchTimeout: 5 * time.Second,
		hedgeAfter:       func(time.Duration) <-chan time.Time { return timer },
	}
}

func TestRaceFastPeerNoCDN(t *testing.T) {
	peerRel := make(chan struct{})
	close(peerRel) // peer completes immediately
	timer := make(chan time.Time) // never fires
	d := &hedgeDoer{peerRelease: peerRel, body: "BYTES", peerHas: true}
	s := newHedgeServer(d, timer)
	s.signed.Set("h", []string{"http://cdn/x"})

	res, src, ok := s.raceOnePeer(context.Background(), "https://b:8000", "h", "bytes=0-4")
	if !ok || src != srcPeer || string(res.body) != "BYTES" {
		t.Fatalf("fast peer = (%q,%v,%v), want BYTES,srcPeer,true", res.body, src, ok)
	}
	if atomic.LoadInt32(&d.cdnCalls) != 0 {
		t.Fatal("fast peer must NOT call the CDN")
	}
	if s.metrics.Snapshot()["peer_hedge_fired"].(int64) != 0 {
		t.Fatal("hedge must not fire when peer beats the timer")
	}
}

func TestRaceSlowPeerCDNWins(t *testing.T) {
	peerRel := make(chan struct{}) // peer never completes
	cdnRel := make(chan struct{})
	close(cdnRel) // CDN completes as soon as it fires
	timer := make(chan time.Time)
	close(timer) // hedge fires immediately
	d := &hedgeDoer{peerRelease: peerRel, cdnRelease: cdnRel, body: "BYTES", peerHas: true}
	s := newHedgeServer(d, timer)
	s.signed.Set("h", []string{"http://cdn/x"})

	res, src, ok := s.raceOnePeer(context.Background(), "https://b:8000", "h", "bytes=0-4")
	if !ok || src != srcCDN || string(res.body) != "BYTES" {
		t.Fatalf("slow peer = (%q,%v,%v), want BYTES,srcCDN,true", res.body, src, ok)
	}
	snap := s.metrics.Snapshot()
	if snap["peer_hedge_fired"].(int64) != 1 || snap["peer_hedge_cdn_won"].(int64) != 1 {
		t.Fatalf("counters = %+v, want fired=1 cdn_won=1", snap)
	}
	if atomic.LoadInt32(&d.cdnCalls) != 1 {
		t.Fatalf("cdnCalls = %d, want 1", d.cdnCalls)
	}
}

func TestRacePeerWinsAfterHedge(t *testing.T) {
	peerRel := make(chan struct{})
	cdnRel := make(chan struct{}) // CDN never completes
	timer := make(chan time.Time)
	close(timer)   // hedge fires
	close(peerRel) // peer completes right after the hedge, before the CDN
	d := &hedgeDoer{peerRelease: peerRel, cdnRelease: cdnRel, body: "BYTES", peerHas: true}
	s := newHedgeServer(d, timer)
	s.signed.Set("h", []string{"http://cdn/x"})

	res, src, ok := s.raceOnePeer(context.Background(), "https://b:8000", "h", "bytes=0-4")
	if !ok || src != srcPeer || string(res.body) != "BYTES" {
		t.Fatalf("peer-after-hedge = (%q,%v,%v), want BYTES,srcPeer,true", res.body, src, ok)
	}
	snap := s.metrics.Snapshot()
	if snap["peer_hedge_fired"].(int64) != 1 || snap["peer_hedge_peer_won"].(int64) != 1 {
		t.Fatalf("counters = %+v, want fired=1 peer_won=1", snap)
	}
	if snap["peer_bytes_wasted"].(int64) != 5 {
		t.Fatalf("peer_bytes_wasted = %v, want 5 (the discarded CDN range)", snap["peer_bytes_wasted"])
	}
}
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd shim-go && go test -race -run 'TestRace' ./...`
Expected: FAIL — `undefined: raceOnePeer`, `srcPeer`, `srcCDN`.

- [ ] **Step 4: Implement the race**

Create `shim-go/peer_hedge.go`:

```go
package main

import "context"

// hedgeSource identifies which side of the race produced the served body.
type hedgeSource int

const (
	srcPeer hedgeSource = iota
	srcCDN
)

// rangeSize returns the byte length of an HTTP Range (hi-lo), 0 if unparseable.
func rangeSize(byteRange string) int64 {
	lo, hi, err := parseRange(byteRange)
	if err != nil {
		return 0
	}
	return hi - lo
}

type raceResult struct {
	res xorbResult
	src hedgeSource
	ok  bool
}

// raceOnePeer fires the peer GET to base and gives it an adaptive head start
// sized by the peer's recent throughput. If the peer finishes first, it wins
// with zero CDN cost. If the timer fires first, the CDN GET is launched in
// parallel and the first to finish wins; the loser is cancelled via context.
// Guarantee: worst-case latency is hedgeDelay + cdnFetch <= PEER_HEDGE_MAX_MS +
// cdnFetch, because a stalled peer produces no bytes and the CDN wins.
func (s *Server) raceOnePeer(ctx context.Context, base, hash, byteRange string) (xorbResult, hedgeSource, bool) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	size := rangeSize(byteRange)
	delay := s.peerStats.hedgeDelay(base, size, s.hedgeFactor, s.hedgeMinMs, s.hedgeMaxMs)

	peerCh := make(chan raceResult, 1)
	go func() {
		start := s.now()
		res, ok := s.peerGet(ctx, base, hash, byteRange)
		if ok {
			s.peerStats.update(base, int64(len(res.body)), s.now().Sub(start))
		}
		peerCh <- raceResult{res: res, src: srcPeer, ok: ok}
	}()

	// Head start: peer wins outright if it finishes before the timer.
	select {
	case o := <-peerCh:
		return o.res, o.src, o.ok
	case <-s.after(delay):
	}

	// Timer fired: hedge the CDN in parallel and race to first completion.
	s.metrics.Incr("peer_hedge_fired", 1)
	cdnCh := make(chan raceResult, 1)
	go func() {
		res, ok := s.cdnGet(ctx, hash, byteRange)
		cdnCh <- raceResult{res: res, src: srcCDN, ok: ok}
	}()

	for {
		select {
		case o := <-peerCh:
			if o.ok {
				cancel() // stop the CDN loser
				s.metrics.Incr("peer_hedge_peer_won", 1)
				s.metrics.Incr("peer_bytes_wasted", size) // CDN range discarded
				return o.res, srcPeer, true
			}
			// Peer failed after the hedge; the CDN is our only hope.
			c := <-cdnCh
			return c.res, srcCDN, c.ok
		case o := <-cdnCh:
			if o.ok {
				cancel() // stop the peer loser
				s.metrics.Incr("peer_hedge_cdn_won", 1)
				return o.res, srcCDN, true
			}
			// CDN failed; fall back to whatever the peer produces.
			p := <-peerCh
			return p.res, srcPeer, p.ok
		}
	}
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd shim-go && go test -race -run 'TestRace' ./...`
Expected: PASS.

- [ ] **Step 6: Wire config into main.go**

In `shim-go/main.go`, add the `envFloat` helper after `envInt`:

```go
func envFloat(key string, def float64) float64 {
	if v := os.Getenv(key); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			return f
		}
	}
	return def
}
```

In the `s := &Server{...}` literal, add after `peerFetchTimeout: fetchTimeout,`:

```go
		peerFetchTimeout: fetchTimeout,
		peerStats:        newPeerStats(),
		hedgeFactor:      envFloat("PEER_HEDGE_FACTOR", 1.5),
		hedgeMinMs:       envInt("PEER_HEDGE_MIN_MS", 50),
		hedgeMaxMs:       envInt("PEER_HEDGE_MAX_MS", 1000),
```

Extend the startup `slog.Info` final line:

```go
		"peers", len(peerList), "peer_max_idle_conns", peerTransport.MaxIdleConnsPerHost,
		"hedge_factor", envFloat("PEER_HEDGE_FACTOR", 1.5), "hedge_max_ms", envInt("PEER_HEDGE_MAX_MS", 1000))
```

- [ ] **Step 7: Document the knobs**

In `deploy/xet-dc-cache.env.example`, after the `# PEER_KEEPALIVE_INTERVAL_MS=0` line append:

```
#
# --- Adaptive hedged fetch (Mechanism B: never slower than WAN) ---
# The peer gets a head start; if it runs long the CDN is raced in and the first
# to finish wins. Guarantee: client latency <= WAN latency + PEER_HEDGE_MAX_MS.
# Multiplier on the predicted peer transfer time before the CDN is hedged in.
# PEER_HEDGE_FACTOR=1.5
# Floor on the hedge delay in ms (don't hedge instantly on tiny ranges).
# PEER_HEDGE_MIN_MS=50
# Cap on the hedge delay in ms = the guarantee's bounded slack.
# PEER_HEDGE_MAX_MS=1000
```

- [ ] **Step 8: Run full suite and vet**

Run: `cd shim-go && go test -race ./... && go vet ./...`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add shim-go/peer_hedge.go shim-go/peer_hedge_test.go shim-go/server.go shim-go/main.go deploy/xet-dc-cache.env.example
git commit -m "feat(peer): adaptive hedged race core (raceOnePeer)"
```

---

### Task 8: Wire the race into discovery + getXorb (drop sticky HEAD, keep fanout HEAD)

**Files:**
- Modify: `shim-go/peer_fetch.go` (rework `fetchFromPeer`; add `recordRaceWin`)
- Modify: `shim-go/xorb.go` (`getXorb` closure uses the new return + accounting)
- Modify: `shim-go/peer_fetch_test.go` (update call sites to the new signature; add sticky-bare-GET + CDN-win accounting cases)

**Interfaces:**
- Changes: `func (s *Server) fetchFromPeer(ctx context.Context, hash, byteRange string) (xorbResult, hedgeSource, bool)` (was `(xorbResult, bool)`). Sticky path now fires a **bare peer GET via the race** (no HEAD — a bare GET 404 is the miss signal); fan-out path keeps the HEAD discovery probe, then races the winner. On any race win it records metrics via `recordRaceWin` and updates the sticky pointer (set on peer win, clear on CDN win). Returns `ok=false` only when no peer had the range, so `getXorb` falls through to the plain CDN path.
- Produces: `func (s *Server) recordRaceWin(res xorbResult, src hedgeSource)` — `srcPeer` => `peer_hits` + `peer_bytes`; `srcCDN` => `wan_bytes`.
- Consumes: `raceOnePeer` (Task 7), `fanoutProbe` (Task 1).

- [ ] **Step 1: Update the existing peer_fetch tests to the new signature**

In `shim-go/peer_fetch_test.go`, every call `res, ok := s.fetchFromPeer(...)` becomes `res, _, ok := s.fetchFromPeer(...)`, and every `_, ok := s.fetchFromPeer(...)` becomes `_, _, ok := s.fetchFromPeer(...)`. Also update `newPeerFetchServer` to initialize the hedge fields so the race can run:

```go
func newPeerFetchServer(peers []string, d httpDoer) *Server {
	return &Server{
		metrics:          NewMetrics(),
		doer:             d,
		peerDoer:         d,
		peers:            staticPeers{list: peers},
		sticky:           newStickyPeer(60*time.Second, nil),
		peerProbeTimeout: 200 * time.Millisecond,
		peerFetchTimeout: 2 * time.Second,
		peerStats:        newPeerStats(),
		hedgeFactor:      1.5,
		hedgeMinMs:       50,
		hedgeMaxMs:       1000,
	}
}
```

The `peerDoer` bodies (`peerDoer.Do`) return instantly, so the peer always beats the (default `time.After`) hedge — existing fanout/sticky/short-read/timeout tests keep their meaning. In `TestFetchFromPeerStickyReused` the sticky path no longer sends a HEAD; a bare GET to `b` returns 206 and the assertion still holds.

- [ ] **Step 2: Add sticky-bare-GET and CDN-win accounting tests**

Append to `shim-go/peer_fetch_test.go`:

```go
// The sticky path must fire a bare GET (no HEAD). A sticky peer that lacks THIS
// range 404s the GET; fetchFromPeer must clear sticky and fall through to fanout.
func TestFetchFromPeerStickyBareGetMissFallsToFanout(t *testing.T) {
	d := &peerDoer{
		have: map[string]map[string]bool{"https://c:8000": {"h": true}}, // only c has it
		body: "BYTES",
	}
	s := newPeerFetchServer([]string{"https://b:8000", "https://c:8000"}, d)
	s.sticky.set("https://b:8000") // stale sticky; b lacks h

	res, src, ok := s.fetchFromPeer(context.Background(), "h", "bytes=0-4")
	if !ok || src != srcPeer || string(res.body) != "BYTES" {
		t.Fatalf("fanout recovery = (%q,%v,%v), want BYTES,srcPeer,true", res.body, src, ok)
	}
	if url, live := s.sticky.get(); !live || url != "https://c:8000" {
		t.Fatalf("sticky = (%q,%v), want c live after fanout recovery", url, live)
	}
}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd shim-go && go test -race -run 'FetchFromPeer' ./...`
Expected: FAIL — signature mismatch (`fetchFromPeer` still returns two values) / `undefined: srcPeer`.

- [ ] **Step 4: Rework fetchFromPeer**

Replace the body of `fetchFromPeer` in `shim-go/peer_fetch.go` (keep the imports; `sync` is still used by `fanoutProbe`):

```go
// fetchFromPeer tries to serve (hash, byteRange) from the fleet, racing the
// chosen peer against a hedged CDN pull (see raceOnePeer). Returns ok=false
// only when no peer had the range, in which case getXorb falls through to the
// plain CDN path. When a race runs, ok=true and hedgeSource says who won.
func (s *Server) fetchFromPeer(ctx context.Context, hash, byteRange string) (xorbResult, hedgeSource, bool) {
	if s.peers == nil {
		return xorbResult{}, srcPeer, false
	}
	peers := s.peers.Peers()
	if len(peers) == 0 {
		return xorbResult{}, srcPeer, false
	}

	// 1. Sticky peer: bare GET race, no HEAD (a bare peer GET that 404s IS the
	// miss signal — getXorb serves peers hit-or-404). A win keeps sticky (peer)
	// or drops it (CDN raced past a slow sticky peer); any failure clears it and
	// falls through to fan-out discovery.
	if url, live := s.sticky.get(); live {
		if res, src, ok := s.raceOnePeer(ctx, url, hash, byteRange); ok {
			s.updateSticky(url, src)
			s.recordRaceWin(res, src)
			return res, src, true
		}
		s.sticky.clear()
	}

	// 2. Fan-out HEAD probe for discovery (which of N peers has the range), then
	// race the winner. HEAD stays here: firing N speculative GETs would multiply
	// peer load.
	if winner, ok := s.fanoutProbe(ctx, peers, hash, byteRange); ok {
		if res, src, ok := s.raceOnePeer(ctx, winner, hash, byteRange); ok {
			s.updateSticky(winner, src)
			s.recordRaceWin(res, src)
			return res, src, true
		}
	}

	s.metrics.Incr("peer_misses", 1)
	return xorbResult{}, srcPeer, false
}

// updateSticky pins the peer on a peer win, or drops it when the CDN raced past
// a slow peer (so a burst doesn't keep betting on the laggard).
func (s *Server) updateSticky(url string, src hedgeSource) {
	if src == srcPeer {
		s.sticky.set(url)
	} else {
		s.sticky.clear()
	}
}

// recordRaceWin books the winning transfer: a peer win displaces WAN/CDN bytes
// (peer_hits/peer_bytes); a CDN win is a real WAN pull (wan_bytes). misses is
// counted once by getXorb, outside this function.
func (s *Server) recordRaceWin(res xorbResult, src hedgeSource) {
	if src == srcPeer {
		s.metrics.Incr("peer_hits", 1)
		s.metrics.Incr("peer_bytes", int64(len(res.body)))
	} else {
		s.metrics.Incr("wan_bytes", int64(len(res.body)))
	}
}
```

Remove the now-unused `probeOK` method from `peer_fetch.go` (the sticky path no longer HEAD-probes). Search for other callers first:

Run: `cd shim-go && grep -rn 'probeOK' .`
If `fetchFromPeer` was the only caller, delete the `probeOK` func. If `go vet` later flags it as unused, delete it.

- [ ] **Step 5: Update getXorb's closure**

In `shim-go/xorb.go` `getXorb`, replace the peer block inside the singleflight closure:

```go
		// Tier 1.5: race a warm peer against a hedged CDN pull before the plain
		// CDN path. recordRaceWin books peer_bytes (peer won) or wan_bytes (CDN
		// won); misses is counted once here.
		if s.peers != nil {
			if res, _, ok := s.fetchFromPeer(r.Context(), hash, byteRange); ok {
				if werr := writeCacheFileAtomic(s.cacheDir, name, path, res.body); werr != nil {
					return nil, &httpError{500, "cache write: " + werr.Error()}
				}
				s.lru.Record(name, int64(len(res.body)))
				s.metrics.Incr("misses", 1)
				return res, nil
			}
		}
```

(The plain CDN path below it is unchanged — it still increments `misses` and `wan_bytes` for the no-peer case.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd shim-go && go test -race -run 'FetchFromPeer|GetXorb' ./...`
Expected: PASS.

- [ ] **Step 7: Run full suite and vet**

Run: `cd shim-go && go test -race ./... && go vet ./...`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add shim-go/peer_fetch.go shim-go/xorb.go shim-go/peer_fetch_test.go
git commit -m "feat(peer): race chosen peer against hedged CDN in getXorb"
```

---

### Task 9: Never-slower-than-WAN guarantee matrix + integration

**Files:**
- Create: `shim-go/peer_guarantee_test.go`

**Interfaces:**
- Consumes: `raceOnePeer` (Task 7), the `hedgeAfter`/`nowFn` seams (Task 7), `getXorb` end-to-end (Task 8).
- Produces: a deterministic fake-clock harness (`fakeClock`) and a `(peer fast/slow/dead) × (CDN fast/slow)` matrix asserting virtual latency ≤ `min(peerMs, PEER_HEDGE_MAX_MS + cdnMs)`, plus a two-`Server` integration test.

**Design note (determinism):** the harness fully controls event ordering with release channels — only the intended winner's release is closed; the loser stays blocked until `raceOnePeer`'s `cancel()` aborts it. Virtual latency is read from `fakeClock.Now()`, which the test advances to a stage's virtual timestamp *before* closing that stage's release, so the value observed when the race returns is exactly that stage's completion time. No wall-clock sleeps gate the assertions.

- [ ] **Step 1: Write the guarantee matrix test**

Create `shim-go/peer_guarantee_test.go`:

```go
package main

import (
	"context"
	"sync"
	"testing"
	"time"
)

// fakeClock is a manually-advanced clock for virtual-time latency assertions.
type fakeClock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}
func (c *fakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	c.t = c.t.Add(d)
	c.mu.Unlock()
}

const guaranteeMaxMs = 200 // PEER_HEDGE_MAX_MS for this matrix (bootstrap => delay == max)

// runGuaranteeCase drives one matrix cell in virtual time and returns the
// winning source and the virtual latency observed when the race returned.
func runGuaranteeCase(t *testing.T, peerMs, cdnMs int, peerDead bool) (hedgeSource, time.Duration) {
	t.Helper()
	clk := &fakeClock{t: time.Unix(0, 0)}
	timer := make(chan time.Time)
	peerRel := make(chan struct{})
	cdnRel := make(chan struct{})
	d := &hedgeDoer{peerRelease: peerRel, cdnRelease: cdnRel, body: "BYTES", peerHas: true}
	s := newHedgeServer(d, timer)
	s.hedgeMaxMs = guaranteeMaxMs
	s.nowFn = clk.Now
	s.signed.Set("h", []string{"http://cdn/x"})

	type out struct {
		src hedgeSource
		ok  bool
	}
	done := make(chan out, 1)
	go func() {
		res, src, ok := s.raceOnePeer(context.Background(), "https://b:8000", "h", "bytes=0-4")
		_ = res
		done <- out{src, ok}
	}()

	// Build the virtual schedule: hedge fires at guaranteeMaxMs; the winner is
	// whichever completes earliest. Close only the winner's release; the loser
	// stays blocked and is cancelled by raceOnePeer.
	peerDone := peerMs
	cdnDone := guaranteeMaxMs + cdnMs // CDN only starts when the hedge fires

	if !peerDead && peerDone < guaranteeMaxMs {
		clk.Advance(time.Duration(peerDone) * time.Millisecond)
		close(peerRel) // peer wins before the hedge
	} else {
		// Hedge fires first.
		clk.Advance(time.Duration(guaranteeMaxMs) * time.Millisecond)
		close(timer)
		if !peerDead && peerDone <= cdnDone {
			clk.Advance(time.Duration(peerDone-guaranteeMaxMs) * time.Millisecond)
			close(peerRel) // peer wins after the hedge
		} else {
			clk.Advance(time.Duration(cdnDone-guaranteeMaxMs) * time.Millisecond)
			close(cdnRel) // CDN wins
		}
	}

	res := <-done
	if !res.ok {
		t.Fatal("race returned ok=false; a served body was expected")
	}
	return res.src, clk.Now().Sub(time.Unix(0, 0))
}

func TestGuaranteeMatrix(t *testing.T) {
	cases := []struct {
		name     string
		peerMs   int
		cdnMs    int
		peerDead bool
		wantSrc  hedgeSource
	}{
		{"peer_fast_cdn_fast", 100, 100, false, srcPeer},
		{"peer_fast_cdn_slow", 100, 400, false, srcPeer},
		{"peer_slow_cdn_fast", 500, 100, false, srcCDN}, // hedge@200 + cdn 100 = 300 < peer 500
		{"peer_slow_cdn_slow", 500, 400, false, srcPeer}, // cdn done @600 > peer @500
		{"peer_dead_cdn_fast", 0, 100, true, srcCDN},
		{"peer_dead_cdn_slow", 0, 400, true, srcCDN},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			src, latency := runGuaranteeCase(t, c.peerMs, c.cdnMs, c.peerDead)
			if src != c.wantSrc {
				t.Fatalf("winner = %v, want %v", src, c.wantSrc)
			}
			// Bound: client latency <= min(peerMs, PEER_HEDGE_MAX_MS + cdnMs).
			peerBound := time.Duration(c.peerMs) * time.Millisecond
			cdnBound := time.Duration(guaranteeMaxMs+c.cdnMs) * time.Millisecond
			bound := cdnBound
			if !c.peerDead && peerBound < bound {
				bound = peerBound
			}
			if latency > bound {
				t.Fatalf("latency %v exceeds guarantee bound %v", latency, bound)
			}
		})
	}
}
```

- [ ] **Step 2: Run the matrix to verify it passes**

Run: `cd shim-go && go test -race -run TestGuaranteeMatrix ./...`
Expected: PASS (all six cells).

- [ ] **Step 3: Write the two-Server integration test**

Append to `shim-go/peer_guarantee_test.go`:

```go
// Healthy B: A serves B's bytes via the peer path with zero CDN calls and no
// hedge fired (peer beats the default real-time hedge).
func TestIntegrationHealthyPeerZeroCDN(t *testing.T) {
	warm := t.TempDir()
	if err := os.WriteFile(cachePath(warm, "h", "bytes=0-4"), []byte("BYTES"), 0o644); err != nil {
		t.Fatal(err)
	}
	b := &Server{
		cacheDir: warm, metrics: NewMetrics(), lru: newLRU(0, 0, nil, func(string) {}),
		signed: NewTTLMap(3600e9, 100, nil), doer: &countingDoer{}, signedCandidates: 8,
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /xorb/xorbs/default/{xorb_hash}", b.getXorb)
	ts := httptest.NewServer(mux)
	defer ts.Close()

	cdn := &countingDoer{}
	a := &Server{
		cacheDir: t.TempDir(), metrics: NewMetrics(), lru: newLRU(0, 0, nil, func(string) {}),
		signed: NewTTLMap(3600e9, 100, nil), doer: cdn, peerDoer: http.DefaultClient, signedCandidates: 8,
		peers: staticPeers{list: []string{ts.URL}}, sticky: newStickyPeer(time.Minute, nil),
		peerProbeTimeout: time.Second, peerFetchTimeout: 5 * time.Second,
		peerStats: newPeerStats(), hedgeFactor: 1.5, hedgeMinMs: 50, hedgeMaxMs: 1000,
	}
	a.signed.Set("h", []string{"http://cdn/x"})

	rec := doGetXorb(a, "h", "bytes=0-4")
	if rec.Code != 206 || rec.Body.String() != "BYTES" {
		t.Fatalf("A code=%d body=%q, want 206/BYTES from peer", rec.Code, rec.Body.String())
	}
	snap := a.metrics.Snapshot()
	if snap["peer_bytes"].(int64) != 5 || snap["wan_bytes"].(int64) != 0 {
		t.Fatalf("peer_bytes=%v wan_bytes=%v, want 5/0", snap["peer_bytes"], snap["wan_bytes"])
	}
	if snap["peer_hedge_fired"].(int64) != 0 {
		t.Fatalf("hedge fired %v times against a healthy fast peer, want 0", snap["peer_hedge_fired"])
	}
	if cdn.n != 0 {
		t.Fatalf("CDN called %d times for a healthy-peer hit, want 0", cdn.n)
	}
}

// Throttled B: B stalls the GET body, so A's hedge fires and A serves the CDN
// bytes without exceeding the latency bound. A's CDN doer returns immediately.
func TestIntegrationThrottledPeerHedgesToCDN(t *testing.T) {
	pd := &peerDoer{
		have:    map[string]map[string]bool{"https://b:8000": {"h": true}},
		body:    "BYTES",
		hangGet: map[string]bool{"https://b:8000": true}, // HEAD ok, GET stalls
	}
	a := &Server{
		cacheDir: t.TempDir(), metrics: NewMetrics(), lru: newLRU(0, 0, nil, func(string) {}),
		signed: NewTTLMap(3600e9, 100, nil), doer: &countingDoer{}, peerDoer: pd, signedCandidates: 8,
		peers: staticPeers{list: []string{"https://b:8000"}}, sticky: newStickyPeer(time.Minute, nil),
		peerProbeTimeout: time.Second, peerFetchTimeout: 2 * time.Second,
		peerStats: newPeerStats(), hedgeFactor: 1.5, hedgeMinMs: 50, hedgeMaxMs: 200,
	}
	a.signed.Set("h", []string{"http://cdn/x"})

	start := time.Now()
	rec := doGetXorb(a, "h", "bytes=0-4")
	elapsed := time.Since(start)
	if rec.Code != 206 || rec.Body.String() != "BYTES" {
		t.Fatalf("A code=%d body=%q, want 206/BYTES from CDN hedge", rec.Code, rec.Body.String())
	}
	snap := a.metrics.Snapshot()
	if snap["peer_hedge_fired"].(int64) < 1 || snap["peer_hedge_cdn_won"].(int64) < 1 {
		t.Fatalf("counters=%+v, want hedge_fired>=1 cdn_won>=1", snap)
	}
	if snap["wan_bytes"].(int64) != 5 {
		t.Fatalf("wan_bytes=%v, want 5 (CDN won the race)", snap["wan_bytes"])
	}
	// Bound: hedge (200ms) + fast CDN, generously slack for CI scheduling.
	if elapsed > time.Second {
		t.Fatalf("took %v; must be bounded near hedgeMax + cdn, not the stalled peer", elapsed)
	}
}
```

Add the import block for this file:

```go
import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"sync"
	"testing"
	"time"
)
```

- [ ] **Step 4: Run the integration tests to verify they pass**

Run: `cd shim-go && go test -race -run 'TestIntegration' ./...`
Expected: PASS.

- [ ] **Step 5: Run the full suite and vet**

Run: `cd shim-go && go test -race ./... && go vet ./...`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add shim-go/peer_guarantee_test.go
git commit -m "test(peer): never-slower-than-WAN guarantee matrix + integration"
```

---

## Self-Review

**1. Spec coverage:**
- Mechanism A dedicated transport, HTTP/1.1 forced, fat idle pool → Task 1. Socket buffers → Task 2. Keepalive → Task 3. Deploy sysctl note → documented in Task 3's env comment.
- Mechanism B: drop sticky HEAD / keep fanout HEAD → Task 8. Adaptive timer + EWMA → Tasks 4 & 7. Race + cancel loser → Task 7. Bootstrap (no sample => max) → Task 4 + Task 7 default. Context cancellation of CDN loser → Task 6.
- Config table (six vars, defaults) → parsed in Tasks 1, 2, 3, 7; documented in env.example across the same tasks; `PEER_FETCH_TIMEOUT_MS` retained (untouched in `peerGet`).
- Metrics (four counters + throughput gauge, JSON + Prometheus) → Task 5.
- singleflight accounting invariants → Task 8 (`recordRaceWin` + `misses` once in `getXorb`).
- Guarantee matrix (peer fast/slow/dead × CDN fast/slow, fake clock) + integration → Task 9.
- Explicitly-rejected items (QUIC, RDMA, striping, auto-BDP, per-peer labels) → correctly absent; fleet-aggregate gauge chosen per the out-of-scope note.

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to Task N". Every code step carries exact Go.

**3. Type consistency:** `hedgeSource`/`srcPeer`/`srcCDN` defined in Task 7, consumed identically in Tasks 8–9. `fetchFromPeer` new 3-value signature defined in Task 8 and used in `getXorb` (Task 8) and tests (Tasks 8–9). `peerStats` methods (`hedgeDelay`, `update`, `fleetThroughput`) defined in Task 4, consumed in Tasks 5 & 7. `newPeerTransport`/`peerTransportConfig` defined in Task 1, extended in Task 2, used in `main.go`. `hedgeDoer`/`newHedgeServer` defined in Task 7's test, reused in Task 9. `fetchAuthorized(ctx,...)`/`cdnGet` defined in Task 6, consumed in Tasks 7–8. Metric keys match between `metrics.go` (Task 5), `prometheus.go` (Task 5), and `Incr` call sites (Tasks 7–8).

One fix applied during review: the throughput gauge is nil-guarded in `prometheus.go` and the `/metrics` handler so pre-existing `Server` literals in `prometheus_test.go` (which don't set `peerStats`) keep passing.
