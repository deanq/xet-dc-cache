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
	if interval <= 0 || s.peer.peers == nil {
		return
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-stop:
			return
		case <-ticker.C:
			for _, base := range s.peer.peers.Peers() {
				s.pingPeer(base)
			}
		}
	}
}

// pingPeer issues one best-effort HEAD /healthz carrying the peer headers, under
// the probe timeout so a dead peer never blocks the loop.
func (s *Server) pingPeer(base string) {
	ctx := context.Background()
	if s.peer.probeTimeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, s.peer.probeTimeout)
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
