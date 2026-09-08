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
	hfUpstream       string
	casUpstream      string
	publicBase       string
	cacheDir         string
	signed           *TTLMap
	manifests        *ManifestCache
	metrics          *Metrics
	lru              *lruCache
	doer             httpDoer
	sf               singleflight.Group
	signedCandidates int

	// sem bounds concurrent upstream miss fetches (nil = unlimited). Worst-case
	// miss RSS is bounded to cap * max-range-size, and upstreams are shielded
	// from a thundering herd of distinct cold ranges.
	sem chan struct{}
	// authToken, when non-empty, gates inbound requests (Bearer). Empty = open
	// (the trusted-LAN default). See withAuth in middleware.go.
	authToken string

	// peer groups the entire "Tier 1.5" peering surface (config, per-peer
	// state, hedge tuning, test seam). Its zero value = peering disabled, so a
	// Server built without it behaves exactly as a non-peering shim.
	peer peerEngine

	// nowFn is a test seam (clock injection), nil in production => time.Now. It
	// lives on Server (not peerEngine) because the plain xorb hit/miss path uses
	// it too; peerEngine.hedgeAfter is the peer-only companion seam.
	nowFn func() time.Time
}

// peerEngine isolates the "Tier 1.5" peer cache surface: pull a warm range from
// a sibling cache over the private backbone before falling back to the CDN. The
// methods that drive it stay on *Server (they need metrics, the fetch semaphore,
// and the clock); this struct just namespaces the peer-specific state so the
// Server god-struct no longer mixes it in with the hub/relay/store roles.
type peerEngine struct {
	// doer carries peer traffic on a dedicated, HTTP/1.1-forced transport (see
	// newPeerTransport). Kept distinct from Server.doer so peer tuning never
	// perturbs the CDN redirect/identity contract. nil => fall back to doer.
	doer httpDoer

	// peers is the sibling fleet; nil (the zero value) disables peering.
	peers        PeerSet
	sticky       *stickyPeer
	probeTimeout time.Duration
	// fetchTimeout bounds the peer GET request + body read (separate from
	// probeTimeout, which only budgets the HEAD probe). Zero disables the bound
	// (used by tests that don't set it).
	fetchTimeout time.Duration
	stats        *peerStats

	// Adaptive hedge (Mechanism B): give the peer a head start sized by its
	// recent throughput, then race the CDN. stats holds the per-peer EWMA.
	hedgeFactor float64 // multiplier on predicted peer time before hedging the CDN
	hedgeMinMs  int     // floor on the hedge delay
	hedgeMaxMs  int     // cap on the hedge delay = the guarantee's bounded slack

	// hedgeAfter is a test seam (clock injection), nil in production =>
	// time.After. Paired with Server.nowFn.
	hedgeAfter func(time.Duration) <-chan time.Time
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

// tryAcquire grabs a fetch slot without blocking, returning false if the pool
// is saturated. The speculative hedge uses this so a burst of hedged CDN pulls
// can never consume the slots a genuine cold miss is waiting on: under pressure
// the hedge simply doesn't fire and the peer carries the range (or, if the peer
// fails, the plain miss path acquires a slot the normal blocking way).
func (s *Server) tryAcquire() bool {
	if s.sem == nil {
		return true
	}
	select {
	case s.sem <- struct{}{}:
		return true
	default:
		return false
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
	if s.peer.doer != nil {
		return s.peer.doer
	}
	return s.doer
}

// now returns the current time via the injected clock (tests) or the wall clock.
func (s *Server) now() time.Time {
	if s.nowFn != nil {
		return s.nowFn()
	}
	return time.Now()
}
