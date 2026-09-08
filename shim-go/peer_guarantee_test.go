package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
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
	s.peer.hedgeMaxMs = guaranteeMaxMs
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
		{"peer_slow_cdn_fast", 500, 100, false, srcCDN},  // hedge@200 + cdn 100 = 300 < peer 500
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
		signed: NewTTLMap(3600e9, 100, nil), doer: cdn, signedCandidates: 8,
		peer: peerEngine{
			doer:  http.DefaultClient,
			peers: staticPeers{list: []string{ts.URL}}, sticky: newStickyPeer(time.Minute, nil),
			probeTimeout: time.Second, fetchTimeout: 5 * time.Second,
			stats: newPeerStats(), hedgeFactor: 1.5, hedgeMinMs: 50, hedgeMaxMs: 1000,
		},
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
		signed: NewTTLMap(3600e9, 100, nil), doer: &countingDoer{}, signedCandidates: 8,
		peer: peerEngine{
			doer:  pd,
			peers: staticPeers{list: []string{"https://b:8000"}}, sticky: newStickyPeer(time.Minute, nil),
			probeTimeout: time.Second, fetchTimeout: 2 * time.Second,
			stats: newPeerStats(), hedgeFactor: 1.5, hedgeMinMs: 50, hedgeMaxMs: 200,
		},
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
