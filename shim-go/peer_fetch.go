package main

import (
	"context"
	"io"
	"net/http"
	"sync"
)

// fetchFromPeer tries to serve (hash, byteRange) from the fleet, racing the
// chosen peer against a hedged CDN pull (see raceOnePeer). Returns ok=false
// only when no peer had the range, in which case getXorb falls through to the
// plain CDN path. When a race runs, ok=true and hedgeSource says who won.
func (s *Server) fetchFromPeer(ctx context.Context, hash, byteRange string) (xorbResult, hedgeSource, bool) {
	if s.peer.peers == nil {
		return xorbResult{}, srcPeer, false
	}
	peers := s.peer.peers.Peers()
	if len(peers) == 0 {
		return xorbResult{}, srcPeer, false
	}

	// 1. Sticky peer: bare GET race, no HEAD (a bare peer GET that 404s IS the
	// miss signal — getXorb serves peers hit-or-404). A win keeps sticky (peer)
	// or drops it (CDN raced past a slow sticky peer); any failure clears it and
	// falls through to fan-out discovery.
	if url, live := s.peer.sticky.get(); live {
		if res, src, ok := s.raceOnePeer(ctx, url, hash, byteRange); ok {
			s.updateSticky(url, src)
			s.recordRaceWin(res, src)
			return res, src, true
		}
		s.peer.sticky.clear()
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
		s.peer.sticky.set(url)
	} else {
		s.peer.sticky.clear()
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
		// A hedge CDN win. Book wan_bytes (total WAN) AND the hedge-specific
		// subset so operators can tell "hedge raced past a slow peer" apart from
		// the plain "no peer had it" fallthrough (which books wan_bytes only).
		s.metrics.Incr("wan_bytes", int64(len(res.body)))
		s.metrics.Incr("peer_hedge_cdn_bytes", int64(len(res.body)))
	}
}

// fanoutProbe HEAD-probes all peers in parallel under one shared budget and
// returns the first that has the range. Cancels the rest once a winner is found.
func (s *Server) fanoutProbe(ctx context.Context, peers []string, hash, byteRange string) (string, bool) {
	pctx, cancel := context.WithTimeout(ctx, s.peer.probeTimeout)
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
	if s.peer.fetchTimeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, s.peer.fetchTimeout)
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
