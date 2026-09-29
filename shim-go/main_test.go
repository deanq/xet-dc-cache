package main

import (
	"os"
	"testing"
)

// TestEnvBool pins envBool's semantics — critical now that STREAM_CACHE_HITS
// defaults on: unset must yield the default, and "0"/"false" must be able to
// force it off.
func TestEnvBool(t *testing.T) {
	const k = "XET_TEST_BOOL"
	cases := []struct {
		val string
		set bool
		def bool
		want bool
	}{
		{set: false, def: true, want: true},   // unset -> default (on)
		{set: false, def: false, want: false}, // unset -> default (off)
		{val: "1", set: true, def: false, want: true},
		{val: "true", set: true, def: false, want: true},
		{val: "YES", set: true, def: false, want: true},
		{val: "0", set: true, def: true, want: false},     // explicit disable
		{val: "false", set: true, def: true, want: false},
		{val: "", set: true, def: true, want: true},       // empty -> default
	}
	for _, c := range cases {
		os.Unsetenv(k)
		if c.set {
			t.Setenv(k, c.val)
		}
		if got := envBool(k, c.def); got != c.want {
			t.Errorf("envBool(%q=%q set=%v, def=%v) = %v, want %v", k, c.val, c.set, c.def, got, c.want)
		}
	}
}

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
