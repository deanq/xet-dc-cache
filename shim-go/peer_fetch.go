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

	// 1. Sticky peer: probe just it under the budget; on hit, fetch it. Any
	// miss here (failed probe or failed GET) clears the sticky pointer so a
	// single bad/stalled peer can't tax an entire burst pull.
	if url, live := s.sticky.get(); live {
		if s.probeOK(ctx, url, hash, byteRange) {
			if res, ok := s.peerGet(ctx, url, hash, byteRange); ok {
				s.sticky.set(url)
				s.metrics.Incr("peer_hits", 1)
				s.metrics.Incr("peer_bytes", int64(len(res.body)))
				return res, true
			}
		}
		s.sticky.clear()
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
	resp, err := s.peerHTTP().Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

// peerGet fetches the range from base and verifies the body length matches the
// request. Returns (result, true) only on a clean 206 of the expected size.
func (s *Server) peerGet(ctx context.Context, base, hash, byteRange string) (xorbResult, bool) {
	if s.peerFetchTimeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, s.peerFetchTimeout)
		defer cancel()
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, base+"/xorb/xorbs/default/"+hash, nil)
	if err != nil {
		return xorbResult{}, false
	}
	s.setPeerHeaders(req, byteRange)
	resp, err := s.peerHTTP().Do(req)
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
	lo, hi, perr := parseRange(byteRange)
	if perr != nil || int64(len(body)) != hi-lo {
		return xorbResult{}, false // short/over read, or unparseable range — don't trust it
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
