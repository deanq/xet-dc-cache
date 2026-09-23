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

// requestClass buckets a request path by the shim role it exercises, so
// per-path counters can show which stages a client actually routes through the
// shim: "hub" (metadata: token / resolve / api), "reconstruction" (the Xet
// terms/xorbs manifest), or "xorb" (the actual chunk bytes). A client that hits
// hub but not xorb is fetching bytes elsewhere (e.g. hf_xet going direct to CAS).
func requestClass(p string) string {
	switch {
	case strings.HasPrefix(p, "/cas/"):
		return "reconstruction"
	case strings.HasPrefix(p, "/xorb/"):
		return "xorb"
	default:
		return "hub"
	}
}

// withLogging emits one structured (slog) line per request at completion:
// method, path, status, the X-Cache disposition (HIT/MISS), bytes, duration.
// It also increments a per-path request counter (req_hub / req_reconstruction /
// req_xorb) so /metrics reveals where a client's traffic actually lands.
func withLogging(m *Metrics, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		if !isOperationalPath(r.URL.Path) {
			m.Incr("req_"+requestClass(r.URL.Path), 1)
		}
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

// withAuth authenticates the PEER channel with a shared secret when token != "".
// It deliberately does NOT gate client data traffic: under transparent
// interception a stock HuggingFace client only ever sends its own HF token in
// `Authorization` (which the shim forwards upstream), so it can never present
// the shim secret — gating client paths on it would 401 every real download.
// Client traffic is protected by network isolation (the trusted-LAN posture,
// see deploy/README.md). Peer requests (X-Xet-Peer:1) DO carry the bearer
// explicitly (setPeerHeaders), so the secret guards the one channel that can
// actually satisfy it: an unauthorized node cannot pull from or probe the
// fleet cache. Empty token = open. Health/metrics stay open for probes.
func withAuth(token string, next http.Handler) http.Handler {
	if token == "" {
		return next
	}
	want := "Bearer " + token
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !isPeerRequest(r) || isOperationalPath(r.URL.Path) {
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
