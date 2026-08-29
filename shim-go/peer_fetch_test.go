package main

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
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
	// hangGet, when set for a peer base URL, makes a GET (not HEAD) block
	// until the request's context is cancelled, then return its error —
	// simulating a peer that answers HEAD fine but stalls mid-body on GET.
	hangGet map[string]bool
	gets    int32
}

func (d *peerDoer) Do(req *http.Request) (*http.Response, error) {
	base := req.URL.Scheme + "://" + req.URL.Host
	hash := strings.TrimPrefix(req.URL.Path, "/xorb/xorbs/default/")
	if d.hang[base] {
		<-req.Context().Done()
		return nil, req.Context().Err()
	}
	if req.Method == http.MethodGet && d.hangGet[base] {
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
		peerFetchTimeout: 2 * time.Second,
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

// A peer that answers HEAD 200 (so it wins the probe) but then hangs on the
// GET body must not block fetchFromPeer anywhere near the 60s shared client
// timeout: the separate, generous peerFetchTimeout must bound the transfer.
func TestFetchFromPeerGetBodyHangBounded(t *testing.T) {
	d := &peerDoer{
		have:    map[string]map[string]bool{"https://b:8000": {"h": true}},
		body:    "BYTES",
		hangGet: map[string]bool{"https://b:8000": true},
	}
	s := newPeerFetchServer([]string{"https://b:8000"}, d)
	s.peerFetchTimeout = 50 * time.Millisecond

	start := time.Now()
	_, ok := s.fetchFromPeer(context.Background(), "h", "bytes=0-4")
	if ok {
		t.Fatal("hanging GET body should cause a false return (fall to CDN)")
	}
	if elapsed := time.Since(start); elapsed > 500*time.Millisecond {
		t.Fatalf("took %v; peerFetchTimeout must bound the GET near 50ms", elapsed)
	}
}

// A sticky peer that fails on the GET (after passing the probe) must be
// cleared so a subsequent burst of ranges doesn't keep re-probing/re-hanging
// on the same bad peer.
func TestFetchFromPeerStickyClearedOnGetFailure(t *testing.T) {
	d := &peerDoer{
		have:    map[string]map[string]bool{"https://b:8000": {"h": true}},
		body:    "BYTES",
		hangGet: map[string]bool{"https://b:8000": true},
	}
	s := newPeerFetchServer([]string{"https://b:8000"}, d)
	s.peerFetchTimeout = 50 * time.Millisecond
	s.sticky.set("https://b:8000")

	if _, ok := s.fetchFromPeer(context.Background(), "h", "bytes=0-4"); ok {
		t.Fatal("sticky peer with hanging GET should return false")
	}
	if _, live := s.sticky.get(); live {
		t.Fatal("sticky pointer should be cleared after a failed sticky GET")
	}
}
