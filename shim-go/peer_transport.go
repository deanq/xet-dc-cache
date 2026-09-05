package main

import (
	"crypto/tls"
	"net/http"
	"time"
)

// peerTransportConfig carries the tunables for the dedicated peer transport.
// SocketBufferBytes is consumed in a later task (0 = OS autotune).
type peerTransportConfig struct {
	MaxIdleConnsPerHost int
	SocketBufferBytes   int
}

// newPeerTransport builds the transport used ONLY for peer traffic. It is kept
// strictly distinct from the CDN doer: HTTP/2 is disabled so N concurrent ranges
// ride N independent TCP congestion windows instead of one multiplexed stream,
// and the idle pool is fat so a thousands-of-ranges model pull never thrashes
// connections. The CDN transport's redirect/identity/compression contract is
// unaffected — this is a separate object.
func newPeerTransport(cfg peerTransportConfig) *http.Transport {
	if cfg.MaxIdleConnsPerHost <= 0 {
		cfg.MaxIdleConnsPerHost = 64
	}
	return &http.Transport{
		// The empty non-nil map is what actually disables the H2 upgrade;
		// ForceAttemptHTTP2:false alone is not enough for https:// peer URLs.
		ForceAttemptHTTP2:   false,
		TLSNextProto:        map[string]func(string, *tls.Conn) http.RoundTripper{},
		MaxIdleConnsPerHost: cfg.MaxIdleConnsPerHost,
		MaxConnsPerHost:     0,
		IdleConnTimeout:     90 * time.Second,
	}
}
