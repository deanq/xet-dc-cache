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
