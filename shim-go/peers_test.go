package main

import (
	"testing"
	"time"
)

func TestParsePeers(t *testing.T) {
	cases := []struct {
		name, raw, self string
		want            []string
	}{
		{"empty", "", "", nil},
		{"single", "https://a:8000", "", []string{"https://a:8000"}},
		{"trims spaces and slashes", " https://a:8000/ , https://b:8000 ", "",
			[]string{"https://a:8000", "https://b:8000"}},
		{"drops empties", "https://a:8000,,https://b:8000,", "",
			[]string{"https://a:8000", "https://b:8000"}},
		{"excludes self", "https://a:8000,https://b:8000", "https://b:8000/",
			[]string{"https://a:8000"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := parsePeers(c.raw, c.self)
			if !equalSlice(got, c.want) {
				t.Fatalf("parsePeers(%q,%q) = %v, want %v", c.raw, c.self, got, c.want)
			}
		})
	}
}

func TestStaticPeersImplementsPeerSet(t *testing.T) {
	var ps PeerSet = staticPeers{list: []string{"https://a:8000"}}
	if got := ps.Peers(); !equalSlice(got, []string{"https://a:8000"}) {
		t.Fatalf("Peers() = %v", got)
	}
}

func TestStickyPeerSetGetExpiry(t *testing.T) {
	now := time.Unix(1000, 0)
	sp := newStickyPeer(60*time.Second, func() time.Time { return now })

	if _, ok := sp.get(); ok {
		t.Fatal("empty sticky should report not-live")
	}
	sp.set("https://a:8000")
	if url, ok := sp.get(); !ok || url != "https://a:8000" {
		t.Fatalf("get after set = (%q,%v)", url, ok)
	}
	now = now.Add(59 * time.Second)
	if _, ok := sp.get(); !ok {
		t.Fatal("should still be live at 59s (ttl 60s)")
	}
	now = now.Add(2 * time.Second) // 61s total
	if _, ok := sp.get(); ok {
		t.Fatal("should be expired past ttl")
	}
}

func TestPeerStatsBootstrapHedgesAtMax(t *testing.T) {
	p := newPeerStats()
	// No sample yet: any range hedges at ~PEER_HEDGE_MAX_MS.
	got := p.hedgeDelay("https://a:8000", 64<<20, 1.5, 50, 1000)
	if got != 1000*time.Millisecond {
		t.Fatalf("bootstrap hedgeDelay = %v, want 1000ms (max)", got)
	}
}

func TestPeerStatsClampFloorAndCeiling(t *testing.T) {
	p := newPeerStats()
	// Very fast peer: 1000 bytes/ms. A tiny 10-byte range predicts ~0ms; the
	// floor must hold it at PEER_HEDGE_MIN_MS.
	p.update("https://a:8000", 100000, 100*time.Millisecond) // 1000 bytes/ms
	if got := p.hedgeDelay("https://a:8000", 10, 1.5, 50, 1000); got != 50*time.Millisecond {
		t.Fatalf("tiny range hedgeDelay = %v, want floor 50ms", got)
	}
	// Huge range predicts far above the ceiling; the cap must hold it at max.
	if got := p.hedgeDelay("https://a:8000", 1<<40, 1.5, 50, 1000); got != 1000*time.Millisecond {
		t.Fatalf("huge range hedgeDelay = %v, want ceiling 1000ms", got)
	}
}

func TestPeerStatsPredictionInBand(t *testing.T) {
	p := newPeerStats()
	p.update("https://a:8000", 1_000_000, 100*time.Millisecond) // 10000 bytes/ms
	// 5,000,000 bytes / 10000 = 500ms predicted; *1.5 = 750ms, within [50,1000].
	if got := p.hedgeDelay("https://a:8000", 5_000_000, 1.5, 50, 1000); got != 750*time.Millisecond {
		t.Fatalf("hedgeDelay = %v, want 750ms", got)
	}
}

func TestPeerStatsEWMATightensAfterFastSample(t *testing.T) {
	p := newPeerStats()
	p.update("https://a:8000", 1_000_000, 1000*time.Millisecond) // slow: 1000 bytes/ms
	slow := p.hedgeDelay("https://a:8000", 1_000_000, 1.5, 50, 1000)
	// A run of fast samples must raise throughput and shorten the predicted delay.
	for i := 0; i < 10; i++ {
		p.update("https://a:8000", 1_000_000, 100*time.Millisecond) // fast: 10000 bytes/ms
	}
	fast := p.hedgeDelay("https://a:8000", 1_000_000, 1.5, 50, 1000)
	if !(fast < slow) {
		t.Fatalf("fast-sample delay %v must be < slow-sample delay %v", fast, slow)
	}
}

func TestPeerStatsFleetThroughput(t *testing.T) {
	p := newPeerStats()
	if p.fleetThroughput() != 0 {
		t.Fatal("empty fleetThroughput must be 0")
	}
	p.update("https://a:8000", 1000, 1*time.Millisecond) // 1000 bytes/ms
	p.update("https://b:8000", 3000, 1*time.Millisecond) // 3000 bytes/ms
	if got := p.fleetThroughput(); got != 2000 {
		t.Fatalf("fleetThroughput = %v, want 2000 (mean of 1000,3000)", got)
	}
}
