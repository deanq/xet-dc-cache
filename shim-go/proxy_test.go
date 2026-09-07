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

// Current huggingface_hub reads the xet connection info from the token
// response's headers. The shim must forward them and rewrite X-Xet-Cas-Url to
// point at itself, while passing the access token and expiration through.
func TestHubRewritesTokenCasUrlHeader(t *testing.T) {
	resp := jsonResp(200, `{"casUrl":"https://cas-server.xethub.hf.co"}`)
	resp.Header.Set("X-Xet-Cas-Url", "https://cas-server.xethub.hf.co")
	resp.Header.Set("X-Xet-Access-Token", "tok123")
	resp.Header.Set("X-Xet-Token-Expiration", "1788021090")
	s := newHubServer(&urlCapturingDoer{resp: resp})
	rec := doHub(s, "/api/models/foo/xet-read-token/abc")
	if got := rec.Header().Get("X-Xet-Cas-Url"); got != "http://shim/cas" {
		t.Fatalf("X-Xet-Cas-Url = %q, want http://shim/cas", got)
	}
	if got := rec.Header().Get("X-Xet-Access-Token"); got != "tok123" {
		t.Fatalf("access token must pass through, got %q", got)
	}
	if got := rec.Header().Get("X-Xet-Token-Expiration"); got != "1788021090" {
		t.Fatalf("expiration must pass through, got %q", got)
	}
}

// A HEAD to `resolve` for a non-LFS file carries its size ONLY in
// Content-Length (LFS files use X-Linked-Size). The proxy must preserve it, or
// huggingface_hub's metadata HEAD fails with "no Content-Length" and every full
// snapshot_download through the shim breaks (finding #12).
func TestHubPreservesContentLengthOnHeadResolve(t *testing.T) {
	h := http.Header{}
	h.Set("Content-Length", "861")
	h.Set("Location", "/api/resolve-cache/models/foo/abc/config.json")
	resp := &http.Response{StatusCode: 307, Body: io.NopCloser(strings.NewReader("")), Header: h}
	s := newHubServer(&urlCapturingDoer{resp: resp})
	req := httptest.NewRequest("HEAD", "/models/foo/resolve/main/config.json", nil)
	rec := httptest.NewRecorder()
	s.hub(rec, req)
	if got := rec.Header().Get("Content-Length"); got != "861" {
		t.Fatalf("Content-Length = %q, want 861 (non-LFS file size must survive the proxy)", got)
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
