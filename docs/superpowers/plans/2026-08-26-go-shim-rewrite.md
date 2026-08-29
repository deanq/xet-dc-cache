# Go Shim Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the shipping Python Xet DC-cache shim (`m1/shim.py` + support modules) to a single-binary Go service with 1:1 behavioral parity.

**Architecture:** One self-referential `net/http` service playing three roles — (A) Hub proxy that rewrites only the `xet-read-token` `casUrl`; (B) CAS relay that rewrites reconstruction xorb urls and stashes signed CDN urls; (C) Tier 1 xorb range cache keyed by `(hash, range)`. Flat `main` package, one file per concern. The on-disk cache format is byte-identical to the Python shim so the warm production cache is reused.

**Tech Stack:** Go 1.22+ (stdlib `net/http` with `ServeMux` path params), `golang.org/x/sync/singleflight` (only third-party dep). Existing Python `smoke_test.py`/`integration_test.py` reused as the black-box acceptance gate.

## Global Constraints

- **Go 1.22+** required (`ServeMux` method+wildcard patterns, `r.PathValue`).
- **Only third-party dependency:** `golang.org/x/sync/singleflight`. Nothing else in `go.mod`.
- **Module path:** `xetcache` (local module — GitHub push is on hold; do not bake in a remote path).
- **All code lives in `shim-go/`.** The Python shim in `m1/` stays untouched and runnable as rollback.
- **On-disk parity is load-bearing.** Xorb cache key = `sha256(fmt.Sprintf("%s:%s", hash, byteRange))` hex. Manifest filename = `{version}_{file_id}_{sanitizedRange or "full"}.json`, sanitize rule `[^0-9A-Za-z_-] → _`. These MUST match Python exactly.
- **Env var names/defaults** unchanged from `shim.py`: `HF_UPSTREAM` (`https://huggingface.co`), `CAS_UPSTREAM` (`https://cas-server.xethub.hf.co`), `PUBLIC_BASE` (`http://127.0.0.1:8000`), `CACHE_DIR` (`./xorb-cache`), `XORB_CACHE_MAX_GIB` (`0`=unbounded), `SIGNED_URL_TTL_SECONDS` (`3600`), `SIGNED_URL_MAX_ENTRIES` (`100000`), `MANIFEST_CACHE_MAX_ENTRIES` (`50000`), `PORT` (`8000`). **`CACHE_TIER` is dropped.**
- **HTTP client:** `CheckRedirect` returns `http.ErrUseLastResponse` (no redirect following); `Transport.DisableCompression = true`; 60s timeout. All upstream requests set `Accept-Encoding: identity` (control-plane + range bytes only; Xet file bytes never traverse the hub path).
- **Commits:** local only. Do NOT push (GitHub hold in effect).
- **Naming convention across tasks:** `Server` (methods `getXorb`, `reconstruction`, `hub`, `proxyHub`, `fetchAuthorized`, `rememberSigned`, `rewriteManifest`), `Metrics`, `TTLMap`, `lruCache`, `ManifestCache`, `httpDoer`, `httpError`, helpers `cachePath`, `parseRange`, `cleanHeaders`, `writeJSON`, `writeXorb`.

---

### Task 0: Toolchain + module scaffold

**Files:**
- Create: `shim-go/go.mod`, `shim-go/main.go` (temporary stub)

**Interfaces:**
- Consumes: nothing.
- Produces: a buildable `main` package in `shim-go/` with `x/sync` available.

- [x] **Step 1: Install Go** — DONE (go1.27.0 darwin/arm64 already installed).

Verify: `go version` → `go version go1.27.0 darwin/arm64` (≥1.22 ✓).

- [ ] **Step 2: Init module and add the one dependency**

```bash
cd shim-go
go mod init xetcache
go get golang.org/x/sync/singleflight
```

- [ ] **Step 3: Write a stub `main.go` so the package builds**

```go
package main

func main() {}
```

- [ ] **Step 4: Verify it builds**

Run: `cd shim-go && go build ./... && go vet ./...`
Expected: no output, exit 0.

- [ ] **Step 5: Commit**

```bash
git add shim-go/go.mod shim-go/go.sum shim-go/main.go
git commit -m "chore: scaffold shim-go module"
```

---

### Task 1: Metrics

**Files:**
- Create: `shim-go/metrics.go`, `shim-go/metrics_test.go`

**Interfaces:**
- Produces: `type Metrics`; `NewMetrics() *Metrics`; `(*Metrics).Incr(key string, n int64)`; `(*Metrics).Snapshot() map[string]any` — includes every counter plus `wan_bytes_saved = served_bytes - wan_bytes` and `hit_rate = round(hits/(hits+misses), 4)` (0.0 when no requests).

- [ ] **Step 1: Write the failing test**

```go
package main

import "testing"

func TestMetricsSnapshotDerived(t *testing.T) {
	m := NewMetrics()
	m.Incr("hits", 3)
	m.Incr("misses", 1)
	m.Incr("served_bytes", 1000)
	m.Incr("wan_bytes", 250)
	s := m.Snapshot()
	if s["wan_bytes_saved"].(int64) != 750 {
		t.Fatalf("wan_bytes_saved = %v, want 750", s["wan_bytes_saved"])
	}
	if s["hit_rate"].(float64) != 0.75 {
		t.Fatalf("hit_rate = %v, want 0.75", s["hit_rate"])
	}
}

func TestMetricsEmptyHitRate(t *testing.T) {
	if NewMetrics().Snapshot()["hit_rate"].(float64) != 0.0 {
		t.Fatal("empty hit_rate should be 0.0")
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd shim-go && go test -run TestMetrics ./...`
Expected: FAIL (undefined: NewMetrics).

- [ ] **Step 3: Implement `metrics.go`**

```go
package main

import (
	"math"
	"sync"
)

// Metrics is a thread-safe counter bag. Headline derived value is
// wan_bytes_saved = served_bytes - wan_bytes (the WAN transfer the cache
// eliminated). Port of m1/metrics.py.
type Metrics struct {
	mu sync.Mutex
	c  map[string]int64
}

func NewMetrics() *Metrics { return &Metrics{c: map[string]int64{}} }

func (m *Metrics) Incr(key string, n int64) {
	m.mu.Lock()
	m.c[key] += n
	m.mu.Unlock()
}

func (m *Metrics) Snapshot() map[string]any {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make(map[string]any, len(m.c)+2)
	for k, v := range m.c {
		out[k] = v
	}
	out["wan_bytes_saved"] = m.c["served_bytes"] - m.c["wan_bytes"]
	total := m.c["hits"] + m.c["misses"]
	rate := 0.0
	if total > 0 {
		rate = math.Round(float64(m.c["hits"])/float64(total)*10000) / 10000
	}
	out["hit_rate"] = rate
	return out
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd shim-go && go test -run TestMetrics ./...`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shim-go/metrics.go shim-go/metrics_test.go
git commit -m "feat: port Metrics to Go"
```

---

### Task 2: TTLMap (signed-url side-map)

**Files:**
- Create: `shim-go/signedmap.go`, `shim-go/signedmap_test.go`

**Interfaces:**
- Produces: `type TTLMap`; `NewTTLMap(ttl time.Duration, max int, clock func() time.Time) *TTLMap` (nil clock → `time.Now`); `(*TTLMap).Set(key string, value []string)`; `(*TTLMap).Get(key string) ([]string, bool)`; `(*TTLMap).Len() int`. Value is `[]string` (the candidate-url list; matches actual `shim.py` usage). Expiry is `now >= expiry`; oldest-drop when over `max`.

- [ ] **Step 1: Write the failing test**

```go
package main

import (
	"testing"
	"time"
)

func TestTTLMapExpiry(t *testing.T) {
	now := time.Unix(0, 0)
	m := NewTTLMap(10*time.Second, 100, func() time.Time { return now })
	m.Set("k", []string{"a"})
	if v, ok := m.Get("k"); !ok || v[0] != "a" {
		t.Fatal("expected live entry")
	}
	now = now.Add(10 * time.Second) // now == expiry -> expired
	if _, ok := m.Get("k"); ok {
		t.Fatal("expected expiry at now>=expiry")
	}
}

func TestTTLMapCapDropsOldest(t *testing.T) {
	m := NewTTLMap(time.Hour, 2, nil)
	m.Set("a", []string{"1"})
	m.Set("b", []string{"2"})
	m.Set("c", []string{"3"}) // evicts "a"
	if _, ok := m.Get("a"); ok {
		t.Fatal("a should have been dropped")
	}
	if m.Len() != 2 {
		t.Fatalf("len = %d, want 2", m.Len())
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd shim-go && go test -run TestTTLMap ./...`
Expected: FAIL (undefined: NewTTLMap).

- [ ] **Step 3: Implement `signedmap.go`**

```go
package main

import (
	"container/list"
	"sync"
	"time"
)

// TTLMap holds ephemeral signed CDN urls: TTL-expiring, size-capped, oldest-drop.
// Value is a candidate list (a boundary-spanning xorb has several window urls).
// Port of m1/ttlmap.py.
type TTLMap struct {
	mu    sync.Mutex
	ttl   time.Duration
	max   int
	clock func() time.Time
	order *list.List // front = oldest
	items map[string]*list.Element
}

type ttlEntry struct {
	key    string
	expiry time.Time
	value  []string
}

func NewTTLMap(ttl time.Duration, max int, clock func() time.Time) *TTLMap {
	if clock == nil {
		clock = time.Now
	}
	return &TTLMap{ttl: ttl, max: max, clock: clock, order: list.New(), items: map[string]*list.Element{}}
}

func (m *TTLMap) Set(key string, value []string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	exp := m.clock().Add(m.ttl)
	if el, ok := m.items[key]; ok {
		e := el.Value.(*ttlEntry)
		e.expiry, e.value = exp, value
		m.order.MoveToBack(el)
	} else {
		m.items[key] = m.order.PushBack(&ttlEntry{key: key, expiry: exp, value: value})
	}
	for m.max > 0 && m.order.Len() > m.max {
		front := m.order.Front()
		delete(m.items, front.Value.(*ttlEntry).key)
		m.order.Remove(front)
	}
}

func (m *TTLMap) Get(key string) ([]string, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	el, ok := m.items[key]
	if !ok {
		return nil, false
	}
	e := el.Value.(*ttlEntry)
	if !m.clock().Before(e.expiry) { // now >= expiry
		delete(m.items, key)
		m.order.Remove(el)
		return nil, false
	}
	return e.value, true
}

func (m *TTLMap) Len() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.items)
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd shim-go && go test -run TestTTLMap ./...`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shim-go/signedmap.go shim-go/signedmap_test.go
git commit -m "feat: port TTLMap to Go"
```

---

### Task 3: LruDiskCache

**Files:**
- Create: `shim-go/lru.go`, `shim-go/lru_test.go`

**Interfaces:**
- Produces: `type lruCache`; `newLRU(maxBytes int64, del func(key string)) *lruCache`; methods `Load(entries []lruSeed)` (seeds oldest-first by mtime then evicts), `Touch(key string)`, `Record(key string, size int64)`, `Forget(key string)`, `TotalBytes() int64`. `type lruSeed struct { Key string; Size int64; MTime time.Time }`. `maxBytes <= 0` disables eviction. `del` is called for each evicted key.

- [ ] **Step 1: Write the failing test**

```go
package main

import (
	"testing"
	"time"
)

func TestLRUEvictsLeastRecentlyUsed(t *testing.T) {
	var deleted []string
	c := newLRU(10, func(k string) { deleted = append(deleted, k) })
	c.Record("a", 6)
	c.Record("b", 3)
	c.Touch("a")        // a is now MRU; b is LRU
	c.Record("c", 4)    // total 13 > 10 -> evict LRU (b)
	if len(deleted) != 1 || deleted[0] != "b" {
		t.Fatalf("deleted = %v, want [b]", deleted)
	}
	if c.TotalBytes() != 10 {
		t.Fatalf("total = %d, want 10", c.TotalBytes())
	}
}

func TestLRUUnboundedWhenCapZero(t *testing.T) {
	c := newLRU(0, func(string) { t.Fatal("should never evict when cap=0") })
	c.Record("a", 1<<30)
	c.Record("b", 1<<30)
}

func TestLRULoadSeedsOldestFirst(t *testing.T) {
	var deleted []string
	c := newLRU(10, func(k string) { deleted = append(deleted, k) })
	base := time.Unix(1000, 0)
	c.Load([]lruSeed{
		{"new", 6, base.Add(2 * time.Second)},
		{"old", 6, base}, // older -> evicted first when over cap
	})
	if len(deleted) != 1 || deleted[0] != "old" {
		t.Fatalf("deleted = %v, want [old]", deleted)
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd shim-go && go test -run TestLRU ./...`
Expected: FAIL (undefined: newLRU).

- [ ] **Step 3: Implement `lru.go`**

```go
package main

import (
	"container/list"
	"sort"
	"sync"
	"time"
)

// lruCache is a byte-capped LRU accountant. It owns accounting, not files;
// eviction calls the injected delete callback. Port of m1/eviction.py.
type lruCache struct {
	mu    sync.Mutex
	max   int64
	del   func(string)
	order *list.List // front = LRU, back = MRU
	items map[string]*list.Element
	total int64
}

type lruEntry struct {
	key  string
	size int64
}

type lruSeed struct {
	Key   string
	Size  int64
	MTime time.Time
}

func newLRU(maxBytes int64, del func(string)) *lruCache {
	return &lruCache{max: maxBytes, del: del, order: list.New(), items: map[string]*list.Element{}}
}

func (c *lruCache) TotalBytes() int64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.total
}

func (c *lruCache) Load(entries []lruSeed) {
	c.mu.Lock()
	defer c.mu.Unlock()
	sort.Slice(entries, func(i, j int) bool { return entries[i].MTime.Before(entries[j].MTime) })
	for _, e := range entries {
		if _, ok := c.items[e.Key]; !ok {
			c.items[e.Key] = c.order.PushBack(&lruEntry{e.Key, e.Size})
			c.total += e.Size
		}
	}
	c.evictLocked()
}

func (c *lruCache) Touch(key string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if el, ok := c.items[key]; ok {
		c.order.MoveToBack(el)
	}
}

func (c *lruCache) Record(key string, size int64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if el, ok := c.items[key]; ok {
		c.total -= el.Value.(*lruEntry).size
		el.Value.(*lruEntry).size = size
		c.order.MoveToBack(el)
	} else {
		c.items[key] = c.order.PushBack(&lruEntry{key, size})
	}
	c.total += size
	c.evictLocked()
}

func (c *lruCache) Forget(key string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if el, ok := c.items[key]; ok {
		c.total -= el.Value.(*lruEntry).size
		c.order.Remove(el)
		delete(c.items, key)
	}
}

func (c *lruCache) evictLocked() {
	if c.max <= 0 {
		return
	}
	for c.total > c.max && c.order.Len() > 0 {
		front := c.order.Front()
		e := front.Value.(*lruEntry)
		c.total -= e.size
		c.order.Remove(front)
		delete(c.items, e.key)
		c.del(e.key)
	}
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd shim-go && go test -run TestLRU ./...`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shim-go/lru.go shim-go/lru_test.go
git commit -m "feat: port LruDiskCache to Go"
```

---

### Task 4: ManifestCache (range-keyed)

**Files:**
- Create: `shim-go/manifests.go`, `shim-go/manifests_test.go`

**Interfaces:**
- Produces: `type ManifestCache`; `newManifestCache(dir string, max int) *ManifestCache`; `(*ManifestCache).Get(version, fileID, rangeKey string) map[string]any` (nil if absent); `(*ManifestCache).Put(version, fileID, rangeKey string, manifest map[string]any)`. Filename: `{version}_{fileID}_{sanitized}.json` where sanitized = regexp `[^0-9A-Za-z_-]` → `_`, or `full` if rangeKey empty. Byte-identical to `m1/manifest_cache.py`.

- [ ] **Step 1: Write the failing test**

```go
package main

import (
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
```

Add imports `"os"` to the test file.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd shim-go && go test -run TestManifest ./...`
Expected: FAIL (undefined: newManifestCache).

- [ ] **Step 3: Implement `manifests.go`**

```go
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd shim-go && go test -run TestManifest ./...`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shim-go/manifests.go shim-go/manifests_test.go
git commit -m "feat: port range-keyed ManifestCache to Go"
```

---

### Task 5: Shared helpers (cache path, range parse, header hygiene)

**Files:**
- Create: `shim-go/util.go`, `shim-go/util_test.go`

**Interfaces:**
- Produces:
  - `cachePath(dir, hash, byteRange string) string` — `filepath.Join(dir, hex(sha256(hash+":"+byteRange)))`.
  - `parseRange(byteRange string) (lo int64, endExclusive int64, err error)` — `"bytes=lo-hi"` → `(lo, hi+1, nil)`; error otherwise.
  - `cleanHeaders(dst http.Header, src http.Header)` — copies all but hop-by-hop + `content-length` + `content-encoding`.
  - `type httpError struct { code int; msg string }` with `Error() string`.
  - `writeJSON(w http.ResponseWriter, status int, v any)`; `httpErrWrite(w http.ResponseWriter, code int, msg string)`.

- [ ] **Step 1: Write the failing test**

```go
package main

import (
	"net/http"
	"testing"
)

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
```

Add a tiny helper in the test file: `func filepathBase(p string) string { return filepath.Base(p) }` with `"path/filepath"` imported.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd shim-go && go test -run 'TestCachePath|TestParseRange|TestCleanHeaders' ./...`
Expected: FAIL (undefined: cachePath).

- [ ] **Step 3: Implement `util.go`**

```go
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"path/filepath"
	"strconv"
	"strings"
)

func cachePath(dir, hash, byteRange string) string {
	sum := sha256.Sum256([]byte(hash + ":" + byteRange))
	return filepath.Join(dir, hex.EncodeToString(sum[:]))
}

func parseRange(byteRange string) (int64, int64, error) {
	s := strings.TrimPrefix(byteRange, "bytes=")
	lo, hi, ok := strings.Cut(s, "-")
	if !ok {
		return 0, 0, fmt.Errorf("unsupported Range: %q", byteRange)
	}
	l, err1 := strconv.ParseInt(lo, 10, 64)
	h, err2 := strconv.ParseInt(hi, 10, 64)
	if err1 != nil || err2 != nil {
		return 0, 0, fmt.Errorf("unsupported Range: %q", byteRange)
	}
	return l, h + 1, nil
}

// hop-by-hop (RFC 7230 §6.1) plus content-length/-encoding, matching m1/shim.py.
var hopByHop = map[string]bool{
	"connection": true, "keep-alive": true, "proxy-authenticate": true,
	"proxy-authorization": true, "te": true, "trailers": true,
	"transfer-encoding": true, "upgrade": true, "content-encoding": true,
	"content-length": true,
}

func cleanHeaders(dst, src http.Header) {
	for k, vs := range src {
		if hopByHop[strings.ToLower(k)] {
			continue
		}
		for _, v := range vs {
			dst.Add(k, v)
		}
	}
}

type httpError struct {
	code int
	msg  string
}

func (e *httpError) Error() string { return e.msg }

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func httpErrWrite(w http.ResponseWriter, code int, msg string) {
	http.Error(w, msg, code)
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd shim-go && go test -run 'TestCachePath|TestParseRange|TestCleanHeaders' ./...`
Expected: PASS (interop hash matches).

- [ ] **Step 5: Commit**

```bash
git add shim-go/util.go shim-go/util_test.go
git commit -m "feat: cache-path (python-interop), range parse, header hygiene"
```

---

### Task 6: Server type, httpDoer, rememberSigned, fetchAuthorized

**Files:**
- Create: `shim-go/server.go`, `shim-go/xorb.go`, `shim-go/xorb_test.go`

**Interfaces:**
- Consumes: `Metrics`, `TTLMap`, `lruCache`, `ManifestCache`, `httpError`, `singleflight.Group`.
- Produces:
  - `type httpDoer interface { Do(*http.Request) (*http.Response, error) }`.
  - `type Server struct` with fields: `hfUpstream, casUpstream, publicBase, cacheDir string`, `signed *TTLMap`, `manifests *ManifestCache`, `metrics *Metrics`, `lru *lruCache`, `doer httpDoer`, `sf singleflight.Group`, `signedCandidates int`.
  - `(*Server).rememberSigned(hash, url string)` — prepend/dedupe/cap at `signedCandidates` (default 8), most-recent-first.
  - `(*Server).fetchAuthorized(hash, byteRange string) (*http.Response, error)` — iterate candidates, return first 200/206; `&httpError{409,...}` if no candidates; `&httpError{502,...}` if none authorize.

- [ ] **Step 1: Write the failing test**

```go
package main

import (
	"io"
	"net/http"
	"strings"
	"testing"
)

type fakeDoer struct {
	good  string
	calls []string
}

func (f *fakeDoer) Do(req *http.Request) (*http.Response, error) {
	f.calls = append(f.calls, req.URL.String())
	code := 403
	body := ""
	if req.URL.String() == f.good {
		code, body = 206, "BYTES"
	}
	return &http.Response{
		StatusCode: code,
		Body:       io.NopCloser(strings.NewReader(body)),
		Header:     http.Header{},
	}, nil
}

func newTestServer(doer httpDoer) *Server {
	return &Server{
		signed:           NewTTLMap(3600e9, 100000, nil),
		metrics:          NewMetrics(),
		doer:             doer,
		signedCandidates: 8,
	}
}

func TestRememberSignedOrderDedupCap(t *testing.T) {
	s := newTestServer(nil)
	for _, u := range []string{"u1", "u2", "u3"} {
		s.rememberSigned("h", u)
	}
	if got, _ := s.signed.Get("h"); !equalSlice(got, []string{"u3", "u2", "u1"}) {
		t.Fatalf("order = %v", got)
	}
	s.rememberSigned("h", "u2") // re-seen -> front, no dup
	if got, _ := s.signed.Get("h"); !equalSlice(got, []string{"u2", "u3", "u1"}) {
		t.Fatalf("after re-seen = %v", got)
	}
	for i := 0; i < 20; i++ {
		s.rememberSigned("h", string(rune('A'+i)))
	}
	if got, _ := s.signed.Get("h"); len(got) != s.signedCandidates {
		t.Fatalf("cap = %d, want %d", len(got), s.signedCandidates)
	}
}

func TestFetchAuthorizedTriesCandidates(t *testing.T) {
	f := &fakeDoer{good: "right"}
	s := newTestServer(f)
	s.signed.Set("h", []string{"wrong1", "wrong2", "right", "wrong3"})
	resp, err := s.fetchAuthorized("h", "bytes=0-4")
	if err != nil || resp.StatusCode != 206 {
		t.Fatalf("got (%v, %v)", resp, err)
	}
	if !equalSlice(f.calls, []string{"wrong1", "wrong2", "right"}) {
		t.Fatalf("calls = %v (should stop at first authorized)", f.calls)
	}
}

func TestFetchAuthorized502WhenNone(t *testing.T) {
	s := newTestServer(&fakeDoer{good: "nope"})
	s.signed.Set("h", []string{"a", "b"})
	_, err := s.fetchAuthorized("h", "bytes=0-4")
	he, ok := err.(*httpError)
	if !ok || he.code != 502 {
		t.Fatalf("err = %v, want 502 httpError", err)
	}
}

func TestFetchAuthorized409Unknown(t *testing.T) {
	s := newTestServer(&fakeDoer{})
	_, err := s.fetchAuthorized("never", "bytes=0-4")
	he, ok := err.(*httpError)
	if !ok || he.code != 409 {
		t.Fatalf("err = %v, want 409 httpError", err)
	}
}

func equalSlice(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd shim-go && go test -run 'TestRememberSigned|TestFetchAuthorized' ./...`
Expected: FAIL (undefined: Server / rememberSigned / fetchAuthorized).

- [ ] **Step 3: Implement `server.go`**

```go
package main

import "golang.org/x/sync/singleflight"

type httpDoer interface {
	Do(req *httpRequest) (*httpResponse, error)
}
```

Correction — use the real stdlib types. Write `server.go` as:

```go
package main

import (
	"net/http"

	"golang.org/x/sync/singleflight"
)

type httpDoer interface {
	Do(*http.Request) (*http.Response, error)
}

// Server plays all three roles (hub proxy / CAS relay / xorb store).
type Server struct {
	hfUpstream       string
	casUpstream      string
	publicBase       string
	cacheDir         string
	signed           *TTLMap
	manifests        *ManifestCache
	metrics          *Metrics
	lru              *lruCache
	doer             httpDoer
	sf               singleflight.Group
	signedCandidates int
}
```

- [ ] **Step 4: Implement `xorb.go` (rememberSigned + fetchAuthorized only for now)**

```go
package main

import (
	"fmt"
	"net/http"
)

// rememberSigned records a signed url for a xorb, most-recent-first, deduped
// and capped. A boundary-spanning xorb has several window-scoped urls.
func (s *Server) rememberSigned(hash, url string) {
	prev, _ := s.signed.Get(hash)
	next := make([]string, 0, len(prev)+1)
	next = append(next, url)
	for _, u := range prev {
		if u != url {
			next = append(next, u)
		}
	}
	if len(next) > s.signedCandidates {
		next = next[:s.signedCandidates]
	}
	s.signed.Set(hash, next)
}

// fetchAuthorized replays byteRange against each candidate url until one is
// authorized (wrong-window urls 403). 409 if unknown xorb, 502 if none work.
func (s *Server) fetchAuthorized(hash, byteRange string) (*http.Response, error) {
	cands, ok := s.signed.Get(hash)
	if !ok || len(cands) == 0 {
		return nil, &httpError{409, "unknown xorb; request reconstruction first"}
	}
	last := 0
	for _, signed := range cands {
		req, err := http.NewRequest(http.MethodGet, signed, nil)
		if err != nil {
			continue
		}
		req.Header.Set("Range", byteRange)
		req.Header.Set("Accept-Encoding", "identity")
		resp, err := s.doer.Do(req)
		if err != nil {
			continue
		}
		if resp.StatusCode == http.StatusOK || resp.StatusCode == http.StatusPartialContent {
			return resp, nil
		}
		last = resp.StatusCode
		resp.Body.Close()
	}
	return nil, &httpError{502, fmt.Sprintf("no signed url authorizes %s (last %d)", byteRange, last)}
}
```

Note: delete the erroneous first `server.go` sketch; use the corrected block.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd shim-go && go test -run 'TestRememberSigned|TestFetchAuthorized' ./...`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add shim-go/server.go shim-go/xorb.go shim-go/xorb_test.go
git commit -m "feat: Server, httpDoer, rememberSigned, fetchAuthorized"
```

---

### Task 7: getXorb handler (Tier 1 + singleflight)

**Files:**
- Modify: `shim-go/xorb.go`
- Test: `shim-go/xorb_handler_test.go`

**Interfaces:**
- Consumes: `Server`, `fetchAuthorized`, `cachePath`, `lruCache`, `Metrics`, `singleflight.Group`.
- Produces: `(*Server).getXorb(w http.ResponseWriter, r *http.Request)` — `GET /xorb/xorbs/default/{xorb_hash}` with `Range`. HIT serves from disk (`X-Cache: HIT`); MISS fetches via singleflight keyed by the cache filename, writes to disk, records LRU, serves (`X-Cache: MISS`). Missing `Range` → 400. Counts `served_bytes` per caller; `misses`+`wan_bytes` once per real fetch.

- [ ] **Step 1: Write the failing test**

```go
package main

import (
	"io"
	"net/http"
	"net/http/httptest"
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
		lru:              newLRU(0, func(string) {}),
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd shim-go && go test -run TestGetXorb ./...`
Expected: FAIL (undefined: getXorb).

- [ ] **Step 3: Implement `getXorb` in `xorb.go`**

```go
type xorbResult struct {
	body        []byte
	contentRange string
}

func (s *Server) getXorb(w http.ResponseWriter, r *http.Request) {
	hash := r.PathValue("xorb_hash")
	byteRange := r.Header.Get("Range")
	if byteRange == "" {
		httpErrWrite(w, http.StatusBadRequest, "Range header required (xorbs are range-addressed)")
		return
	}
	path := cachePath(s.cacheDir, hash, byteRange)
	name := filepath.Base(path)

	if body, err := os.ReadFile(path); err == nil {
		s.lru.Touch(name)
		s.metrics.Incr("hits", 1)
		s.metrics.Incr("served_bytes", int64(len(body)))
		writeXorbBytes(w, body, "HIT", "")
		return
	}

	v, err, _ := s.sf.Do(name, func() (any, error) {
		resp, ferr := s.fetchAuthorized(hash, byteRange)
		if ferr != nil {
			return nil, ferr
		}
		defer resp.Body.Close()
		body, rerr := io.ReadAll(resp.Body)
		if rerr != nil {
			return nil, &httpError{502, "read upstream body: " + rerr.Error()}
		}
		if werr := os.WriteFile(path, body, 0o644); werr != nil {
			return nil, &httpError{500, "cache write: " + werr.Error()}
		}
		s.lru.Record(name, int64(len(body)))
		s.metrics.Incr("misses", 1)
		s.metrics.Incr("wan_bytes", int64(len(body)))
		return xorbResult{body: body, contentRange: resp.Header.Get("Content-Range")}, nil
	})
	if err != nil {
		if he, ok := err.(*httpError); ok {
			httpErrWrite(w, he.code, he.msg)
			return
		}
		httpErrWrite(w, http.StatusBadGateway, err.Error())
		return
	}
	res := v.(xorbResult)
	s.metrics.Incr("served_bytes", int64(len(res.body))) // per-caller (singleflight losers too)
	writeXorbBytes(w, res.body, "MISS", res.contentRange)
}

func writeXorbBytes(w http.ResponseWriter, body []byte, cache, contentRange string) {
	h := w.Header()
	h.Set("X-Cache", cache)
	h.Set("Accept-Ranges", "bytes")
	h.Set("Content-Type", "application/octet-stream")
	if contentRange != "" {
		h.Set("Content-Range", contentRange)
	}
	w.WriteHeader(http.StatusPartialContent)
	_, _ = w.Write(body)
}
```

Add imports to `xorb.go`: `"io"`, `"os"`, `"path/filepath"` (keep `"fmt"`, `"net/http"`).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd shim-go && go test -run TestGetXorb ./...`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shim-go/xorb.go shim-go/xorb_handler_test.go
git commit -m "feat: getXorb Tier 1 handler with singleflight"
```

---

### Task 8: reconstruction handler (CAS relay)

**Files:**
- Create: `shim-go/reconstruction.go`, `shim-go/reconstruction_test.go`

**Interfaces:**
- Consumes: `Server`, `rememberSigned`, `ManifestCache`, `Metrics`, `cleanHeaders`, `writeJSON`.
- Produces:
  - `(*Server).rewriteManifest(m map[string]any)` — for each `xorbs[hash]` entry: `rememberSigned(hash, entry["url"])` then set `entry["url"] = publicBase + "/xorb/xorbs/default/" + hash`.
  - `(*Server).reconstruction(w http.ResponseWriter, r *http.Request)` — `GET /cas/{version}/reconstructions/{file_id}`. Transport error → cached manifest for this exact range (else 502). Reachable non-200 → propagate verbatim. 200 → rewrite, cache keyed by range, return.

- [ ] **Step 1: Write the failing test**

```go
package main

import (
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

type scriptDoer struct {
	resp *http.Response
	err  error
}

func (d *scriptDoer) Do(*http.Request) (*http.Response, error) { return d.resp, d.err }

func jsonResp(code int, body string) *http.Response {
	return &http.Response{StatusCode: code, Body: io.NopCloser(strings.NewReader(body)), Header: http.Header{}}
}

func newReconServer(t *testing.T, doer httpDoer) *Server {
	dir := t.TempDir()
	return &Server{
		casUpstream:      "https://cas.example",
		publicBase:       "http://shim",
		manifests:        newManifestCache(dir+"/manifests", 0),
		metrics:          NewMetrics(),
		signed:           NewTTLMap(3600e9, 100000, nil),
		doer:             doer,
		signedCandidates: 8,
	}
}

func doRecon(s *Server, version, fileID, rng string) *httptest.ResponseRecorder {
	req := httptest.NewRequest("GET", "/cas/"+version+"/reconstructions/"+fileID, nil)
	req.SetPathValue("version", version)
	req.SetPathValue("file_id", fileID)
	if rng != "" {
		req.Header.Set("Range", rng)
	}
	rec := httptest.NewRecorder()
	s.reconstruction(rec, req)
	return rec
}

func TestReconRewritesAndCaches(t *testing.T) {
	body := `{"xorbs":{"deadbeef":[{"url":"https://cdn/signed?sig=1","ranges":[]}]}}`
	s := newReconServer(t, &scriptDoer{resp: jsonResp(200, body)})
	rec := doRecon(s, "v2", "fileZ", "bytes=0-99")
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), "http://shim/xorb/xorbs/default/deadbeef") {
		t.Fatalf("rewrite failed: %s", rec.Body.String())
	}
	if got, _ := s.signed.Get("deadbeef"); len(got) != 1 || got[0] != "https://cdn/signed?sig=1" {
		t.Fatalf("signed not remembered: %v", got)
	}
	if s.manifests.Get("v2", "fileZ", "bytes=0-99") == nil {
		t.Fatal("manifest not cached for this range")
	}
}

func TestReconNon200PropagatesVerbatim(t *testing.T) {
	s := newReconServer(t, &scriptDoer{resp: jsonResp(416, "range not satisfiable")})
	rec := doRecon(s, "v2", "fileZ", "bytes=0-99")
	if rec.Code != 416 {
		t.Fatalf("code = %d, want 416 propagated", rec.Code)
	}
}

func TestReconTransportErrorServesCachedRange(t *testing.T) {
	s := newReconServer(t, &scriptDoer{err: errors.New("dial tcp: connection refused")})
	s.manifests.Put("v2", "fileZ", "bytes=0-99", map[string]any{"cached": true})
	rec := doRecon(s, "v2", "fileZ", "bytes=0-99")
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), "cached") {
		t.Fatalf("offline serve failed: code=%d body=%s", rec.Code, rec.Body.String())
	}
	// no cached manifest for a DIFFERENT range -> 502
	rec2 := doRecon(s, "v2", "fileZ", "bytes=500-599")
	if rec2.Code != 502 {
		t.Fatalf("uncached range offline = %d, want 502", rec2.Code)
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd shim-go && go test -run TestRecon ./...`
Expected: FAIL (undefined: reconstruction).

- [ ] **Step 3: Implement `reconstruction.go`**

```go
package main

import (
	"encoding/json"
	"io"
	"net/http"
)

func (s *Server) rewriteManifest(m map[string]any) {
	xorbs, ok := m["xorbs"].(map[string]any)
	if !ok {
		return
	}
	for hash, entriesAny := range xorbs {
		entries, ok := entriesAny.([]any)
		if !ok {
			continue
		}
		for _, eAny := range entries {
			e, ok := eAny.(map[string]any)
			if !ok {
				continue
			}
			if u, ok := e["url"].(string); ok {
				s.rememberSigned(hash, u)
				e["url"] = s.publicBase + "/xorb/xorbs/default/" + hash
			}
		}
	}
}

func (s *Server) reconstruction(w http.ResponseWriter, r *http.Request) {
	version := r.PathValue("version")
	fileID := r.PathValue("file_id")
	rangeKey := r.Header.Get("Range")

	url := s.casUpstream + "/" + version + "/reconstructions/" + fileID
	req, _ := http.NewRequest(http.MethodGet, url, nil)
	req.URL.RawQuery = r.URL.RawQuery
	if a := r.Header.Get("Authorization"); a != "" {
		req.Header.Set("Authorization", a)
	}
	if rangeKey != "" {
		req.Header.Set("Range", rangeKey)
	}
	req.Header.Set("Accept-Encoding", "identity")

	resp, err := s.doer.Do(req)
	if err != nil {
		// TRANSPORT failure — serve cached manifest for THIS exact range only.
		if cached := s.manifests.Get(version, fileID, rangeKey); cached != nil {
			s.metrics.Incr("reconstructions_offline", 1)
			writeJSON(w, http.StatusOK, cached)
			return
		}
		httpErrWrite(w, http.StatusBadGateway, "reconstruction upstream unreachable and not cached")
		return
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)

	// Reachable but non-200 (e.g. 416): propagate verbatim so the client retries.
	if resp.StatusCode != http.StatusOK {
		cleanHeaders(w.Header(), resp.Header)
		w.WriteHeader(resp.StatusCode)
		_, _ = w.Write(body)
		return
	}

	var manifest map[string]any
	if err := json.Unmarshal(body, &manifest); err != nil {
		httpErrWrite(w, http.StatusBadGateway, "malformed reconstruction manifest")
		return
	}
	s.rewriteManifest(manifest)
	s.manifests.Put(version, fileID, rangeKey, manifest)
	s.metrics.Incr("reconstructions", 1)
	writeJSON(w, http.StatusOK, manifest)
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd shim-go && go test -run TestRecon ./...`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shim-go/reconstruction.go shim-go/reconstruction_test.go
git commit -m "feat: CAS reconstruction relay with offline serve + verbatim non-200"
```

---

### Task 9: hub proxy handler (token rewrite + passthrough)

**Files:**
- Create: `shim-go/proxy.go`, `shim-go/proxy_test.go`

**Interfaces:**
- Consumes: `Server`, `cleanHeaders`, `writeJSON`, `Metrics`.
- Produces:
  - `(*Server).proxyHub(r *http.Request, fullPath string) (*http.Response, []byte, error)` — issues the upstream request to `hfUpstream/fullPath`, forwards method/query/body and cleaned headers, sets `Host` to upstream host and `Accept-Encoding: identity`, returns the response and its already-read body.
  - `(*Server).hub(w http.ResponseWriter, r *http.Request)` — `GET|HEAD /`. If `fullPath` contains `/xet-read-token/` and upstream is 200: parse body, set `casUrl = publicBase + "/cas"`, return JSON. Else passthrough (cleaned headers, verbatim status/body).

- [ ] **Step 1: Write the failing test**

```go
package main

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// urlCapturingDoer returns a fixed response and records the outbound request.
type urlCapturingDoer struct {
	resp *http.Response
	got  *http.Request
}

func (d *urlCapturingDoer) Do(req *http.Request) (*http.Response, error) {
	d.got = req
	return d.resp, nil
}

func newHubServer(doer httpDoer) *Server {
	return &Server{
		hfUpstream: "https://huggingface.co",
		publicBase: "http://shim",
		metrics:    NewMetrics(),
		doer:       doer,
	}
}

func doHub(s *Server, path string) *httptest.ResponseRecorder {
	req := httptest.NewRequest("GET", path, nil)
	rec := httptest.NewRecorder()
	s.hub(rec, req)
	return rec
}

func TestHubRewritesToken(t *testing.T) {
	resp := jsonResp(200, `{"casUrl":"https://cas-server.xethub.hf.co","accessToken":"tok"}`)
	s := newHubServer(&urlCapturingDoer{resp: resp})
	rec := doHub(s, "/api/models/foo/xet-read-token/abc")
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), `"casUrl":"http://shim/cas"`) {
		t.Fatalf("token not rewritten: %s", rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), `"accessToken":"tok"`) {
		t.Fatal("accessToken should be preserved")
	}
}

func TestHubPassesThrough302(t *testing.T) {
	h := http.Header{}
	h.Set("Location", "https://cdn/xorb")
	h.Set("X-Xet-Hash", "abc123")
	resp := &http.Response{StatusCode: 302, Body: io.NopCloser(strings.NewReader("")), Header: h}
	s := newHubServer(&urlCapturingDoer{resp: resp})
	rec := doHub(s, "/models/foo/resolve/main/model.safetensors")
	if rec.Code != 302 {
		t.Fatalf("code = %d, want 302 passthrough", rec.Code)
	}
	if rec.Header().Get("X-Xet-Hash") != "abc123" {
		t.Fatal("X-Xet-Hash must pass through")
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd shim-go && go test -run TestHub ./...`
Expected: FAIL (undefined: hub).

- [ ] **Step 3: Implement `proxy.go`**

```go
package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"strings"
)

func (s *Server) proxyHub(r *http.Request, fullPath string) (*http.Response, []byte, error) {
	var body io.Reader
	if r.Body != nil {
		b, _ := io.ReadAll(r.Body)
		body = bytes.NewReader(b)
	}
	url := s.hfUpstream + "/" + fullPath
	req, err := http.NewRequest(r.Method, url, body)
	if err != nil {
		return nil, nil, err
	}
	cleanHeaders(req.Header, r.Header)
	req.URL.RawQuery = r.URL.RawQuery
	req.Host = strings.TrimPrefix(s.hfUpstream, "https://")
	req.Host = strings.TrimPrefix(req.Host, "http://")
	req.Header.Set("Accept-Encoding", "identity")

	resp, err := s.doer.Do(req)
	if err != nil {
		return nil, nil, err
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)
	return resp, respBody, nil
}

func (s *Server) hub(w http.ResponseWriter, r *http.Request) {
	fullPath := strings.TrimPrefix(r.URL.Path, "/")
	upstream, body, err := s.proxyHub(r, fullPath)
	if err != nil {
		httpErrWrite(w, http.StatusBadGateway, "hub upstream error: "+err.Error())
		return
	}

	if strings.Contains(fullPath, "/xet-read-token/") && upstream.StatusCode == http.StatusOK {
		var tok map[string]any
		if json.Unmarshal(body, &tok) == nil {
			tok["casUrl"] = s.publicBase + "/cas"
			s.metrics.Incr("token_rewrites", 1)
			writeJSON(w, http.StatusOK, tok)
			return
		}
	}

	cleanHeaders(w.Header(), upstream.Header)
	w.WriteHeader(upstream.StatusCode)
	_, _ = w.Write(body)
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd shim-go && go test -run TestHub ./...`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shim-go/proxy.go shim-go/proxy_test.go
git commit -m "feat: hub proxy with xet-read-token rewrite + passthrough"
```

---

### Task 10: main.go — config, wiring, routes, LRU seed, server

**Files:**
- Modify: `shim-go/main.go` (replace the stub)

**Interfaces:**
- Consumes: everything above.
- Produces: `func main()` that reads env, builds a `Server` with a real `*http.Client` doer, seeds the Tier 1 LRU from disk, registers routes on a `http.ServeMux`, and serves on `:PORT`. Also `func newServer(...) *Server` and `func seedLRU(s *Server)` for clarity. Route table: `GET /healthz`, `GET /metrics`, `GET /xorb/xorbs/default/{xorb_hash}`, `GET /cas/{version}/reconstructions/{file_id}`, `GET /`, `HEAD /`.

- [ ] **Step 1: Implement `main.go`**

```go
package main

import (
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
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
	cacheDir := env("CACHE_DIR", "./xorb-cache")
	if err := os.MkdirAll(cacheDir, 0o755); err != nil {
		log.Fatalf("cache dir: %v", err)
	}
	maxGiB, _ := strconv.ParseFloat(env("XORB_CACHE_MAX_GIB", "0"), 64)
	maxBytes := int64(maxGiB * float64(1<<30))

	metrics := NewMetrics()
	lru := newLRU(maxBytes, func(name string) {
		_ = os.Remove(filepath.Join(cacheDir, name))
	})

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
	mux.HandleFunc("GET /xorb/xorbs/default/{xorb_hash}", s.getXorb)
	mux.HandleFunc("GET /cas/{version}/reconstructions/{file_id}", s.reconstruction)
	mux.HandleFunc("GET /", s.hub)
	mux.HandleFunc("HEAD /", s.hub)

	port := env("PORT", "8000")
	log.Printf("xet-dc-cache (go) on :%s  PUBLIC_BASE=%s  CACHE_DIR=%s  max=%.1fGiB",
		port, s.publicBase, cacheDir, maxGiB)
	if err := http.ListenAndServe("0.0.0.0:"+port, mux); err != nil {
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
		info, err := e.Info()
		if err != nil {
			continue
		}
		seeds = append(seeds, lruSeed{Key: e.Name(), Size: info.Size(), MTime: info.ModTime()})
	}
	s.lru.Load(seeds)
}
```

- [ ] **Step 2: Build and vet**

Run: `cd shim-go && go build ./... && go vet ./...`
Expected: exit 0, no output.

- [ ] **Step 3: Run the full unit suite**

Run: `cd shim-go && go test ./...`
Expected: PASS (all tasks 1–9).

- [ ] **Step 4: Smoke the binary by hand (healthz)**

```bash
cd shim-go && go build -o xetcache . && \
  CACHE_DIR=$(mktemp -d) PORT=8199 ./xetcache & sleep 1 && \
  curl -s http://127.0.0.1:8199/healthz && \
  curl -s http://127.0.0.1:8199/metrics && \
  kill %1
```
Expected: `{"status":"ok"}` then a JSON metrics blob with `wan_bytes_saved` and `hit_rate`.

- [ ] **Step 5: Commit**

```bash
git add shim-go/main.go
git commit -m "feat: wire config, routes, LRU seed, http server"
```

---

### Task 11: Makefile — build/run the Go binary

**Files:**
- Modify: `Makefile`

**Interfaces:**
- Produces: `make build` compiles `shim-go/xetcache`; `make start`/`run` exec the binary with the same env; `make test` runs Go + Python black-box tests. `stop`/`status`/`logs`/`metrics`/`clean-cache` unchanged.

- [ ] **Step 1: Add a `build` target and a `BIN` var**

Add near the top vars (after `MAX_GIB`):

```makefile
GO_DIR      := shim-go
BIN         := $(GO_DIR)/xetcache
```

Add the target and update `.PHONY`:

```makefile
.PHONY: help build start stop restart status logs metrics run test clean-cache

build:
	@cd $(GO_DIR) && go build -o xetcache . && echo "built $(BIN)"
```

- [ ] **Step 2: Point `start` at the binary**

Replace the `start:` recipe body's launch line. New `start`:

```makefile
start: stop build
	@mkdir -p "$(CACHE_DIR)"
	@PUBLIC_BASE=$(PUBLIC_BASE) CACHE_DIR=$(CACHE_DIR) \
		XORB_CACHE_MAX_GIB=$(MAX_GIB) PORT=$(PORT) \
		nohup ./$(BIN) > "$(LOG)" 2>&1 < /dev/null & \
		disown
	@echo "starting shim (PORT=$(PORT), PUBLIC_BASE=$(PUBLIC_BASE), CACHE_DIR=$(CACHE_DIR))"
	@for i in $$(seq 1 20); do \
		curl -s -m1 http://127.0.0.1:$(PORT)/healthz >/dev/null 2>&1 && break || sleep 0.5; \
	done
	@$(MAKE) --no-print-directory status
```

- [ ] **Step 3: Point `run` at the binary**

```makefile
run: stop build
	@mkdir -p "$(CACHE_DIR)"
	@echo "running in foreground on :$(PORT) — Ctrl-C to stop"
	@PUBLIC_BASE=$(PUBLIC_BASE) CACHE_DIR=$(CACHE_DIR) \
		XORB_CACHE_MAX_GIB=$(MAX_GIB) PORT=$(PORT) ./$(BIN)
```

- [ ] **Step 4: Update `test` to run Go + Python black-box**

```makefile
test:
	@cd $(GO_DIR) && go test ./...
	@cd $(M1) && uv run test_m3.py && uv run test_shim_recon.py && \
		uv run test_xorb_store.py && uv run test_eviction.py
```

- [ ] **Step 5: Verify**

Run: `make build && make test`
Expected: Go tests PASS; Python unit tests PASS (these don't need the server running).

- [ ] **Step 6: Commit**

```bash
git add Makefile
git commit -m "chore: Makefile builds and runs the Go binary"
```

---

### Task 12: Black-box acceptance — parity + warm-cache reuse

**Files:**
- None created (uses `m1/smoke_test.py`, `m1/integration_test.py`).
- Optional create: `docs/superpowers/plans/notes-go-acceptance.md` (record results).

**Interfaces:**
- Consumes: the built `shim-go/xetcache` binary; existing Python black-box tests (they target the shim via `HF_ENDPOINT`, language-agnostic).

- [ ] **Step 1: Start the Go binary on a scratch cache**

```bash
cd shim-go && go build -o xetcache .
CACHE_DIR=$(mktemp -d) PUBLIC_BASE=http://127.0.0.1:8000 PORT=8000 ./xetcache &
sleep 1 && curl -s http://127.0.0.1:8000/healthz
```
Expected: `{"status":"ok"}`.

- [ ] **Step 2: Run the smoke test against the Go binary**

The smoke test drives token→resolve→reconstruction→xorb-range and asserts MISS→HIT with bytes identical to a direct upstream fetch.

Run: `cd m1 && HF_ENDPOINT=http://127.0.0.1:8000 uv run smoke_test.py`
Expected: PASS — MISS then HIT, byte-identical.

- [ ] **Step 3: Run the real hf-xet integration test**

Run: `cd m1 && uv run integration_test.py` (it sets `HF_ENDPOINT` to the shim itself; if it targets a fixed port, ensure the Go binary is on it).
Expected: PASS — `hf_hub_download` byte-identical (sha256) to a direct pull; xorb cache populated; second pull a HIT.

- [ ] **Step 4: Prove warm-cache on-disk reuse**

Point the Go binary at the EXISTING production cache and re-request a range known to be cached by the Python shim; it must be a HIT with no WAN bytes.

```bash
kill %1 2>/dev/null; sleep 1
CACHE_DIR=$HOME/.cache/xet-dc-cache PORT=8000 ./xetcache & sleep 1
# re-run the smoke test; ranges cached by the python shim should HIT immediately
cd ../m1 && HF_ENDPOINT=http://127.0.0.1:8000 uv run smoke_test.py
curl -s http://127.0.0.1:8000/metrics
kill %1 2>/dev/null
```
Expected: cached ranges report `X-Cache: HIT`; `/metrics` shows `hit_rate > 0` and `wan_bytes_saved > 0` — confirming the Go binary reads the Python-written cache format.

- [ ] **Step 5: Record results and commit**

Write a short results note (pass/fail, sha256 match, hit_rate, bytes saved) to `docs/superpowers/plans/notes-go-acceptance.md`.

```bash
git add docs/superpowers/plans/notes-go-acceptance.md
git commit -m "test: go shim passes black-box parity + warm-cache reuse"
```

---

## Self-Review

**Spec coverage:**
- Architecture (A/B/C roles) → Tasks 7 (C), 8 (B), 9 (A). ✓
- Package layout → Tasks 0–10 create each file. ✓
- Config env vars + dropped `CACHE_TIER` → Task 10 (Global Constraints enumerate them). ✓
- TTLMap / LruDiskCache / ManifestCache / Metrics ports → Tasks 2, 3, 4, 1. ✓
- HTTP client (`ErrUseLastResponse`, `DisableCompression`, identity) → Task 10 + set on each outbound request in Tasks 6/8/9. ✓
- fetchAuthorized / rememberSigned bug fixes → Task 6. ✓
- reconstruction offline-serve + non-200 passthrough bug fix → Task 8. ✓
- On-disk compat (cache key + manifest filename) → Tasks 5 (interop test) + 4 (filename test) + 12 step 4 (live reuse). ✓
- singleflight → Task 7 (dedicated concurrency test). ✓
- Testing (unit + black-box acceptance) → Tasks 1–9 unit, Task 12 acceptance. ✓
- Makefile changes → Task 11. ✓
- Rollback (Python untouched) → Global Constraints + Task 11 keeps `m1/` runnable. ✓

**Placeholder scan:** No TBD/TODO; every code step has full code. Task 6 explicitly instructs discarding the intentionally-wrong first `server.go` sketch and using the corrected block (kept as a teaching correction, not a placeholder — the final code is complete).

**Type consistency:** `Server` fields and method names (`getXorb`, `reconstruction`, `hub`, `proxyHub`, `fetchAuthorized`, `rememberSigned`, `rewriteManifest`), `httpDoer.Do`, `httpError{code,msg}`, `lruSeed{Key,Size,MTime}`, `xorbResult{body,contentRange}`, helper names (`cachePath`, `parseRange`, `cleanHeaders`, `writeJSON`, `httpErrWrite`, `writeXorbBytes`, `trimSlash`, `seedLRU`) are used consistently across tasks.
