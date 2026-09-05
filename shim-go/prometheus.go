package main

import (
	"fmt"
	"sort"
	"strings"
)

// prometheusText renders the shim's counters and gauges in the Prometheus text
// exposition format (v0.0.4). Hand-rolled to avoid pulling in client_golang and
// its transitive deps — the shim tracks a fixed, small set of series. The JSON
// /metrics endpoint stays the source of truth for humans; this is for scrapers.
func (s *Server) prometheusText() string {
	snap := s.metrics.Snapshot()

	var b strings.Builder
	promMetric(&b, "xet_hits_total", "counter",
		"Xorb range requests served from the local cache.", snap["hits"])
	promMetric(&b, "xet_misses_total", "counter",
		"Xorb range requests that required an upstream fetch.", snap["misses"])
	promMetric(&b, "xet_wan_bytes_total", "counter",
		"Bytes fetched from upstream (WAN).", snap["wan_bytes"])
	promMetric(&b, "xet_served_bytes_total", "counter",
		"Bytes served to clients from the xorb store.", snap["served_bytes"])
	promMetric(&b, "xet_peer_hits_total", "counter",
		"Xorb ranges served by a sibling peer cache.", snap["peer_hits"])
	promMetric(&b, "xet_peer_misses_total", "counter",
		"Misses where peers were tried but none had the range (went to CDN).", snap["peer_misses"])
	promMetric(&b, "xet_peer_bytes_total", "counter",
		"Bytes pulled from peers (WAN/CDN transfer displaced).", snap["peer_bytes"])
	promMetric(&b, "xet_peer_probe_timeouts_total", "counter",
		"Peer discovery probes that exceeded PEER_PROBE_TIMEOUT.", snap["peer_probe_timeouts"])
	promMetric(&b, "xet_peer_hedge_fired_total", "counter",
		"Races where the CDN was hedged in (the adaptive timer fired).", snap["peer_hedge_fired"])
	promMetric(&b, "xet_peer_hedge_peer_won_total", "counter",
		"Hedged races the peer won (CDN GET cancelled).", snap["peer_hedge_peer_won"])
	promMetric(&b, "xet_peer_hedge_cdn_won_total", "counter",
		"Hedged races the CDN won (peer GET cancelled).", snap["peer_hedge_cdn_won"])
	promMetric(&b, "xet_peer_bytes_wasted_total", "counter",
		"CDN bytes discarded because the peer won after the CDN GET fired (cost of the latency insurance).", snap["peer_bytes_wasted"])
	promMetric(&b, "xet_peer_hedge_cdn_bytes_total", "counter",
		"Bytes booked to wan_bytes via a hedge CDN win (subset of wan_bytes; the rest is the plain no-peer fallthrough).", snap["peer_hedge_cdn_bytes"])
	promMetric(&b, "xet_wan_bytes_saved", "gauge",
		"served_bytes - wan_bytes: WAN transfer the cache eliminated.", snap["wan_bytes_saved"])
	promMetric(&b, "xet_hit_rate", "gauge",
		"hits / (hits + misses).", snap["hit_rate"])
	promMetric(&b, "xet_signed_urls_tracked", "gauge",
		"Distinct xorb hashes with a live signed CDN url.", int64(s.signed.Len()))
	promMetric(&b, "xet_cache_bytes", "gauge",
		"Bytes currently accounted in the Tier 1 LRU.", s.lru.TotalBytes())
	var throughput float64
	if s.peerStats != nil {
		throughput = s.peerStats.fleetThroughput()
	}
	promMetric(&b, "xet_peer_throughput_bytes_per_ms", "gauge",
		"Fleet-aggregate peer throughput EWMA (bytes/ms), used to size the hedge delay.", throughput)

	// Per-peer throughput EWMA — a labeled gauge so one slow peer is visible
	// where the fleet aggregate would hide it.
	if s.peerStats != nil {
		perPeer := s.peerStats.perPeer()
		peers := make([]string, 0, len(perPeer))
		for p := range perPeer {
			peers = append(peers, p)
		}
		sort.Strings(peers)
		if len(peers) > 0 {
			fmt.Fprintf(&b, "# HELP %s %s\n# TYPE %s gauge\n",
				"xet_peer_peer_throughput_bytes_per_ms",
				"Per-peer throughput EWMA (bytes/ms).",
				"xet_peer_peer_throughput_bytes_per_ms")
			for _, p := range peers {
				fmt.Fprintf(&b, "xet_peer_peer_throughput_bytes_per_ms{peer=%q} %g\n", p, perPeer[p])
			}
		}
	}

	promHistogram(&b, "xet_xorb_latency_ms",
		"Served-range latency by source (hit|peer|cdn), milliseconds.",
		s.metrics.Histograms())
	return b.String()
}

// promHistogram renders one Prometheus histogram family per source label.
func promHistogram(b *strings.Builder, name, help string, hists map[string]HistogramSnapshot) {
	if len(hists) == 0 {
		return
	}
	sources := make([]string, 0, len(hists))
	for src := range hists {
		sources = append(sources, src)
	}
	sort.Strings(sources)
	fmt.Fprintf(b, "# HELP %s %s\n# TYPE %s histogram\n", name, help, name)
	for _, src := range sources {
		h := hists[src]
		for i, ub := range h.Buckets {
			fmt.Fprintf(b, "%s_bucket{source=%q,le=%q} %d\n", name, src, fmt.Sprintf("%g", ub), h.Cumulative[i])
		}
		fmt.Fprintf(b, "%s_bucket{source=%q,le=\"+Inf\"} %d\n", name, src, h.Count)
		fmt.Fprintf(b, "%s_sum{source=%q} %g\n", name, src, h.Sum)
		fmt.Fprintf(b, "%s_count{source=%q} %d\n", name, src, h.Count)
	}
}

// promMetric appends one HELP/TYPE/sample block. Values are int64 (%d) or
// float64 (%g, Prometheus-parseable); anything else is skipped defensively.
func promMetric(b *strings.Builder, name, typ, help string, v any) {
	var sample string
	switch n := v.(type) {
	case int64:
		sample = fmt.Sprintf("%d", n)
	case float64:
		sample = fmt.Sprintf("%g", n)
	default:
		return
	}
	fmt.Fprintf(b, "# HELP %s %s\n# TYPE %s %s\n%s %s\n", name, help, name, typ, name, sample)
}
