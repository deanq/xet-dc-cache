package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestManifestCacheRangeKeyed(t *testing.T) {
	dir := t.TempDir()
	mc := newManifestCache(dir, 0)
	mc.Put("v2", "fileA", "bytes=0-99", map[string]any{"r": "first"})
	mc.Put("v2", "fileA", "bytes=100-199", map[string]any{"r": "second"})
	if got := mc.Get("v2", "fileA", "bytes=0-99")["r"]; got != "first" {
		t.Fatalf("range 0-99 = %v, want first", got)
	}
	if got := mc.Get("v2", "fileA", "bytes=100-199")["r"]; got != "second" {
		t.Fatalf("range 100-199 = %v, want second", got)
	}
	if mc.Get("v2", "fileA", "bytes=999-999") != nil {
		t.Fatal("unknown range should be nil")
	}
}

func TestManifestFilenameMatchesPython(t *testing.T) {
	dir := t.TempDir()
	mc := newManifestCache(dir, 0)
	mc.Put("v2", "abc", "bytes=0-99", map[string]any{"x": 1})
	// python: f"{version}_{file_id}_{rk}.json" with [^0-9A-Za-z_-]->_
	want := filepath.Join(dir, "v2_abc_bytes_0-99.json")
	if _, err := readFileExists(want); err != nil {
		t.Fatalf("expected file %s: %v", want, err)
	}
	// empty range -> "full"
	mc.Put("v2", "abc", "", map[string]any{"x": 1})
	if _, err := readFileExists(filepath.Join(dir, "v2_abc_full.json")); err != nil {
		t.Fatalf("expected full-range file: %v", err)
	}
}

func TestManifestCacheEvictsOldestOverCap(t *testing.T) {
	dir := t.TempDir()
	mc := newManifestCache(dir, 2)
	mc.Put("v", "a", "r1", map[string]any{"n": 1})
	mc.Put("v", "b", "r2", map[string]any{"n": 2})
	mc.Put("v", "c", "r3", map[string]any{"n": 3}) // evicts the oldest (a)

	if mc.Get("v", "a", "r1") != nil {
		t.Fatal("oldest entry (a) should have been evicted")
	}
	if mc.Get("v", "b", "r2") == nil || mc.Get("v", "c", "r3") == nil {
		t.Fatal("the two most-recent entries must survive")
	}
}

func TestManifestCacheRePutDoesNotDoubleCount(t *testing.T) {
	dir := t.TempDir()
	mc := newManifestCache(dir, 2)
	mc.Put("v", "a", "r", map[string]any{"n": 1})
	mc.Put("v", "a", "r", map[string]any{"n": 2}) // same key, overwrite
	mc.Put("v", "b", "r", map[string]any{"n": 3})
	// a and b are the only two distinct keys; both must survive (the re-Put of
	// a must not have counted as a second slot and evicted anything).
	if mc.Get("v", "a", "r")["n"] != float64(2) {
		t.Fatalf("a = %v, want overwritten value 2", mc.Get("v", "a", "r")["n"])
	}
	if mc.Get("v", "b", "r") == nil {
		t.Fatal("b must survive")
	}
}

func TestManifestCacheSeedTrimsAcrossRestart(t *testing.T) {
	dir := t.TempDir()
	first := newManifestCache(dir, 0) // unlimited: all three persist
	first.Put("v", "a", "r1", map[string]any{"n": 1})
	first.Put("v", "b", "r2", map[string]any{"n": 2})
	first.Put("v", "c", "r3", map[string]any{"n": 3})

	// Restart with a cap of 2: seeding from disk must trim to the 2 newest.
	second := newManifestCache(dir, 2)
	surviving := 0
	for _, k := range []struct{ id, rk string }{{"a", "r1"}, {"b", "r2"}, {"c", "r3"}} {
		if second.Get("v", k.id, k.rk) != nil {
			surviving++
		}
	}
	if surviving != 2 {
		t.Fatalf("after restart with cap=2, %d entries survived, want 2", surviving)
	}
}

func readFileExists(p string) ([]byte, error) { return os.ReadFile(p) }
