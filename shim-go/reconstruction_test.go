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
