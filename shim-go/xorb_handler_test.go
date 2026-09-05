package main

import (
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
)

// countingDoer authorizes any url, returns 5 bytes, counts fetches.
type countingDoer struct{ n int32 }

func (d *countingDoer) Do(req *http.Request) (*http.Response, error) {
	atomic.AddInt32(&d.n, 1)
	h := http.Header{}
	h.Set("Content-Range", "bytes 0-4/999")
	return &http.Response{StatusCode: 206, Body: io.NopCloser(strings.NewReader("BYTES")), Header: h}, nil
}

func newXorbTestServer(t *testing.T, doer httpDoer) *Server {
	dir := t.TempDir()
	return &Server{
		cacheDir:         dir,
		signed:           NewTTLMap(3600e9, 100000, nil),
		metrics:          NewMetrics(),
		lru:              newLRU(0, 0, nil, func(string) {}),
		doer:             doer,
		signedCandidates: 8,
	}
}

func doGetXorb(s *Server, hash, rng string) *httptest.ResponseRecorder {
	req := httptest.NewRequest("GET", "/xorb/xorbs/default/"+hash, nil)
	req.SetPathValue("xorb_hash", hash)
	if rng != "" {
		req.Header.Set("Range", rng)
	}
	rec := httptest.NewRecorder()
	s.getXorb(rec, req)
	return rec
}

func TestGetXorbMissThenHit(t *testing.T) {
	d := &countingDoer{}
	s := newXorbTestServer(t, d)
	s.signed.Set("h", []string{"http://cdn/x"})

	rec1 := doGetXorb(s, "h", "bytes=0-4")
	if rec1.Code != 206 || rec1.Header().Get("X-Cache") != "MISS" || rec1.Body.String() != "BYTES" {
		t.Fatalf("miss: code=%d xcache=%s body=%q", rec1.Code, rec1.Header().Get("X-Cache"), rec1.Body.String())
	}
	rec2 := doGetXorb(s, "h", "bytes=0-4")
	if rec2.Code != 206 || rec2.Header().Get("X-Cache") != "HIT" {
		t.Fatalf("hit: code=%d xcache=%s", rec2.Code, rec2.Header().Get("X-Cache"))
	}
	// A 206 MUST carry Content-Range (RFC 7233); the HIT reconstructs it from the
	// requested range with "*" for the unknown total.
	if got := rec2.Header().Get("Content-Range"); got != "bytes 0-4/*" {
		t.Fatalf("HIT Content-Range = %q, want \"bytes 0-4/*\"", got)
	}
	if atomic.LoadInt32(&d.n) != 1 {
		t.Fatalf("fetched %d times, want 1 (second is a HIT)", d.n)
	}
}

func TestGetXorbRequiresRange(t *testing.T) {
	s := newXorbTestServer(t, &countingDoer{})
	if rec := doGetXorb(s, "h", ""); rec.Code != 400 {
		t.Fatalf("no-range code = %d, want 400", rec.Code)
	}
}

// TestWriteCacheFileAtomicFailureLeavesNoFile proves a failed cache write
// (temp write succeeds, rename fails) never leaves a partial/corrupt file
// readable at the destination path, and cleans up its temp file. This is
// the fix for the singleflight miss closure previously using a direct
// os.WriteFile(path, ...), which could leave a truncated file at path on a
// partial failure, silently corrupting a subsequent cache HIT.
func TestWriteCacheFileAtomicFailureLeavesNoFile(t *testing.T) {
	dir := t.TempDir()
	path := dir + "/x"
	// Force os.Rename to fail: target is an existing directory.
	if err := os.Mkdir(path, 0o755); err != nil {
		t.Fatal(err)
	}

	if err := writeCacheFileAtomic(dir, "x", path, []byte("BYTES")); err == nil {
		t.Fatal("want error from writeCacheFileAtomic, got nil")
	}

	if _, err := os.ReadFile(path); err == nil {
		t.Fatal("path became a readable regular file after a failed write; would cause a false HIT")
	}

	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if strings.Contains(e.Name(), ".tmp-") {
			t.Fatalf("leftover temp file after failed write: %s", e.Name())
		}
	}
}

// TestGetXorbFailedWriteLeavesNoFalseHit proves the getXorb miss path,
// specifically: if the on-disk cache write fails, the handler returns an
// error (not a corrupted success) and the NEXT request for the same range
// still misses (fetches again) instead of serving a truncated file as a
// false X-Cache: HIT.
func TestGetXorbFailedWriteLeavesNoFalseHit(t *testing.T) {
	d := &countingDoer{}
	s := newXorbTestServer(t, d)
	s.signed.Set("h", []string{"http://cdn/x"})

	path := cachePath(s.cacheDir, "h", "bytes=0-4")
	// Pre-create a directory at the cache path so the atomic rename fails.
	if err := os.Mkdir(path, 0o755); err != nil {
		t.Fatal(err)
	}

	rec := doGetXorb(s, "h", "bytes=0-4")
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("write-fail code = %d, want 500", rec.Code)
	}

	if _, err := os.ReadFile(path); err == nil {
		t.Fatal("cache path readable as a file after a failed write; next request would be a false HIT")
	}
}

// TestGetXorbMetricsCounters proves getXorb's metrics counters increment
// correctly across a MISS then a HIT for the same (hash, range): one wire
// fetch (singleflight/on-disk cache collapses the second call), but bytes
// are served to both callers.
func TestGetXorbMetricsCounters(t *testing.T) {
	d := &countingDoer{}
	s := newXorbTestServer(t, d)
	s.signed.Set("h", []string{"http://cdn/x"})

	doGetXorb(s, "h", "bytes=0-4")
	doGetXorb(s, "h", "bytes=0-4")

	snap := s.metrics.Snapshot()
	if got := snap["misses"].(int64); got != 1 {
		t.Fatalf("misses = %d, want 1", got)
	}
	if got := snap["hits"].(int64); got != 1 {
		t.Fatalf("hits = %d, want 1", got)
	}
	if got := snap["wan_bytes"].(int64); got != 5 {
		t.Fatalf("wan_bytes = %d, want 5 (one fetch of 5 bytes)", got)
	}
	if got := snap["served_bytes"].(int64); got != 10 {
		t.Fatalf("served_bytes = %d, want 10 (5 bytes served to each of 2 callers)", got)
	}
}

func TestGetXorbSingleflightCollapsesConcurrent(t *testing.T) {
	d := &countingDoer{}
	s := newXorbTestServer(t, d)
	s.signed.Set("h", []string{"http://cdn/x"})
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); doGetXorb(s, "h", "bytes=0-4") }()
	}
	wg.Wait()
	if got := atomic.LoadInt32(&d.n); got > 5 {
		t.Fatalf("fetched %d times; singleflight should collapse concurrent cold fetches", got)
	}
}
