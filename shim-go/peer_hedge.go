package main

import (
	"context"
	"time"
)

// hedgeSource identifies which side of the race produced the served body.
type hedgeSource int

const (
	srcPeer hedgeSource = iota
	srcCDN
)

// label is the metrics source name for this side of the race.
func (h hedgeSource) label() string {
	if h == srcPeer {
		return "peer"
	}
	return "cdn"
}

// rangeSize returns the byte length of an HTTP Range (hi-lo), 0 if unparseable.
func rangeSize(byteRange string) int64 {
	lo, hi, err := parseRange(byteRange)
	if err != nil {
		return 0
	}
	return hi - lo
}

type raceResult struct {
	res   xorbResult
	src   hedgeSource
	ok    bool
	bytes int64 // body bytes read (used to book true CDN waste on a lost hedge)
}

// raceOnePeer fires the peer GET to base and gives it an adaptive head start
// sized by the peer's recent throughput. If the peer finishes first, it wins
// with zero CDN cost. If the timer fires first, the CDN GET is launched in
// parallel and the first to finish wins; the loser is cancelled via context.
// Guarantee: worst-case latency is hedgeDelay + cdnFetch <= PEER_HEDGE_MAX_MS +
// cdnFetch, because a stalled peer produces no bytes and the CDN wins. This
// bound is measured from when the race starts; on the fan-out discovery path
// one peer HEAD RTT precedes the race (the sticky path avoids it), so real
// slack on a cold/non-sticky range is discovery RTT + PEER_HEDGE_MAX_MS.
//
// The bound is PER RACE. raceOnePeer returns ok=false only when BOTH its peer
// and CDN sides fail, so on cascading CDN failure the caller (fetchFromPeer)
// can run a sticky race then a fan-out race, and getXorb then still runs the
// plain CDN path — up to three sequential CDN attempts. That is resilience
// (retry on CDN failure), not a violation, but the per-request worst case
// exceeds this per-race bound whenever the CDN is failing.
func (s *Server) raceOnePeer(ctx context.Context, base, hash, byteRange string) (xorbResult, hedgeSource, bool) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	size := rangeSize(byteRange)
	delay := s.peerStats.hedgeDelay(base, size, s.hedgeFactor, s.hedgeMinMs, s.hedgeMaxMs)

	peerCh := make(chan raceResult, 1)
	go func() {
		start := s.now()
		res, ok := s.peerGet(ctx, base, hash, byteRange)
		if ok {
			s.peerStats.update(base, int64(len(res.body)), s.now().Sub(start))
		}
		peerCh <- raceResult{res: res, src: srcPeer, ok: ok}
	}()

	// Head start: peer wins outright if it finishes before the timer.
	var timerC <-chan time.Time
	if s.hedgeAfter != nil {
		timerC = s.hedgeAfter(delay)
	} else {
		t := time.NewTimer(delay)
		defer t.Stop()
		timerC = t.C
	}
	select {
	case o := <-peerCh:
		return o.res, o.src, o.ok
	case <-timerC:
	}

	// Timer fired: hedge the CDN in parallel and race to first completion.
	s.metrics.Incr("peer_hedge_fired", 1)
	cdnCh := make(chan raceResult, 1)
	go func() {
		res, n, ok := s.cdnGet(ctx, hash, byteRange)
		cdnCh <- raceResult{res: res, src: srcCDN, ok: ok, bytes: n}
	}()

	for {
		select {
		case o := <-peerCh:
			if o.ok {
				cancel() // stop the CDN loser
				s.metrics.Incr("peer_hedge_peer_won", 1)
				// Book the CDN bytes actually transferred before cancellation
				// (the true waste), not the requested range size. Draining
				// cdnCh here is bounded by cancellation latency — we already
				// cancel()'d, so the CDN read unwinds promptly; we are NOT
				// waiting for the CDN download to finish.
				if c := <-cdnCh; c.bytes > 0 {
					s.metrics.Incr("peer_bytes_wasted", c.bytes)
				}
				return o.res, srcPeer, true
			}
			// Peer failed after the hedge; the CDN is our only hope.
			c := <-cdnCh
			return c.res, srcCDN, c.ok
		case o := <-cdnCh:
			if o.ok {
				cancel() // stop the peer loser
				s.metrics.Incr("peer_hedge_cdn_won", 1)
				return o.res, srcCDN, true
			}
			// CDN failed; fall back to whatever the peer produces.
			p := <-peerCh
			return p.res, srcPeer, p.ok
		}
	}
}
