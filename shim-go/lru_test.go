package main

import (
	"testing"
	"time"
)

func TestLRUEvictsLeastRecentlyUsed(t *testing.T) {
	var deleted []string
	c := newLRU(10, 0, nil, func(k string) { deleted = append(deleted, k) })
	c.Record("a", 6)
	c.Record("b", 3)
	c.Touch("a")     // a is now MRU; b is LRU
	c.Record("c", 4) // total 13 > 10 -> evict LRU (b)
	if len(deleted) != 1 || deleted[0] != "b" {
		t.Fatalf("deleted = %v, want [b]", deleted)
	}
	if c.TotalBytes() != 10 {
		t.Fatalf("total = %d, want 10", c.TotalBytes())
	}
}

func TestLRUUnboundedWhenCapZero(t *testing.T) {
	c := newLRU(0, 0, nil, func(string) { t.Fatal("should never evict when both limits disabled") })
	c.Record("a", 1<<30)
	c.Record("b", 1<<30)
}

func TestLRUEvictsOnDiskFloor(t *testing.T) {
	// Byte budget disabled; keep >=50 bytes free. freeFn models a volume under
	// external pressure; each eviction reclaims 30 bytes of real disk.
	free := int64(100)
	var deleted []string
	c := newLRU(0, 50, func() int64 { return free }, func(k string) {
		deleted = append(deleted, k)
		free += 30
	})
	c.Record("a", 10)
	c.Record("b", 10) // free=100 >= 50 -> no eviction
	if len(deleted) != 0 {
		t.Fatalf("premature eviction: %v", deleted)
	}
	free = 40         // outside pressure drops the volume below the floor
	c.Record("c", 10) // floor breached -> evict LRU until free >= 50 (a -> free 70)
	if len(deleted) != 1 || deleted[0] != "a" {
		t.Fatalf("deleted = %v, want [a]", deleted)
	}
}

func TestLRULoadSeedsOldestFirst(t *testing.T) {
	var deleted []string
	c := newLRU(10, 0, nil, func(k string) { deleted = append(deleted, k) })
	base := time.Unix(1000, 0)
	c.Load([]lruSeed{
		{"new", 6, base.Add(2 * time.Second)},
		{"old", 6, base}, // older -> evicted first when over cap
	})
	if len(deleted) != 1 || deleted[0] != "old" {
		t.Fatalf("deleted = %v, want [old]", deleted)
	}
}
