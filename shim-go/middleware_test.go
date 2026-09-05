package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestWithAuth(t *testing.T) {
	var reached bool
	next := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		reached = true
		w.WriteHeader(http.StatusOK)
	})
	h := withAuth("s3cret", next)

	cases := []struct {
		name       string
		path       string
		auth       string
		peer       bool
		wantStatus int
		wantReach  bool
	}{
		// Client data traffic is NOT gated by the bearer: a stock HF client can
		// only send its HF Authorization token (which the shim forwards
		// upstream), so it can never present the shim secret. Gating it would
		// 401 every real download. Client paths rely on network isolation.
		{"client data path, no token -> pass", "/xorb/xorbs/default/h", "", false, http.StatusOK, true},
		{"client data path, HF token -> pass", "/xorb/xorbs/default/h", "Bearer hf_abc", false, http.StatusOK, true},
		// Peer traffic (X-Xet-Peer:1) IS gated — peers set the bearer explicitly.
		{"peer request, no token -> 401", "/xorb/xorbs/default/h", "", true, http.StatusUnauthorized, false},
		{"peer request, wrong token -> 401", "/xorb/xorbs/default/h", "Bearer nope", true, http.StatusUnauthorized, false},
		{"peer request, right token -> pass", "/xorb/xorbs/default/h", "Bearer s3cret", true, http.StatusOK, true},
		{"healthz exempt", "/healthz", "", false, http.StatusOK, true},
		{"metrics exempt", "/metrics", "", false, http.StatusOK, true},
		{"metrics/prometheus exempt", "/metrics/prometheus", "", false, http.StatusOK, true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			reached = false
			rec := httptest.NewRecorder()
			req := httptest.NewRequest("GET", c.path, nil)
			if c.auth != "" {
				req.Header.Set("Authorization", c.auth)
			}
			if c.peer {
				req.Header.Set("X-Xet-Peer", "1")
			}
			h.ServeHTTP(rec, req)
			if rec.Code != c.wantStatus {
				t.Errorf("status = %d, want %d", rec.Code, c.wantStatus)
			}
			if reached != c.wantReach {
				t.Errorf("reached next = %v, want %v", reached, c.wantReach)
			}
		})
	}
}

func TestWithAuthEmptyTokenIsOpen(t *testing.T) {
	var reached bool
	next := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { reached = true })
	h := withAuth("", next)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/xorb/xorbs/default/h", nil))
	if !reached {
		t.Fatal("empty token must leave the shim open (no auth); next was not reached")
	}
}

func TestWithLoggingCapturesStatusAndPassesThrough(t *testing.T) {
	next := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("X-Cache", "MISS")
		w.WriteHeader(http.StatusPartialContent)
		_, _ = w.Write([]byte("hello"))
	})
	rec := httptest.NewRecorder()
	withLogging(next).ServeHTTP(rec, httptest.NewRequest("GET", "/x", nil))
	if rec.Code != http.StatusPartialContent {
		t.Fatalf("status = %d, want 206 (logging must not alter the response)", rec.Code)
	}
	if rec.Body.String() != "hello" {
		t.Fatalf("body = %q, want hello", rec.Body.String())
	}
}
