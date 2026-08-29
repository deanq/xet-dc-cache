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
		wantStatus int
		wantReach  bool
	}{
		{"data path, no token -> 401", "/xorb/xorbs/default/h", "", http.StatusUnauthorized, false},
		{"data path, wrong token -> 401", "/xorb/xorbs/default/h", "Bearer nope", http.StatusUnauthorized, false},
		{"data path, right token -> pass", "/xorb/xorbs/default/h", "Bearer s3cret", http.StatusOK, true},
		{"healthz exempt", "/healthz", "", http.StatusOK, true},
		{"metrics exempt", "/metrics", "", http.StatusOK, true},
		{"metrics/prometheus exempt", "/metrics/prometheus", "", http.StatusOK, true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			reached = false
			rec := httptest.NewRecorder()
			req := httptest.NewRequest("GET", c.path, nil)
			if c.auth != "" {
				req.Header.Set("Authorization", c.auth)
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
