package main

import (
	"container/list"
	"sort"
	"sync"
	"time"
)

// lruCache is a byte-capped LRU accountant with an optional disk-free floor. It
// owns accounting, not files; eviction calls the injected delete callback. Two
// independent limits trigger eviction: a logical byte budget (max) and a
// real-disk watermark (minFree via freeFn). The watermark measures actual free
// space, so it backstops the disk even if byte accounting drifts from what's on
// disk. Port of m1/eviction.py, plus the disk floor.
type lruCache struct {
	mu      sync.Mutex
	max     int64        // byte budget; <=0 disables this limit
	minFree int64        // keep at least this many bytes free on the volume; <=0 disables
	freeFn  func() int64 // current bytes available on the cache volume
	del     func(string)
	order   *list.List // front = LRU, back = MRU
	items   map[string]*list.Element
	total   int64
}

type lruEntry struct {
	key  string
	size int64
}

type lruSeed struct {
	Key   string
	Size  int64
	MTime time.Time
}

func newLRU(maxBytes, minFree int64, freeFn func() int64, del func(string)) *lruCache {
	return &lruCache{
		max: maxBytes, minFree: minFree, freeFn: freeFn, del: del,
		order: list.New(), items: map[string]*list.Element{},
	}
}

func (c *lruCache) TotalBytes() int64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.total
}

func (c *lruCache) Load(entries []lruSeed) {
	c.mu.Lock()
	defer c.mu.Unlock()
	sort.Slice(entries, func(i, j int) bool { return entries[i].MTime.Before(entries[j].MTime) })
	for _, e := range entries {
		if _, ok := c.items[e.Key]; !ok {
			c.items[e.Key] = c.order.PushBack(&lruEntry{e.Key, e.Size})
			c.total += e.Size
		}
	}
	c.evictLocked()
}

func (c *lruCache) Touch(key string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if el, ok := c.items[key]; ok {
		c.order.MoveToBack(el)
	}
}

func (c *lruCache) Record(key string, size int64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if el, ok := c.items[key]; ok {
		c.total -= el.Value.(*lruEntry).size
		el.Value.(*lruEntry).size = size
		c.order.MoveToBack(el)
	} else {
		c.items[key] = c.order.PushBack(&lruEntry{key, size})
	}
	c.total += size
	c.evictLocked()
}

func (c *lruCache) Forget(key string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if el, ok := c.items[key]; ok {
		c.total -= el.Value.(*lruEntry).size
		c.order.Remove(el)
		delete(c.items, key)
	}
}

func (c *lruCache) evictLocked() {
	for c.order.Len() > 0 && c.overLimitLocked() {
		front := c.order.Front()
		e := front.Value.(*lruEntry)
		c.total -= e.size
		c.order.Remove(front)
		delete(c.items, e.key)
		c.del(e.key) // frees real disk, so the next freeFn() reflects it
	}
}

// overLimitLocked reports whether either limit is currently exceeded: the byte
// budget, or the disk-free watermark. freeFn is re-read each call so the loop in
// evictLocked stops as soon as enough real disk has been reclaimed. The byte
// budget is checked first, so freeFn (a statfs syscall) only runs once the
// budget is satisfied. Statfs is a local ~microsecond call and miss fetches are
// concurrency-capped, so we accept it in the locked path rather than sampling.
func (c *lruCache) overLimitLocked() bool {
	if c.max > 0 && c.total > c.max {
		return true
	}
	if c.minFree > 0 && c.freeFn != nil && c.freeFn() < c.minFree {
		return true
	}
	return false
}
