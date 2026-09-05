package main

import (
	"context"
	"net/http"
	"time"

	"golang.org/x/sync/singleflight"
)

type httpDoer interface {
	Do(*http.Request) (*http.Response, error)
}

// Server plays all three roles (hub proxy / CAS relay / xorb store).
type Server struct {
	hfUpstream  string
	casUpstream string
	publicBase  string
	cacheDir    string
	signed      *TTLMap
	manifests   *ManifestCache
	metrics     *Metrics
	lru         *lruCache
	doer        httpDoer
	// peerDoer carries peer traffic on a dedicated, HTTP/1.1-forced transport
	// (see newPeerTransport). Kept distinct from doer so peer tuning never
	// perturbs the CDN redirect/identity contract. nil => fall back to doer.
	peerDoer         httpDoer
	sf               singleflight.Group
	signedCandidates int

	// sem bounds concurrent upstream miss fetches (nil = unlimited). Worst-case
	// miss RSS is bounded to cap * max-range-size, and upstreams are shielded
	// from a thundering herd of distinct cold ranges.
	sem chan struct{}
	// authToken, when non-empty, gates inbound requests (Bearer). Empty = open
	// (the trusted-LAN default). See withAuth in middleware.go.
	authToken string

	// Peering ("Tier 1.5"): pull a warm range from a sibling cache over the
	// private backbone before falling back to the CDN. nil peers = disabled.
	peers            PeerSet
	sticky           *stickyPeer
	peerProbeTimeout time.Duration
	// peerFetchTimeout bounds the peer GET request + body read (separate from
	// peerProbeTimeout, which only budgets the HEAD probe). Zero disables the
	// bound (used by tests that don't set it).
	peerFetchTimeout time.Duration
	peerStats        *peerStats
}

// acquire takes a fetch slot, honoring the caller's context so a client that
// disconnects (or times out) while waiting frees itself instead of piling up.
// No-op when the cap is disabled.
func (s *Server) acquire(ctx context.Context) error {
	if s.sem == nil {
		return nil
	}
	select {
	case s.sem <- struct{}{}:
		return nil
	case <-ctx.Done():
		return &httpError{http.StatusServiceUnavailable, "server busy (fetch cap reached): " + ctx.Err().Error()}
	}
}

func (s *Server) release() {
	if s.sem != nil {
		<-s.sem
	}
}

// peerHTTP returns the doer used for peer traffic: the dedicated tuned peer
// transport when configured, else the CDN doer (keeps tests that only set doer
// working, and keeps peering functional if the peer transport is unset).
func (s *Server) peerHTTP() httpDoer {
	if s.peerDoer != nil {
		return s.peerDoer
	}
	return s.doer
}
