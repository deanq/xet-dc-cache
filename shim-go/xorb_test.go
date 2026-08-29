package main

import (
	"io"
	"net/http"
	"strings"
	"testing"
)

type fakeDoer struct {
	good  string
	calls []string
}

func (f *fakeDoer) Do(req *http.Request) (*http.Response, error) {
	f.calls = append(f.calls, req.URL.String())
	code := 403
	body := ""
	if req.URL.String() == f.good {
		code, body = 206, "BYTES"
	}
	return &http.Response{
		StatusCode: code,
		Body:       io.NopCloser(strings.NewReader(body)),
		Header:     http.Header{},
	}, nil
}

func newTestServer(doer httpDoer) *Server {
	return &Server{
		signed:           NewTTLMap(3600e9, 100000, nil),
		metrics:          NewMetrics(),
		doer:             doer,
		signedCandidates: 8,
	}
}

func TestRememberSignedOrderDedupCap(t *testing.T) {
	s := newTestServer(nil)
	for _, u := range []string{"u1", "u2", "u3"} {
		s.rememberSigned("h", u)
	}
	if got, _ := s.signed.Get("h"); !equalSlice(got, []string{"u3", "u2", "u1"}) {
		t.Fatalf("order = %v", got)
	}
	s.rememberSigned("h", "u2") // re-seen -> front, no dup
	if got, _ := s.signed.Get("h"); !equalSlice(got, []string{"u2", "u3", "u1"}) {
		t.Fatalf("after re-seen = %v", got)
	}
	for i := 0; i < 20; i++ {
		s.rememberSigned("h", string(rune('A'+i)))
	}
	if got, _ := s.signed.Get("h"); len(got) != s.signedCandidates {
		t.Fatalf("cap = %d, want %d", len(got), s.signedCandidates)
	}
}

func TestFetchAuthorizedTriesCandidates(t *testing.T) {
	f := &fakeDoer{good: "right"}
	s := newTestServer(f)
	s.signed.Set("h", []string{"wrong1", "wrong2", "right", "wrong3"})
	resp, err := s.fetchAuthorized("h", "bytes=0-4")
	if err != nil || resp.StatusCode != 206 {
		t.Fatalf("got (%v, %v)", resp, err)
	}
	if !equalSlice(f.calls, []string{"wrong1", "wrong2", "right"}) {
		t.Fatalf("calls = %v (should stop at first authorized)", f.calls)
	}
}

func TestFetchAuthorized502WhenNone(t *testing.T) {
	s := newTestServer(&fakeDoer{good: "nope"})
	s.signed.Set("h", []string{"a", "b"})
	_, err := s.fetchAuthorized("h", "bytes=0-4")
	he, ok := err.(*httpError)
	if !ok || he.code != 502 {
		t.Fatalf("err = %v, want 502 httpError", err)
	}
}

func TestFetchAuthorized409Unknown(t *testing.T) {
	s := newTestServer(&fakeDoer{})
	_, err := s.fetchAuthorized("never", "bytes=0-4")
	he, ok := err.(*httpError)
	if !ok || he.code != 409 {
		t.Fatalf("err = %v, want 409 httpError", err)
	}
}

func equalSlice(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
