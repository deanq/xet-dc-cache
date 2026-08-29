package main

import (
	"strings"
	"sync"
	"time"
)

// PeerSet is the set of sibling caches this host may pull warm ranges from.
// Static today (parsed from PEERS at startup); an interface so a future source
// (e.g. cloud service discovery) can replace it without touching the fetch path.
type PeerSet interface {
	Peers() []string // base URLs, self already excluded
}

type staticPeers struct{ list []string }

func (s staticPeers) Peers() []string { return s.list }

// parsePeers splits a comma-separated peer list, trimming whitespace and
// trailing slashes, dropping empties and any entry equal to self. Returns nil
// when no usable peers remain (peering disabled).
func parsePeers(raw, self string) []string {
	self = trimSlash(strings.TrimSpace(self))
	var out []string
	for _, p := range strings.Split(raw, ",") {
		p = trimSlash(strings.TrimSpace(p))
		if p == "" || p == self {
			continue
		}
		out = append(out, p)
	}
	return out
}

// stickyPeer remembers the peer that most recently served us a range, so a
// burst model-pull (thousands of ranges over seconds) probes ~once instead of
// per-range. A short TTL lets it lapse between pulls. Thread-safe.
type stickyPeer struct {
	mu     sync.Mutex
	url    string
	expiry time.Time
	ttl    time.Duration
	now    func() time.Time
}

func newStickyPeer(ttl time.Duration, clock func() time.Time) *stickyPeer {
	if clock == nil {
		clock = time.Now
	}
	return &stickyPeer{ttl: ttl, now: clock}
}

func (s *stickyPeer) get() (string, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.url == "" || !s.now().Before(s.expiry) {
		return "", false
	}
	return s.url, true
}

func (s *stickyPeer) set(url string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.url = url
	s.expiry = s.now().Add(s.ttl)
}

// clear drops the sticky pointer, e.g. after the sticky peer fails to serve —
// so one bad/stalled peer can't keep taxing an entire burst pull.
func (s *stickyPeer) clear() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.url = ""
}
