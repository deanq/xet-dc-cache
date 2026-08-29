package main

import (
	"testing"
	"time"
)

func TestTTLMapExpiry(t *testing.T) {
	now := time.Unix(0, 0)
	m := NewTTLMap(10*time.Second, 100, func() time.Time { return now })
	m.Set("k", []string{"a"})
	if v, ok := m.Get("k"); !ok || v[0] != "a" {
		t.Fatal("expected live entry")
	}
	now = now.Add(10 * time.Second) // now == expiry -> expired
	if _, ok := m.Get("k"); ok {
		t.Fatal("expected expiry at now>=expiry")
	}
}

func TestTTLMapCapDropsOldest(t *testing.T) {
	m := NewTTLMap(time.Hour, 2, nil)
	m.Set("a", []string{"1"})
	m.Set("b", []string{"2"})
	m.Set("c", []string{"3"}) // evicts "a"
	if _, ok := m.Get("a"); ok {
		t.Fatal("a should have been dropped")
	}
	if m.Len() != 2 {
		t.Fatalf("len = %d, want 2", m.Len())
	}
}
