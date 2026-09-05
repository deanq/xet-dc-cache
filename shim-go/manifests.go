package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"sync"
)

// ManifestCache persists REWRITTEN reconstruction manifests (xorb urls already
// point at us, so they carry no CDN signature and are immutable). Range-keyed:
// manifests differ per Range, so serving the wrong range is incoherent.
// Port of m1/manifest_cache.py.
//
// Writes are atomic (temp + rename), so a crash or a concurrent read mid-write
// never yields a truncated manifest — which is why Get can read without the
// mutex (it sees either the old or the new complete file). Eviction is tracked
// in memory (a FIFO seeded from disk at boot) so trimming is O(1) amortized per
// Put rather than globbing + stat-ing the whole directory every time.
type ManifestCache struct {
	dir string
	max int

	mu    sync.Mutex
	order []string        // paths in eviction (insertion / mtime) order, oldest first
	known map[string]bool // membership, to avoid double-counting a re-Put
}

var manifestUnsafe = regexp.MustCompile(`[^0-9A-Za-z_-]`)

func newManifestCache(dir string, max int) *ManifestCache {
	_ = os.MkdirAll(dir, 0o755)
	m := &ManifestCache{dir: dir, max: max, known: map[string]bool{}}
	m.seedFromDisk()
	return m
}

// seedFromDisk rebuilds the eviction order from manifests left by earlier runs,
// oldest first, so the byte/entry budget is honored across restarts without a
// per-Put directory scan. Only needed when a cap is set.
func (m *ManifestCache) seedFromDisk() {
	if m.max <= 0 {
		return
	}
	files, _ := filepath.Glob(filepath.Join(m.dir, "*.json"))
	type fi struct {
		path  string
		mtime int64
	}
	infos := make([]fi, 0, len(files))
	for _, f := range files {
		st, err := os.Stat(f)
		if err != nil {
			continue
		}
		infos = append(infos, fi{f, st.ModTime().UnixNano()})
	}
	sort.Slice(infos, func(i, j int) bool { return infos[i].mtime < infos[j].mtime })
	for _, in := range infos {
		m.order = append(m.order, in.path)
		m.known[in.path] = true
	}
	m.trimLocked()
}

func (m *ManifestCache) path(version, fileID, rangeKey string) string {
	rk := manifestUnsafe.ReplaceAllString(rangeKey, "_")
	if rk == "" {
		rk = "full"
	}
	return filepath.Join(m.dir, version+"_"+fileID+"_"+rk+".json")
}

// Get reads without the mutex: writes are atomic (temp + rename), so a reader
// always sees a complete file, and a failed Unmarshal falls back to a miss.
func (m *ManifestCache) Get(version, fileID, rangeKey string) map[string]any {
	data, err := os.ReadFile(m.path(version, fileID, rangeKey))
	if err != nil {
		return nil
	}
	var out map[string]any
	if json.Unmarshal(data, &out) != nil {
		return nil
	}
	return out
}

func (m *ManifestCache) Put(version, fileID, rangeKey string, manifest map[string]any) {
	data, err := json.Marshal(manifest)
	if err != nil {
		return
	}
	p := m.path(version, fileID, rangeKey)
	if werr := writeCacheFileAtomic(m.dir, filepath.Base(p), p, data); werr != nil {
		return
	}
	if m.max <= 0 {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.known[p] { // a re-Put overwrites in place; don't double-count it
		m.known[p] = true
		m.order = append(m.order, p)
	}
	m.trimLocked()
}

// trimLocked evicts the oldest entries until at most max remain. O(k) in the
// number actually over budget (usually 0 or 1 per Put), not O(n) in the whole
// directory. Caller holds m.mu.
func (m *ManifestCache) trimLocked() {
	if m.max <= 0 {
		return
	}
	for len(m.order) > m.max {
		oldest := m.order[0]
		m.order = m.order[1:]
		delete(m.known, oldest)
		_ = os.Remove(oldest)
	}
}
