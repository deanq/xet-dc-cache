package main

import "testing"

func TestMetricsSnapshotDerived(t *testing.T) {
	m := NewMetrics()
	m.Incr("hits", 3)
	m.Incr("misses", 1)
	m.Incr("served_bytes", 1000)
	m.Incr("wan_bytes", 250)
	s := m.Snapshot()
	if s["wan_bytes_saved"].(int64) != 750 {
		t.Fatalf("wan_bytes_saved = %v, want 750", s["wan_bytes_saved"])
	}
	if s["hit_rate"].(float64) != 0.75 {
		t.Fatalf("hit_rate = %v, want 0.75", s["hit_rate"])
	}
}

func TestMetricsEmptyHitRate(t *testing.T) {
	if NewMetrics().Snapshot()["hit_rate"].(float64) != 0.0 {
		t.Fatal("empty hit_rate should be 0.0")
	}
}

func TestMetricsHedgeCountersZeroFilled(t *testing.T) {
	s := NewMetrics().Snapshot()
	for _, k := range []string{"peer_hedge_fired", "peer_hedge_peer_won", "peer_hedge_cdn_won", "peer_bytes_wasted"} {
		if v, ok := s[k]; !ok || v.(int64) != 0 {
			t.Fatalf("snapshot[%q] = %v (ok=%v), want int64(0)", k, v, ok)
		}
	}
}
