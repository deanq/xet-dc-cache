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
		peer: peerEngine{
			doer:  d,
			peers: staticPeers{list: []string{"https://a:8000", "https://b:8000"}},
		},
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
	s := &Server{peer: peerEngine{doer: d, peers: staticPeers{list: []string{"https://a:8000"}}}}
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
