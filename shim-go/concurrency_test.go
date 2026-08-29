package main

import (
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"
)

// gatedDoer blocks each fetch on `release` and records the peak number of
// simultaneously in-flight fetches, so a test can prove the semaphore caps
// concurrent distinct upstream misses.
type gatedDoer struct {
	mu          sync.Mutex
	inflight    int
	maxInflight int
	release     chan struct{}
}

func (d *gatedDoer) Do(*http.Request) (*http.Response, error) {
	d.mu.Lock()
	d.inflight++
	if d.inflight > d.maxInflight {
		d.maxInflight = d.inflight
	}
	d.mu.Unlock()

	<-d.release // hold the fetch open until the test lets it complete

	d.mu.Lock()
	d.inflight--
	d.mu.Unlock()

	h := http.Header{}
	h.Set("Content-Range", "bytes 0-4/999")
	return &http.Response{StatusCode: 206, Body: io.NopCloser(strings.NewReader("BYTES")), Header: h}, nil
}

func (d *gatedDoer) peakInflight() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.maxInflight
}

func (d *gatedDoer) curInflight() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.inflight
}

func waitFor(t *testing.T, cond func() bool, msg string) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for: %s", msg)
}

// With cap=1, two concurrent distinct-range misses must not both be in flight.
func TestFetchCapSerializesConcurrentMisses(t *testing.T) {
	d := &gatedDoer{release: make(chan struct{})}
	s := newXorbTestServer(t, d)
	s.sem = make(chan struct{}, 1)
	s.signed.Set("a", []string{"http://cdn/a"})
	s.signed.Set("b", []string{"http://cdn/b"})

	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); doGetXorb(s, "a", "bytes=0-4") }()
	waitFor(t, func() bool { return d.curInflight() == 1 }, "first fetch to enter")

	go func() { defer wg.Done(); doGetXorb(s, "b", "bytes=0-4") }()
	// Second fetch must block on the semaphore, not enter Do. Give it room to
	// misbehave; inflight must stay 1.
	time.Sleep(50 * time.Millisecond)
	if got := d.curInflight(); got != 1 {
		t.Fatalf("inflight = %d while cap=1; second miss was not gated", got)
	}

	d.release <- struct{}{} // let the first finish -> second acquires
	waitFor(t, func() bool { return d.curInflight() == 1 }, "second fetch to enter after first releases")
	d.release <- struct{}{} // let the second finish
	wg.Wait()

	if peak := d.peakInflight(); peak != 1 {
		t.Fatalf("peak inflight = %d, want 1 (cap must serialize)", peak)
	}
}

// With the cap disabled (sem=nil), distinct misses run concurrently — proving
// the serialization above is the cap, not an incidental lock.
func TestNoCapAllowsConcurrentMisses(t *testing.T) {
	d := &gatedDoer{release: make(chan struct{})}
	s := newXorbTestServer(t, d)
	s.sem = nil // unlimited
	s.signed.Set("a", []string{"http://cdn/a"})
	s.signed.Set("b", []string{"http://cdn/b"})

	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); doGetXorb(s, "a", "bytes=0-4") }()
	go func() { defer wg.Done(); doGetXorb(s, "b", "bytes=0-4") }()

	waitFor(t, func() bool { return d.curInflight() == 2 }, "both fetches in flight concurrently")
	close(d.release)
	wg.Wait()

	if peak := d.peakInflight(); peak != 2 {
		t.Fatalf("peak inflight = %d, want 2 (no cap should allow concurrency)", peak)
	}
}
