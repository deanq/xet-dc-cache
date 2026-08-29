package main

import (
	"encoding/json"
	"io"
	"net/http"
)

// rewriteManifest replaces every xorb's signed CDN url with a shim-relative
// url, remembering the original signed url so /xorb/xorbs/default/{hash} can
// fetch through it later without re-hitting CAS for a fresh signature.
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

// reconstruction relays GET /cas/{version}/reconstructions/{file_id}.
//
// Manifests are cached range-keyed because a manifest describes only the
// bytes CAS was asked to reconstruct — serving a manifest for a different
// range than requested is a coherence bug, not a cache hit.
func (s *Server) reconstruction(w http.ResponseWriter, r *http.Request) {
	version := r.PathValue("version")
	fileID := r.PathValue("file_id")
	rangeKey := r.Header.Get("Range")

	url := s.casUpstream + "/" + version + "/reconstructions/" + fileID
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		httpErrWrite(w, http.StatusBadGateway, "reconstruction request build failed")
		return
	}
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
		// Transport failure (upstream unreachable) — serve the cached manifest
		// for THIS exact range only. Any other range is a coherence bug.
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

	// Upstream was reached but returned non-200 (e.g. 416 range not
	// satisfiable): propagate verbatim. Do NOT substitute a cached manifest
	// here — that would silently serve the wrong range to the client.
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
