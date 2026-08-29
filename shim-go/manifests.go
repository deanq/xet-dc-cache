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
type ManifestCache struct {
	dir string
	max int
	mu  sync.Mutex
}

var manifestUnsafe = regexp.MustCompile(`[^0-9A-Za-z_-]`)

func newManifestCache(dir string, max int) *ManifestCache {
	_ = os.MkdirAll(dir, 0o755)
	return &ManifestCache{dir: dir, max: max}
}

func (m *ManifestCache) path(version, fileID, rangeKey string) string {
	rk := manifestUnsafe.ReplaceAllString(rangeKey, "_")
	if rk == "" {
		rk = "full"
	}
	return filepath.Join(m.dir, version+"_"+fileID+"_"+rk+".json")
}

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
	m.mu.Lock()
	defer m.mu.Unlock()
	data, err := json.Marshal(manifest)
	if err != nil {
		return
	}
	_ = os.WriteFile(m.path(version, fileID, rangeKey), data, 0o644)
	m.trimLocked()
}

func (m *ManifestCache) trimLocked() {
	if m.max <= 0 {
		return
	}
	files, _ := filepath.Glob(filepath.Join(m.dir, "*.json"))
	if len(files) <= m.max {
		return
	}
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
	for i := 0; i < len(infos)-m.max; i++ {
		_ = os.Remove(infos[i].path)
	}
}
