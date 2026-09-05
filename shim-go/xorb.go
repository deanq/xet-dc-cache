package main

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
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
func (s *Server) fetchAuthorized(ctx context.Context, hash, byteRange string) (*http.Response, error) {
	cands, ok := s.signed.Get(hash)
	if !ok || len(cands) == 0 {
		return nil, &httpError{409, "unknown xorb; request reconstruction first"}
	}
	last := 0
	for _, signed := range cands {
		if ctx.Err() != nil {
			break
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, signed, nil)
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

// cdnGet is the CDN side of the hedged race: acquire a fetch slot, pull the
// authorized range, read the body. Best-effort — any failure (including ctx
// cancellation when the peer wins the race) returns (zero, n, false); the plain
// miss path keeps surfacing typed errors itself. The returned int64 is the
// number of body bytes actually read, INCLUDING on the cancelled/error path
// (io.ReadAll returns what it read so far) — the caller books it as the true
// wasted-transfer cost of a lost hedge, not the requested range size.
func (s *Server) cdnGet(ctx context.Context, hash, byteRange string) (xorbResult, int64, bool) {
	// Non-blocking: a speculative hedge must never take a slot a real cold miss
	// is queued for. If the pool is full, skip the hedge — the peer is still
	// racing, and a genuine miss falls through to the blocking plain path.
	if !s.tryAcquire() {
		return xorbResult{}, 0, false
	}
	defer s.release()
	resp, err := s.fetchAuthorized(ctx, hash, byteRange)
	if err != nil {
		return xorbResult{}, 0, false
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	n := int64(len(body))
	if err != nil {
		return xorbResult{}, n, false
	}
	return xorbResult{body: body, contentRange: resp.Header.Get("Content-Range")}, n, true
}

// isPeerRequest reports whether this request came from a sibling cache. Such
// requests are served hit-or-404: local disk or an immediate 404, never a CDN
// or onward-peer fetch (see the peering design — one-hop, no recursion).
func isPeerRequest(r *http.Request) bool {
	return r.Header.Get("X-Xet-Peer") == "1"
}

type xorbResult struct {
	body         []byte
	contentRange string
}

// getXorb serves GET /xorb/xorbs/default/{xorb_hash} with a Range header.
// Tier 1 disk cache: HIT reads from disk; MISS fetches upstream (collapsed
// via singleflight so concurrent cold requests for the same range share one
// fetch), writes to disk, and records the entry in the LRU.
func (s *Server) getXorb(w http.ResponseWriter, r *http.Request) {
	hash := r.PathValue("xorb_hash")
	byteRange := r.Header.Get("Range")
	if byteRange == "" {
		httpErrWrite(w, http.StatusBadRequest, "Range header required (xorbs are range-addressed)")
		return
	}
	path := cachePath(s.cacheDir, hash, byteRange)
	name := filepath.Base(path)

	// HEAD is an existence probe (used by peer discovery): stat, no body.
	if r.Method == http.MethodHead {
		if _, err := os.Stat(path); err == nil {
			s.lru.Touch(name)
			w.Header().Set("Accept-Ranges", "bytes")
			w.WriteHeader(http.StatusOK)
			return
		}
		w.WriteHeader(http.StatusNotFound)
		return
	}

	start := s.now()
	if body, err := os.ReadFile(path); err == nil {
		s.lru.Touch(name)
		s.metrics.Incr("hits", 1)
		s.metrics.Incr("served_bytes", int64(len(body)))
		s.metrics.Observe("hit", elapsedMs(start, s.now()))
		writeXorbBytes(w, body, "HIT", contentRangeForHit(byteRange, int64(len(body))))
		return
	}

	// Hit-or-404: a peer's GET never triggers a CDN or onward-peer fetch.
	if isPeerRequest(r) {
		w.WriteHeader(http.StatusNotFound)
		return
	}

	v, err, _ := s.sf.Do(name, func() (any, error) {
		fetchStart := s.now()
		// Tier 1.5: race a warm peer against a hedged CDN pull before the plain
		// CDN path. recordRaceWin books peer_bytes (peer won) or wan_bytes (CDN
		// won); misses is counted once here.
		if s.peers != nil {
			if res, src, ok := s.fetchFromPeer(r.Context(), hash, byteRange); ok {
				if werr := writeCacheFileAtomic(s.cacheDir, name, path, res.body); werr != nil {
					return nil, &httpError{500, "cache write: " + werr.Error()}
				}
				s.lru.Record(name, int64(len(res.body)))
				s.metrics.Incr("misses", 1)
				s.metrics.Observe(src.label(), elapsedMs(fetchStart, s.now()))
				return res, nil
			}
		}

		// Bound concurrent distinct upstream fetches (acquire only the winner;
		// singleflight losers wait on the flight, not a slot). Cancellable so a
		// disconnected client waiting for a slot doesn't hold one.
		if aerr := s.acquire(r.Context()); aerr != nil {
			return nil, aerr
		}
		defer s.release()
		resp, ferr := s.fetchAuthorized(r.Context(), hash, byteRange)
		if ferr != nil {
			return nil, ferr
		}
		defer resp.Body.Close()
		body, rerr := io.ReadAll(resp.Body)
		if rerr != nil {
			return nil, &httpError{502, "read upstream body: " + rerr.Error()}
		}
		if werr := writeCacheFileAtomic(s.cacheDir, name, path, body); werr != nil {
			return nil, &httpError{500, "cache write: " + werr.Error()}
		}
		s.lru.Record(name, int64(len(body)))
		s.metrics.Incr("misses", 1)
		s.metrics.Incr("wan_bytes", int64(len(body)))
		s.metrics.Observe("cdn", elapsedMs(fetchStart, s.now()))
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

// writeCacheFileAtomic writes body to path atomically by writing to a temp
// file in the same directory (so os.Rename is same-filesystem and atomic)
// and renaming it into place only after a fully successful write. On any
// failure the temp file is removed and no file (partial or otherwise) is
// left at path, so a concurrent/subsequent reader never observes a
// truncated cache entry as a false HIT.
func writeCacheFileAtomic(dir, name, path string, body []byte) error {
	tmp, err := os.CreateTemp(dir, "."+name+".tmp-*")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	cleanup := func() {
		tmp.Close()
		os.Remove(tmpPath)
	}

	if _, werr := tmp.Write(body); werr != nil {
		cleanup()
		return werr
	}
	if cerr := tmp.Close(); cerr != nil {
		os.Remove(tmpPath)
		return cerr
	}
	if rerr := os.Rename(tmpPath, path); rerr != nil {
		os.Remove(tmpPath)
		return rerr
	}
	return nil
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
