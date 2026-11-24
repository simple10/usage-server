package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// The bridge puts the usage server's dashboard inside the proxy's management
// panel. It registers:
//
//   - a resource page (the panel shows it in an iframe). The proxy does not
//     authenticate resource pages, so the server's page holds no data.
//   - GET and POST management routes. The proxy checks the management key on
//     these before calling us, then we forward to the server with a bridge
//     token and a short timeout.
//
// If the server is down or slow, the routes return 502/504. Nothing else in
// the proxy is affected.

const (
	apiRoute       = "/usage-server/api"
	dashboardRoute = "/dashboard"
	bridgeHeader   = "X-Usage-Bridge-Token"
	maxBodyBytes   = 32 << 20
)

type bridge struct {
	base   string
	token  string
	client *http.Client
	tmo    time.Duration
}

func newBridge(base, token string, timeout time.Duration) *bridge {
	return &bridge{
		base:  base,
		token: token,
		tmo:   timeout,
		client: &http.Client{
			Timeout: timeout,
			// Never follow redirects to somewhere else.
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
	}
}

type route struct {
	Method      string `json:"Method,omitempty"`
	Path        string `json:"Path"`
	Menu        string `json:"Menu,omitempty"`
	Description string `json:"Description,omitempty"`
}

type managementRegistration struct {
	Routes    []route `json:"routes,omitempty"`
	Resources []route `json:"resources,omitempty"`
}

func (s *state) managementRoutes() managementRegistration {
	b, cfg := s.currentBridge()
	if b == nil {
		return managementRegistration{}
	}
	return managementRegistration{
		Routes: []route{
			{Method: http.MethodGet, Path: apiRoute},
			{Method: http.MethodPost, Path: apiRoute},
		},
		Resources: []route{
			{Path: dashboardRoute, Menu: cfg.MenuLabel, Description: "Token usage and estimated cost by key, upstream, model and session."},
		},
	}
}

type managementRequest struct {
	Method  string
	Path    string
	Headers http.Header
	Query   url.Values
	Body    []byte
}

type managementResponse struct {
	StatusCode int
	Headers    http.Header
	Body       []byte
}

func textResponse(status int, msg string) managementResponse {
	return managementResponse{
		StatusCode: status,
		Headers:    http.Header{"Content-Type": {"application/json"}},
		Body:       mustJSON(map[string]string{"error": msg}),
	}
}

func mustJSON(v any) []byte {
	raw, _ := json.Marshal(v)
	return raw
}

func (s *state) handleManagement(payload []byte) managementResponse {
	b, _ := s.currentBridge()
	if b == nil {
		return textResponse(http.StatusServiceUnavailable, "usage bridge disabled")
	}
	var req managementRequest
	if err := json.Unmarshal(payload, &req); err != nil {
		return textResponse(http.StatusBadRequest, "bad request")
	}
	switch {
	case strings.HasSuffix(req.Path, dashboardRoute):
		return b.forward(http.MethodGet, "/ui/", nil, nil, req.Headers)
	case strings.HasSuffix(req.Path, apiRoute):
		target := req.Query.Get("path")
		if !strings.HasPrefix(target, "/api/") || strings.Contains(target, "..") {
			return textResponse(http.StatusBadRequest, "path must start with /api/")
		}
		q := url.Values{}
		for k, v := range req.Query {
			if k != "path" {
				q[k] = v
			}
		}
		method := strings.ToUpper(req.Method)
		if method != http.MethodGet && method != http.MethodPost {
			return textResponse(http.StatusMethodNotAllowed, "method not allowed")
		}
		return b.forward(method, target, q, req.Body, req.Headers)
	default:
		return textResponse(http.StatusNotFound, "not found")
	}
}

func (b *bridge) forward(method, path string, q url.Values, body []byte, in http.Header) managementResponse {
	u := b.base + path
	if len(q) > 0 {
		u += "?" + q.Encode()
	}
	ctx, cancel := context.WithTimeout(context.Background(), b.tmo)
	defer cancel()
	var reader io.Reader
	if len(body) > 0 {
		reader = bytes.NewReader(body)
	}
	out, err := http.NewRequestWithContext(ctx, method, u, reader)
	if err != nil {
		return textResponse(http.StatusBadRequest, "bad request")
	}
	if b.token != "" {
		out.Header.Set(bridgeHeader, b.token)
	}
	if ct := in.Get("Content-Type"); ct != "" {
		out.Header.Set("Content-Type", ct)
	}
	out.Header.Set("Accept", "application/json, text/html")
	resp, err := b.client.Do(out)
	if err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return textResponse(http.StatusGatewayTimeout, "usage server timed out")
		}
		return textResponse(http.StatusBadGateway, "usage server unavailable")
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxBodyBytes))
	if err != nil {
		return textResponse(http.StatusBadGateway, "usage server read failed")
	}
	h := http.Header{}
	for _, k := range []string{"Content-Type", "Cache-Control", "Content-Security-Policy"} {
		if v := resp.Header.Get(k); v != "" {
			h.Set(k, v)
		}
	}
	return managementResponse{StatusCode: resp.StatusCode, Headers: h, Body: data}
}
