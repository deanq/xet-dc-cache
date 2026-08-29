package main

import (
	"crypto/subtle"
	"log/slog"
	"net/http"
	"strings"
	"time"
)

// statusRecorder captures the status code and byte count of a response so the
// logging middleware can report them. Defaults to 200 (net/http's implicit
// status when a handler writes a body without calling WriteHeader).
type statusRecorder struct {
	http.ResponseWriter
	status int
	bytes  int64
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

func (r *statusRecorder) Write(b []byte) (int, error) {
	n, err := r.ResponseWriter.Write(b)
	r.bytes += int64(n)
	return n, err
}

// withLogging emits one structured (slog) line per request at completion:
// method, path, status, the X-Cache disposition (HIT/MISS), bytes, duration.
func withLogging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		slog.Info("request",
			"method", r.Method,
			"path", r.URL.Path,
			"status", rec.status,
			"x_cache", rec.Header().Get("X-Cache"),
			"bytes", rec.bytes,
			"dur_ms", time.Since(start).Milliseconds(),
			"peer", r.Header.Get("X-Xet-Peer") == "1",
		)
	})
}

// isOperationalPath reports whether p is a health/metrics endpoint exempt from
// auth so probes and scrapers work without the shared secret.
func isOperationalPath(p string) bool {
	return p == "/healthz" || p == "/metrics" || strings.HasPrefix(p, "/metrics/")
}

// withAuth gates inbound requests with a shared secret when token != "".
// Clients present it as `Authorization: Bearer <token>`. Empty token = open
// (the trusted-LAN default; no behavior change). Health/metrics stay open so
// operational probes don't need the secret. This is defense in depth, not the
// trust boundary — see deploy/README.md ("Security / trust boundary").
func withAuth(token string, next http.Handler) http.Handler {
	if token == "" {
		return next
	}
	want := "Bearer " + token
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if isOperationalPath(r.URL.Path) {
			next.ServeHTTP(w, r)
			return
		}
		got := r.Header.Get("Authorization")
		if subtle.ConstantTimeCompare([]byte(got), []byte(want)) != 1 {
			httpErrWrite(w, http.StatusUnauthorized, "missing or invalid bearer token")
			return
		}
		next.ServeHTTP(w, r)
	})
}
