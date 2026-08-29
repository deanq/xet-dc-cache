package main

import (
	"net/http"
	"path/filepath"
	"testing"
)

func filepathBase(p string) string {
	return filepath.Base(p)
}

func TestCachePathMatchesPython(t *testing.T) {
	// python: sha256(b"abc123:bytes=0-99").hexdigest()
	const want = "72c7deb58a78e56cdf5608a475339ca6dfdefd0f734c70fc1a438cb64e6eec8f"
	got := cachePath("/cache", "abc123", "bytes=0-99")
	if filepathBase(got) != want {
		t.Fatalf("cache key = %s, want %s", filepathBase(got), want)
	}
}

func TestParseRange(t *testing.T) {
	lo, end, err := parseRange("bytes=0-99")
	if err != nil || lo != 0 || end != 100 {
		t.Fatalf("got (%d,%d,%v), want (0,100,nil)", lo, end, err)
	}
	if _, _, err := parseRange("garbage"); err == nil {
		t.Fatal("expected error on garbage range")
	}
}

func TestCleanHeadersStripsHopByHop(t *testing.T) {
	src := http.Header{}
	src.Set("Content-Length", "123")
	src.Set("Content-Encoding", "gzip")
	src.Set("Connection", "keep-alive")
	src.Set("X-Xet-Hash", "keepme")
	dst := http.Header{}
	cleanHeaders(dst, src)
	if dst.Get("X-Xet-Hash") != "keepme" {
		t.Fatal("should keep X-Xet-Hash")
	}
	for _, drop := range []string{"Content-Length", "Content-Encoding", "Connection"} {
		if dst.Get(drop) != "" {
			t.Fatalf("should drop %s", drop)
		}
	}
}
