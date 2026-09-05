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

func TestMetricsEffectiveHitRateCreditsPeer(t *testing.T) {
	m := NewMetrics()
	// One disk hit, one peer-served miss (peer wins book both misses & peer_hits).
	m.Incr("hits", 1)
	m.Incr("misses", 1)
	m.Incr("peer_hits", 1)
	s := m.Snapshot()
	if s["hit_rate"].(float64) != 0.5 {
		t.Fatalf("hit_rate = %v, want 0.5 (peer win counts as a miss)", s["hit_rate"])
	}
	if s["effective_hit_rate"].(float64) != 1.0 {
		t.Fatalf("effective_hit_rate = %v, want 1.0 (both served without CDN)", s["effective_hit_rate"])
	}
}

func TestMetricsHedgeCountersZeroFilled(t *testing.T) {
	s := NewMetrics().Snapshot()
	for _, k := range []string{"peer_hedge_fired", "peer_hedge_peer_won", "peer_hedge_cdn_won", "peer_bytes_wasted", "peer_hedge_cdn_bytes"} {
		if v, ok := s[k]; !ok || v.(int64) != 0 {
			t.Fatalf("snapshot[%q] = %v (ok=%v), want int64(0)", k, v, ok)
		}
	}
}

func TestMetricsHistogramBucketsAndSum(t *testing.T) {
	m := NewMetrics()
	// 3ms -> le=5 bucket; 40ms -> le=50; 7000ms -> +Inf overflow.
	m.Observe("cdn", 3)
	m.Observe("cdn", 40)
	m.Observe("cdn", 7000)

	h := m.Histograms()["cdn"]
	if h.Count != 3 {
		t.Fatalf("count = %d, want 3", h.Count)
	}
	if h.Sum != 7043 {
		t.Fatalf("sum = %v, want 7043", h.Sum)
	}
	// Cumulative: le=1 -> 0, le=5 -> 1, le=25 -> 1, le=50 -> 2, le=5000 -> 2.
	want := map[float64]int64{1: 0, 5: 1, 25: 1, 50: 2, 5000: 2}
	for i, ub := range h.Buckets {
		if exp, ok := want[ub]; ok && h.Cumulative[i] != exp {
			t.Errorf("cumulative[le=%g] = %d, want %d", ub, h.Cumulative[i], exp)
		}
	}
	// The +Inf total (Count) must include the 7000ms overflow.
	if h.Cumulative[len(h.Cumulative)-1] != 2 || h.Count != 3 {
		t.Fatalf("finite cum=%d (want 2), +Inf=%d (want 3)", h.Cumulative[len(h.Cumulative)-1], h.Count)
	}
}
