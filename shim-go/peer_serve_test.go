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
