package main

import (
	"io"
	"log"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))

	cacheDir := env("CACHE_DIR", "./xorb-cache")
	if err := os.MkdirAll(cacheDir, 0o755); err != nil {
		log.Fatalf("cache dir: %v", err)
	}
	maxGiB, _ := strconv.ParseFloat(env("XORB_CACHE_MAX_GIB", "0"), 64)
	maxBytes := int64(maxGiB * float64(1<<30))

	// Disk-free watermark: an always-on backstop so the cache can never fill the
	// volume, independent of the byte budget. It measures real free space (so it
	// adapts to any disk size and self-corrects if byte accounting drifts). Set
	// CACHE_MIN_FREE_PCT=0 to disable and rely solely on XORB_CACHE_MAX_GIB.
	minFreePct := envInt("CACHE_MIN_FREE_PCT", 10)
	var minFree int64
	var freeFn func() int64
	if minFreePct > 0 {
		if _, total, ok := diskAvailBytes(cacheDir); ok {
			minFree = total * int64(minFreePct) / 100
			freeFn = func() int64 {
				avail, _, ok := diskAvailBytes(cacheDir)
				if !ok {
					return 1 << 62 // stat failed: report "plenty free" so a transient error never triggers a false purge
				}
				return avail
			}
		} else {
			slog.Warn("disk-free watermark disabled: cannot stat cache volume", "cache_dir", cacheDir)
		}
	}

	metrics := NewMetrics()
	lru := newLRU(maxBytes, minFree, freeFn, func(name string) {
		if err := os.Remove(filepath.Join(cacheDir, name)); err != nil && !os.IsNotExist(err) {
			slog.Warn("lru evict: remove failed", "file", name, "err", err)
		}
	})

	// Concurrency cap on distinct upstream miss fetches (0 = unlimited). Bounds
	// worst-case RSS (cap * max-range) and shields upstreams under a herd.
	var sem chan struct{}
	if n := envInt("MAX_INFLIGHT_FETCHES", 32); n > 0 {
		sem = make(chan struct{}, n)
	}

	// Peering ("Tier 1.5"): PEERS empty = disabled. Self is filtered out so a
	// cache never probes itself.
	peerList := parsePeers(env("PEERS", ""), env("SELF_URL", ""))
	var peers PeerSet
	if len(peerList) > 0 {
		peers = staticPeers{list: peerList}
	}
	stickyTTL := time.Duration(envInt("PEER_STICKY_TTL_SECONDS", 60)) * time.Second
	probeTimeout := time.Duration(envInt("PEER_PROBE_TIMEOUT_MS", 200)) * time.Millisecond
	fetchTimeout := time.Duration(envInt("PEER_FETCH_TIMEOUT_MS", 10000)) * time.Millisecond

	client := &http.Client{
		Timeout: 60 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
		Transport: &http.Transport{DisableCompression: true},
	}

	s := &Server{
		hfUpstream:  trimSlash(env("HF_UPSTREAM", "https://huggingface.co")),
		casUpstream: trimSlash(env("CAS_UPSTREAM", "https://cas-server.xethub.hf.co")),
		publicBase:  trimSlash(env("PUBLIC_BASE", "http://127.0.0.1:8000")),
		cacheDir:    cacheDir,
		signed: NewTTLMap(
			time.Duration(envInt("SIGNED_URL_TTL_SECONDS", 3600))*time.Second,
			envInt("SIGNED_URL_MAX_ENTRIES", 100000), nil),
		manifests:        newManifestCache(filepath.Join(cacheDir, "manifests"), envInt("MANIFEST_CACHE_MAX_ENTRIES", 50000)),
		metrics:          metrics,
		lru:              lru,
		doer:             client,
		signedCandidates: 8,
		sem:              sem,
		authToken:        env("SHIM_AUTH_TOKEN", ""),
		peers:            peers,
		sticky:           newStickyPeer(stickyTTL, nil),
		peerProbeTimeout: probeTimeout,
		peerFetchTimeout: fetchTimeout,
	}
	seedLRU(s)

	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("GET /metrics", func(w http.ResponseWriter, _ *http.Request) {
		snap := s.metrics.Snapshot()
		snap["signed_urls_tracked"] = s.signed.Len()
		writeJSON(w, http.StatusOK, snap)
	})
	mux.HandleFunc("GET /metrics/prometheus", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
		_, _ = io.WriteString(w, s.prometheusText())
	})
	mux.HandleFunc("GET /xorb/xorbs/default/{xorb_hash}", s.getXorb)
	mux.HandleFunc("GET /cas/{version}/reconstructions/{file_id}", s.reconstruction)
	// "GET /" matches both GET and HEAD requests per net/http's ServeMux
	// method-matching rules, so a separate "HEAD /" registration would
	// conflict with (and is redundant alongside) the other GET patterns.
	mux.HandleFunc("GET /", s.hub)

	handler := withLogging(withAuth(s.authToken, mux))

	port := env("PORT", "8000")
	slog.Info("xet-dc-cache starting",
		"port", port, "public_base", s.publicBase, "cache_dir", cacheDir,
		"max_gib", maxGiB, "min_free_pct", minFreePct, "max_inflight_fetches", cap(sem), "auth", s.authToken != "",
		"peers", len(peerList))
	if err := http.ListenAndServe("0.0.0.0:"+port, handler); err != nil {
		log.Fatal(err)
	}
}

func trimSlash(s string) string {
	for len(s) > 0 && s[len(s)-1] == '/' {
		s = s[:len(s)-1]
	}
	return s
}

// seedLRU rebuilds Tier 1 accounting from the cache dir at startup (mtime =
// recency), so a restart keeps the working set instead of re-pulling it. Only
// regular files directly under cacheDir are xorb entries (manifests live in a
// subdir and are skipped).
func seedLRU(s *Server) {
	entries, err := os.ReadDir(s.cacheDir)
	if err != nil {
		return
	}
	var seeds []lruSeed
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		// Skip dotfiles: getXorb's atomic write creates temp files named
		// "."+name+".tmp-*" in this same dir, which can be orphaned by a
		// crash mid-write. Real xorb cache entries are hex sha256 names and
		// never start with ".", so this only excludes temp-file debris.
		if strings.HasPrefix(e.Name(), ".") {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		seeds = append(seeds, lruSeed{Key: e.Name(), Size: info.Size(), MTime: info.ModTime()})
	}
	s.lru.Load(seeds)
}
