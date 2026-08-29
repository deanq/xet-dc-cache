package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"strings"
)

func (s *Server) proxyHub(r *http.Request, fullPath string) (*http.Response, []byte, error) {
	var body io.Reader
	if r.Body != nil {
		b, _ := io.ReadAll(r.Body)
		body = bytes.NewReader(b)
	}
	url := s.hfUpstream + "/" + fullPath
	req, err := http.NewRequest(r.Method, url, body)
	if err != nil {
		return nil, nil, err
	}
	cleanHeaders(req.Header, r.Header)
	req.URL.RawQuery = r.URL.RawQuery
	req.Host = strings.TrimPrefix(s.hfUpstream, "https://")
	req.Host = strings.TrimPrefix(req.Host, "http://")
	req.Header.Set("Accept-Encoding", "identity")

	resp, err := s.doer.Do(req)
	if err != nil {
		return nil, nil, err
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)
	return resp, respBody, nil
}

func (s *Server) hub(w http.ResponseWriter, r *http.Request) {
	fullPath := strings.TrimPrefix(r.URL.Path, "/")
	upstream, body, err := s.proxyHub(r, fullPath)
	if err != nil {
		httpErrWrite(w, http.StatusBadGateway, "hub upstream error: "+err.Error())
		return
	}

	if strings.Contains(fullPath, "/xet-read-token/") && upstream.StatusCode == http.StatusOK {
		var tok map[string]any
		if json.Unmarshal(body, &tok) == nil {
			tok["casUrl"] = s.publicBase + "/cas"
			if rewritten, merr := json.Marshal(tok); merr == nil {
				body = rewritten
			}
			// Current huggingface_hub reads the xet connection info from the
			// response HEADERS, not the body: forward the upstream headers and
			// point the CAS url at us so reconstruction + xorb traffic flows
			// through the shim. The access token and expiration pass through
			// unchanged (the client still authenticates to the real CAS via us).
			cleanHeaders(w.Header(), upstream.Header)
			if w.Header().Get("X-Xet-Cas-Url") != "" {
				w.Header().Set("X-Xet-Cas-Url", s.publicBase+"/cas")
			}
			w.Header().Set("Content-Type", "application/json")
			s.metrics.Incr("token_rewrites", 1)
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(body)
			return
		}
	}

	cleanHeaders(w.Header(), upstream.Header)
	w.WriteHeader(upstream.StatusCode)
	_, _ = w.Write(body)
}
