package main

import (
	"testing"
	"time"
)

func TestParsePeers(t *testing.T) {
	cases := []struct {
		name, raw, self string
		want            []string
	}{
		{"empty", "", "", nil},
		{"single", "https://a:8000", "", []string{"https://a:8000"}},
		{"trims spaces and slashes", " https://a:8000/ , https://b:8000 ", "",
			[]string{"https://a:8000", "https://b:8000"}},
		{"drops empties", "https://a:8000,,https://b:8000,", "",
			[]string{"https://a:8000", "https://b:8000"}},
		{"excludes self", "https://a:8000,https://b:8000", "https://b:8000/",
			[]string{"https://a:8000"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := parsePeers(c.raw, c.self)
			if !equalSlice(got, c.want) {
				t.Fatalf("parsePeers(%q,%q) = %v, want %v", c.raw, c.self, got, c.want)
			}
		})
	}
}

func TestStaticPeersImplementsPeerSet(t *testing.T) {
	var ps PeerSet = staticPeers{list: []string{"https://a:8000"}}
	if got := ps.Peers(); !equalSlice(got, []string{"https://a:8000"}) {
		t.Fatalf("Peers() = %v", got)
	}
}

func TestStickyPeerSetGetExpiry(t *testing.T) {
	now := time.Unix(1000, 0)
	sp := newStickyPeer(60*time.Second, func() time.Time { return now })

	if _, ok := sp.get(); ok {
		t.Fatal("empty sticky should report not-live")
	}
	sp.set("https://a:8000")
	if url, ok := sp.get(); !ok || url != "https://a:8000" {
		t.Fatalf("get after set = (%q,%v)", url, ok)
	}
	now = now.Add(59 * time.Second)
	if _, ok := sp.get(); !ok {
		t.Fatal("should still be live at 59s (ttl 60s)")
	}
	now = now.Add(2 * time.Second) // 61s total
	if _, ok := sp.get(); ok {
		t.Fatal("should be expired past ttl")
	}
}
