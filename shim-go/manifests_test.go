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

func readFileExists(p string) ([]byte, error) { return os.ReadFile(p) }
