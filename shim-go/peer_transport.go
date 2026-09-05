package main

import (
	"crypto/tls"
	"log/slog"
	"net"
	"net/http"
	"syscall"
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
	tr := &http.Transport{
		// The empty non-nil map is what actually disables the H2 upgrade;
		// ForceAttemptHTTP2:false alone is not enough for https:// peer URLs.
		ForceAttemptHTTP2:   false,
		TLSNextProto:        map[string]func(string, *tls.Conn) http.RoundTripper{},
		MaxIdleConnsPerHost: cfg.MaxIdleConnsPerHost,
		MaxConnsPerHost:     0,
		IdleConnTimeout:     90 * time.Second,
	}
	if ctrl := socketBufferControl(cfg.SocketBufferBytes); ctrl != nil {
		tr.DialContext = (&net.Dialer{Timeout: 30 * time.Second, KeepAlive: 30 * time.Second, Control: ctrl}).DialContext
	}
	return tr
}

// socketBufferControl returns a net.Dialer.Control that pins SO_RCVBUF/SO_SNDBUF
// to bytes on the raw socket before connect. Returns nil (no control) when bytes
// <= 0, which is the default: modern Linux tcp_rmem/tcp_wmem autotuning beats a
// static guess, and a wrong static value CAPS throughput. Best-effort: a failed
// setsockopt is logged at debug and ignored, never failing the dial.
func socketBufferControl(bytes int) func(network, address string, c syscall.RawConn) error {
	if bytes <= 0 {
		return nil
	}
	return func(_, _ string, c syscall.RawConn) error {
		return c.Control(func(fd uintptr) {
			if err := syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_RCVBUF, bytes); err != nil {
				slog.Debug("peer dial: SO_RCVBUF failed", "bytes", bytes, "err", err)
			}
			if err := syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_SNDBUF, bytes); err != nil {
				slog.Debug("peer dial: SO_SNDBUF failed", "bytes", bytes, "err", err)
			}
		})
	}
}
