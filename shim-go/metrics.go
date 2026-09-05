package main

import (
	"math"
	"sort"
	"sync"
)

// latencyBucketsMs are the upper bounds (le) for the xorb-latency histogram.
// Chosen to straddle PEER_HEDGE_MAX_MS's default (1000) so the tail the hedge
// guarantee is about is actually observable.
var latencyBucketsMs = []float64{1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000}

// histogram is a fixed-bucket latency histogram for one source label. counts is
// per-bucket (non-cumulative); the +Inf overflow lives in the final slot.
type histogram struct {
	counts []int64
	sum    float64
	count  int64
}

// HistogramSnapshot is one source's rendered histogram: cumulative bucket
// counts aligned to latencyBucketsMs plus the running sum and total count.
type HistogramSnapshot struct {
	Buckets    []float64
	Cumulative []int64
	Sum        float64
	Count      int64
}

// Metrics is a thread-safe counter bag plus per-source latency histograms.
// Headline derived value is wan_bytes_saved = served_bytes - wan_bytes (the WAN
// transfer the cache eliminated). Port of m1/metrics.py.
type Metrics struct {
	mu   sync.Mutex
	c    map[string]int64
	hist map[string]*histogram
}

func NewMetrics() *Metrics {
	return &Metrics{c: map[string]int64{}, hist: map[string]*histogram{}}
}

func (m *Metrics) Incr(key string, n int64) {
	m.mu.Lock()
	m.c[key] += n
	m.mu.Unlock()
}

// Observe records a served-range latency (milliseconds) under source (one of
// "hit", "peer", "cdn"). Bucketed so /metrics/prometheus can expose the p99 the
// hedge guarantee is about.
func (m *Metrics) Observe(source string, ms float64) {
	m.mu.Lock()
	defer m.mu.Unlock()
	h := m.hist[source]
	if h == nil {
		h = &histogram{counts: make([]int64, len(latencyBucketsMs)+1)}
		m.hist[source] = h
	}
	h.sum += ms
	h.count++
	i := sort.SearchFloat64s(latencyBucketsMs, ms)
	if i >= len(h.counts) { // ms greater than every bound -> +Inf overflow slot
		i = len(h.counts) - 1
	}
	h.counts[i]++
}

// Histograms returns each source's cumulative-bucket snapshot, sorted by source
// for stable exposition output.
func (m *Metrics) Histograms() map[string]HistogramSnapshot {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make(map[string]HistogramSnapshot, len(m.hist))
	for src, h := range m.hist {
		cum := make([]int64, len(latencyBucketsMs))
		var running int64
		for i := range latencyBucketsMs {
			running += h.counts[i]
			cum[i] = running
		}
		out[src] = HistogramSnapshot{
			Buckets:    latencyBucketsMs,
			Cumulative: cum,
			Sum:        h.sum,
			Count:      h.count, // == running + overflow slot
		}
	}
	return out
}

func (m *Metrics) Snapshot() map[string]any {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make(map[string]any, len(m.c)+2)
	for k, v := range m.c {
		out[k] = v
	}
	out["wan_bytes_saved"] = m.c["served_bytes"] - m.c["wan_bytes"]
	total := m.c["hits"] + m.c["misses"]
	rate := 0.0
	if total > 0 {
		rate = math.Round(float64(m.c["hits"])/float64(total)*10000) / 10000
	}
	out["hit_rate"] = rate

	for _, k := range []string{
		"hits", "misses", "wan_bytes", "served_bytes",
		"peer_hits", "peer_misses", "peer_bytes", "peer_probe_timeouts",
		"peer_hedge_fired", "peer_hedge_peer_won", "peer_hedge_cdn_won", "peer_bytes_wasted",
		"peer_hedge_cdn_bytes",
	} {
		if _, ok := out[k]; !ok {
			out[k] = int64(0)
		}
	}
	return out
}
