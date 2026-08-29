package main

import (
	"math"
	"sync"
)

// Metrics is a thread-safe counter bag. Headline derived value is
// wan_bytes_saved = served_bytes - wan_bytes (the WAN transfer the cache
// eliminated). Port of m1/metrics.py.
type Metrics struct {
	mu sync.Mutex
	c  map[string]int64
}

func NewMetrics() *Metrics { return &Metrics{c: map[string]int64{}} }

func (m *Metrics) Incr(key string, n int64) {
	m.mu.Lock()
	m.c[key] += n
	m.mu.Unlock()
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
	} {
		if _, ok := out[k]; !ok {
			out[k] = int64(0)
		}
	}
	return out
}
