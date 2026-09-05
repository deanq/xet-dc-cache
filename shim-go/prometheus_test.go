package main

import (
	"strings"
	"testing"
	"time"
)

func TestPrometheusText(t *testing.T) {
	s := &Server{
		metrics: NewMetrics(),
		signed:  NewTTLMap(3600e9, 100, nil),
		lru:     newLRU(0, 0, nil, func(string) {}),
	}
	s.metrics.Incr("hits", 3)
	s.metrics.Incr("misses", 1)
	s.metrics.Incr("wan_bytes", 100)
	s.metrics.Incr("served_bytes", 400)
	s.signed.Set("h", []string{"u"})
	s.lru.Record("k", 50)

	out := s.prometheusText()
	for _, want := range []string{
		"# TYPE xet_hits_total counter\nxet_hits_total 3\n",
		"xet_misses_total 1\n",
		"xet_wan_bytes_total 100\n",
		"xet_served_bytes_total 400\n",
		"xet_wan_bytes_saved 300\n", // served - wan
		"xet_hit_rate 0.75\n",       // 3/(3+1)
		"xet_signed_urls_tracked 1\n",
		"xet_cache_bytes 50\n",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in exposition:\n%s", want, out)
		}
	}
}

// Every metric must carry a TYPE line (scrapers reject bare samples in strict
// mode) — guard against adding a sample without its header.
func TestPrometheusEveryMetricHasType(t *testing.T) {
	s := &Server{metrics: NewMetrics(), signed: NewTTLMap(1, 1, nil), lru: newLRU(0, 0, nil, func(string) {})}
	types := strings.Count(s.prometheusText(), "# TYPE ")
	samples := 0
	for _, line := range strings.Split(strings.TrimSpace(s.prometheusText()), "\n") {
		if line != "" && !strings.HasPrefix(line, "#") {
			samples++
		}
	}
	if types != samples {
		t.Fatalf("%d TYPE lines but %d samples — every metric needs a TYPE", types, samples)
	}
}

func TestPrometheusHedgeSeries(t *testing.T) {
	s := &Server{
		metrics:   NewMetrics(),
		signed:    NewTTLMap(3600e9, 100, nil),
		lru:       newLRU(0, 0, nil, func(string) {}),
		peerStats: newPeerStats(),
	}
	s.metrics.Incr("peer_hedge_fired", 4)
	s.metrics.Incr("peer_hedge_peer_won", 1)
	s.metrics.Incr("peer_hedge_cdn_won", 3)
	s.metrics.Incr("peer_bytes_wasted", 2048)
	s.metrics.Incr("peer_hedge_cdn_bytes", 4096)
	s.peerStats.update("https://a:8000", 1000, 1*time.Millisecond) // 1000 bytes/ms
	s.metrics.Observe("cdn", 40)

	out := s.prometheusText()
	for _, want := range []string{
		"# TYPE xet_peer_hedge_fired_total counter\nxet_peer_hedge_fired_total 4\n",
		"xet_peer_hedge_peer_won_total 1\n",
		"xet_peer_hedge_cdn_won_total 3\n",
		"xet_peer_bytes_wasted_total 2048\n",
		"xet_peer_hedge_cdn_bytes_total 4096\n",
		"# TYPE xet_peer_throughput_bytes_per_ms gauge\nxet_peer_throughput_bytes_per_ms 1000\n",
		"xet_peer_peer_throughput_bytes_per_ms{peer=\"https://a:8000\"} 1000\n",
		"# TYPE xet_xorb_latency_ms histogram\n",
		"xet_xorb_latency_ms_bucket{source=\"cdn\",le=\"50\"} 1\n",
		"xet_xorb_latency_ms_bucket{source=\"cdn\",le=\"+Inf\"} 1\n",
		"xet_xorb_latency_ms_count{source=\"cdn\"} 1\n",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in exposition:\n%s", want, out)
		}
	}
}
