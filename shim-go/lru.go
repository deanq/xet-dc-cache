package main

import (
	"container/list"
	"sort"
	"sync"
	"time"
)

// lruCache is a byte-capped LRU accountant. It owns accounting, not files;
// eviction calls the injected delete callback. Port of m1/eviction.py.
type lruCache struct {
	mu    sync.Mutex
	max   int64
	del   func(string)
	order *list.List // front = LRU, back = MRU
	items map[string]*list.Element
	total int64
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

func newLRU(maxBytes int64, del func(string)) *lruCache {
	return &lruCache{max: maxBytes, del: del, order: list.New(), items: map[string]*list.Element{}}
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
	if c.max <= 0 {
		return
	}
	for c.total > c.max && c.order.Len() > 0 {
		front := c.order.Front()
		e := front.Value.(*lruEntry)
		c.total -= e.size
		c.order.Remove(front)
		delete(c.items, e.key)
		c.del(e.key)
	}
}
