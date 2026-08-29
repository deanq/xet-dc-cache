package main

import (
	"fmt"
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
	promMetric(&b, "xet_wan_bytes_saved", "gauge",
		"served_bytes - wan_bytes: WAN transfer the cache eliminated.", snap["wan_bytes_saved"])
	promMetric(&b, "xet_hit_rate", "gauge",
		"hits / (hits + misses).", snap["hit_rate"])
	promMetric(&b, "xet_signed_urls_tracked", "gauge",
		"Distinct xorb hashes with a live signed CDN url.", int64(s.signed.Len()))
	promMetric(&b, "xet_cache_bytes", "gauge",
		"Bytes currently accounted in the Tier 1 LRU.", s.lru.TotalBytes())
	return b.String()
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
