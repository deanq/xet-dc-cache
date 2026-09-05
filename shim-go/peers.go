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

// ewmaAlpha weights the newest throughput sample in the per-peer EWMA. Small so
// a single outlier fetch can't whipsaw the hedge decision. Not user-facing.
const ewmaAlpha = 0.3

// peerStats holds a per-peer throughput EWMA (bytes/ms), updated on every
// completed peer GET and read to predict the adaptive hedge delay. Throughput
// (size-normalized), not raw latency, so a 64 MB range and a 1 MB range on the
// same link produce comparable samples. Thread-safe; lives beside the sticky
// pointer because both describe "how good is this peer right now".
type peerStats struct {
	mu   sync.Mutex
	ewma map[string]float64 // peer base URL -> throughput bytes/ms
}

func newPeerStats() *peerStats { return &peerStats{ewma: map[string]float64{}} }

// hedgeDelay predicts how long to give the peer before hedging in the CDN.
// With no sample for base it returns maxMs (bootstrap: an unknown peer hedges
// conservatively). Otherwise clamp(size/ewma * factor, minMs, maxMs).
func (p *peerStats) hedgeDelay(base string, size int64, factor float64, minMs, maxMs int) time.Duration {
	p.mu.Lock()
	tp, ok := p.ewma[base]
	p.mu.Unlock()
	if !ok || tp <= 0 {
		return time.Duration(maxMs) * time.Millisecond
	}
	ms := (float64(size) / tp) * factor
	if lo := float64(minMs); ms < lo {
		ms = lo
	}
	if hi := float64(maxMs); ms > hi {
		ms = hi
	}
	return time.Duration(ms) * time.Millisecond
}

// update folds a completed transfer into base's throughput EWMA. Sub-ms
// transfers are floored to 1ms so a fast tiny range can't imply infinite
// throughput.
func (p *peerStats) update(base string, bytes int64, elapsed time.Duration) {
	ms := float64(elapsed.Milliseconds())
	if ms <= 0 {
		ms = 1
	}
	sample := float64(bytes) / ms
	p.mu.Lock()
	defer p.mu.Unlock()
	if cur, ok := p.ewma[base]; ok {
		p.ewma[base] = ewmaAlpha*sample + (1-ewmaAlpha)*cur
	} else {
		p.ewma[base] = sample
	}
}

// fleetThroughput is the mean per-peer EWMA (bytes/ms), 0 when empty. Exposed as
// a fleet-aggregate gauge for tuning (per-peer labels are out of scope for the
// hand-rolled Prometheus exposition).
func (p *peerStats) fleetThroughput() float64 {
	p.mu.Lock()
	defer p.mu.Unlock()
	if len(p.ewma) == 0 {
		return 0
	}
	var sum float64
	for _, v := range p.ewma {
		sum += v
	}
	return sum / float64(len(p.ewma))
}
