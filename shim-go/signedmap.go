package main

import (
	"container/list"
	"sync"
	"time"
)

// TTLMap holds ephemeral signed CDN urls: TTL-expiring, size-capped, oldest-drop.
// Value is a candidate list (a boundary-spanning xorb has several window urls).
// Port of m1/ttlmap.py.
type TTLMap struct {
	mu    sync.Mutex
	ttl   time.Duration
	max   int
	clock func() time.Time
	order *list.List // front = oldest
	items map[string]*list.Element
}

type ttlEntry struct {
	key    string
	expiry time.Time
	value  []string
}

func NewTTLMap(ttl time.Duration, max int, clock func() time.Time) *TTLMap {
	if clock == nil {
		clock = time.Now
	}
	return &TTLMap{ttl: ttl, max: max, clock: clock, order: list.New(), items: map[string]*list.Element{}}
}

func (m *TTLMap) Set(key string, value []string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	exp := m.clock().Add(m.ttl)
	if el, ok := m.items[key]; ok {
		e := el.Value.(*ttlEntry)
		e.expiry, e.value = exp, value
		m.order.MoveToBack(el)
	} else {
		m.items[key] = m.order.PushBack(&ttlEntry{key: key, expiry: exp, value: value})
	}
	for m.max > 0 && m.order.Len() > m.max {
		front := m.order.Front()
		delete(m.items, front.Value.(*ttlEntry).key)
		m.order.Remove(front)
	}
}

func (m *TTLMap) Get(key string) ([]string, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	el, ok := m.items[key]
	if !ok {
		return nil, false
	}
	e := el.Value.(*ttlEntry)
	if !m.clock().Before(e.expiry) { // now >= expiry
		delete(m.items, key)
		m.order.Remove(el)
		return nil, false
	}
	return e.value, true
}

func (m *TTLMap) Len() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.items)
}
