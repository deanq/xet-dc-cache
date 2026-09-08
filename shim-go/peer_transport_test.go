package main

import (
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
	if tr.DisableCompression {
		t.Fatal("peer transport should not inherit the CDN identity/compression contract")
	}
}

func TestPeerHTTPFallsBackToCDNDoer(t *testing.T) {
	c := &countingDoer{}
	s := &Server{doer: c}
	if s.peerHTTP() != httpDoer(c) {
		t.Fatal("peerHTTP must fall back to s.doer when s.peer.doer is nil")
	}
	pc := &countingDoer{}
	s.peer.doer = pc
	if s.peerHTTP() != httpDoer(pc) {
		t.Fatal("peerHTTP must return s.peer.doer when set")
	}
}

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
