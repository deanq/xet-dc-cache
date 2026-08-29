package main

import (
	"testing"
	"time"
)

func TestLRUEvictsLeastRecentlyUsed(t *testing.T) {
	var deleted []string
	c := newLRU(10, func(k string) { deleted = append(deleted, k) })
	c.Record("a", 6)
	c.Record("b", 3)
	c.Touch("a")        // a is now MRU; b is LRU
	c.Record("c", 4)    // total 13 > 10 -> evict LRU (b)
	if len(deleted) != 1 || deleted[0] != "b" {
		t.Fatalf("deleted = %v, want [b]", deleted)
	}
	if c.TotalBytes() != 10 {
		t.Fatalf("total = %d, want 10", c.TotalBytes())
	}
}

func TestLRUUnboundedWhenCapZero(t *testing.T) {
	c := newLRU(0, func(string) { t.Fatal("should never evict when cap=0") })
	c.Record("a", 1<<30)
	c.Record("b", 1<<30)
}

func TestLRULoadSeedsOldestFirst(t *testing.T) {
	var deleted []string
	c := newLRU(10, func(k string) { deleted = append(deleted, k) })
	base := time.Unix(1000, 0)
	c.Load([]lruSeed{
		{"new", 6, base.Add(2 * time.Second)},
		{"old", 6, base}, // older -> evicted first when over cap
	})
	if len(deleted) != 1 || deleted[0] != "old" {
		t.Fatalf("deleted = %v, want [old]", deleted)
	}
}
