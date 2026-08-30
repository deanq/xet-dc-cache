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

func TestLRUDiskFloorEvictsMultipleInOneCall(t *testing.T) {
	// One breach that needs several evictions before the volume recovers, to
	// exercise the eviction loop (not just a single pass). Each eviction reclaims
	// 40 bytes; floor wants >= 100 free.
	free := int64(1000)
	var deleted []string
	c := newLRU(0, 100, func() int64 { return free }, func(k string) {
		deleted = append(deleted, k)
		free += 40
	})
	c.Record("a", 10)
	c.Record("b", 10)
	c.Record("c", 10) // order (LRU->MRU): a, b, c — free 1000, no eviction
	if len(deleted) != 0 {
		t.Fatalf("premature eviction: %v", deleted)
	}
	free = 20        // deep breach: needs 2 evictions (20 -> 60 -> 100) to clear
	c.Record("d", 0) // triggers eviction loop; d itself is size 0
	if len(deleted) != 2 || deleted[0] != "a" || deleted[1] != "b" {
		t.Fatalf("deleted = %v, want [a b]", deleted)
	}
}

func TestLRUDiskFloorEmptyCacheTerminates(t *testing.T) {
	// Volume permanently below the floor (e.g. other processes filling it). The
	// cache must evict everything it has and then stop — the order.Len() guard is
	// the only defense against an infinite loop. Test completing proves it stops.
	var deleted []string
	c := newLRU(0, 1000, func() int64 { return 0 }, func(k string) { deleted = append(deleted, k) })
	c.Record("a", 10)
	c.Record("b", 10)
	if len(deleted) != 2 {
		t.Fatalf("deleted = %v, want both evicted", deleted)
	}
	if c.TotalBytes() != 0 {
		t.Fatalf("total = %d, want 0", c.TotalBytes())
	}
}

func TestLRUBothLimitsActive(t *testing.T) {
	// Byte budget and disk floor together; each fires independently. del reclaims
	// 50 bytes of "disk" so the floor branch can settle.
	free := int64(1000)
	var deleted []string
	c := newLRU(100, 50, func() int64 { return free }, func(k string) {
		deleted = append(deleted, k)
		free += 50
	})
	c.Record("a", 60)
	c.Record("b", 60) // total 120 > 100 -> byte budget evicts LRU (a)
	if len(deleted) != 1 || deleted[0] != "a" {
		t.Fatalf("byte-budget eviction: deleted = %v, want [a]", deleted)
	}
	free = 10         // byte budget fine (total 60), but floor breached
	c.Record("c", 10) // floor evicts LRU (b), then free recovers to >= 50
	if len(deleted) != 2 || deleted[1] != "b" {
		t.Fatalf("floor eviction: deleted = %v, want [a b]", deleted)
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
