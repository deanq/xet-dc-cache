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
		signed:           NewTTLMap(3600e9, 100, nil),
		signedCandidates: 8,
		peer: peerEngine{
			doer:         d,
			peers:        staticPeers{list: []string{"https://b:8000"}},
			sticky:       newStickyPeer(time.Minute, nil),
			stats:        newPeerStats(),
			hedgeFactor:  1.5,
			hedgeMinMs:   50,
			hedgeMaxMs:   1000,
			fetchTimeout: 5 * time.Second,
			hedgeAfter:   func(time.Duration) <-chan time.Time { return timer },
		},
	}
}

func TestRaceFastPeerNoCDN(t *testing.T) {
	peerRel := make(chan struct{})
	close(peerRel)                // peer completes immediately
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
	// The CDN body never delivered a byte before it was cancelled, so the TRUE
	// wasted transfer is 0 — not the 5-byte requested range. Booking the range
	// size here (the old behavior) systematically overcounted (finding #3).
	if snap["peer_bytes_wasted"].(int64) != 0 {
		t.Fatalf("peer_bytes_wasted = %v, want 0 (CDN transferred nothing before cancel)", snap["peer_bytes_wasted"])
	}
}

// partialThenBlockBody yields data once, then blocks until the request context
// is cancelled — modeling a CDN GET that streamed some bytes before losing the
// race and being cancelled. io.ReadAll therefore returns those bytes AND an
// error, so cdnGet reports the partial count as the true wasted transfer.
type partialThenBlockBody struct {
	ctx  context.Context
	data []byte
	sent bool
}

func (b *partialThenBlockBody) Read(p []byte) (int, error) {
	if !b.sent {
		b.sent = true
		return copy(p, b.data), nil
	}
	<-b.ctx.Done()
	return 0, b.ctx.Err()
}
func (b *partialThenBlockBody) Close() error { return nil }

// partialCDNDoer answers the peer GET immediately (peer wins) and the CDN GET
// with a body that streams `body` once then blocks until cancelled.
type partialCDNDoer struct {
	peerRelease <-chan struct{}
	body        string
}

func (d *partialCDNDoer) Do(req *http.Request) (*http.Response, error) {
	h := http.Header{}
	h.Set("Content-Range", "bytes 0-4/999")
	if req.Header.Get("X-Xet-Peer") == "1" {
		return &http.Response{StatusCode: 206, Header: h,
			Body: &hedgeBody{ctx: req.Context(), release: d.peerRelease, data: []byte(d.body)}}, nil
	}
	return &http.Response{StatusCode: 206, Header: h,
		Body: &partialThenBlockBody{ctx: req.Context(), data: []byte(d.body)}}, nil
}

// A peer win after the hedge must book the CDN bytes ACTUALLY transferred
// before cancellation, not the requested range size (finding #3).
func TestRacePeerWinsBooksActualCDNWaste(t *testing.T) {
	peerRel := make(chan struct{})
	close(peerRel) // peer completes right after the hedge fires
	timer := make(chan time.Time)
	close(timer) // hedge fires immediately
	d := &partialCDNDoer{peerRelease: peerRel, body: "BYTES"}
	s := newHedgeServer(&hedgeDoer{}, timer) // placeholder, doer replaced below
	s.doer = d
	s.peer.doer = d
	s.signed.Set("h", []string{"http://cdn/x"})

	res, src, ok := s.raceOnePeer(context.Background(), "https://b:8000", "h", "bytes=0-4")
	if !ok || src != srcPeer || string(res.body) != "BYTES" {
		t.Fatalf("peer-after-hedge = (%q,%v,%v), want BYTES,srcPeer,true", res.body, src, ok)
	}
	// The waste is booked asynchronously (so the peer-win return isn't blocked
	// on CDN teardown), so poll for it rather than reading immediately.
	var got int64
	for i := 0; i < 400; i++ {
		if got = s.metrics.Snapshot()["peer_bytes_wasted"].(int64); got == 5 {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	if got != 5 {
		t.Fatalf("peer_bytes_wasted = %d, want 5 (the bytes the CDN streamed before cancel)", got)
	}
}
