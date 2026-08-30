package main

import (
	"os"
	"testing"
)

// TestSeedLRUSkipsTempFiles proves seedLRU excludes orphaned atomic-write
// temp files (named "."+name+".tmp-*" by getXorb's writeCacheFileAtomic)
// from LRU accounting, since they can never be a cache HIT and would
// otherwise just consume LRU budget until evicted.
func TestSeedLRUSkipsTempFiles(t *testing.T) {
	dir := t.TempDir()

	const realSize = 42
	if err := os.WriteFile(dir+"/abc123def", make([]byte, realSize), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(dir+"/.xyz.tmp-123", make([]byte, 999), 0o644); err != nil {
		t.Fatal(err)
	}

	s := &Server{
		cacheDir: dir,
		lru:      newLRU(0, 0, nil, func(string) {}),
	}
	seedLRU(s)

	if got := s.lru.TotalBytes(); got != realSize {
		t.Fatalf("TotalBytes = %d, want %d (temp file must be skipped)", got, realSize)
	}
}
