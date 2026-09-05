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
	"time"
)

// elapsedMs is the wall time from start to end in milliseconds, with sub-ms
// resolution (microsecond-derived) so fast disk HITs land below the 1ms bucket.
func elapsedMs(start, end time.Time) float64 {
	return float64(end.Sub(start).Microseconds()) / 1000.0
}

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
