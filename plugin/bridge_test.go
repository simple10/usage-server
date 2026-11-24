package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

func TestValidAPIPath(t *testing.T) {
	good := []string{"/api/health", "/api/session/reparse", "/api/breakdown", "/api/a-b_c.d"}
	bad := []string{
		"", "/", "/api", "api/health", "/ui/", "/api/../ui", "/api/..", "/api/a/../../x",
		"/api//x", "/api/x?y=1", "/api/x#f", "/api/%2e%2e/x", `/api\x`, "/api/x y",
		"/api/x\n", "http://evil/api/x", "//evil/api/x", "/api/" + strings.Repeat("a", 300), "/api/ü",
	}
	for _, p := range good {
		if !validAPIPath(p) {
			t.Errorf("rejected good path %q", p)
		}
	}
	for _, p := range bad {
		if validAPIPath(p) {
			t.Errorf("accepted bad path %q", p)
		}
	}
}

type seen struct {
	method, path, query, token, auth, ct, body string
}

func newServer(t *testing.T, h http.HandlerFunc) *httptest.Server {
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	return srv
}

func mgmtPayload(t *testing.T, method, path string, q url.Values, body string) []byte {
	raw, err := json.Marshal(managementRequest{
		Method: method, Path: path, Query: q, Body: []byte(body),
		Headers: http.Header{"Authorization": {"Bearer management-key"}, "Content-Type": {"application/json"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func withBridge(t *testing.T, base string, timeout time.Duration) {
	t.Helper()
	cfg := Config{Enabled: true, ServerURL: base, BridgeToken: "bridge-secret", MenuLabel: "Usage"}
	global.cfg.Store(&cfg)
	global.bridge.Store(newBridge(base, "bridge-secret", timeout))
	t.Cleanup(func() { global.bridge.Store(nil); global.cfg.Store(nil) })
}

func TestBridgeForwardsAPI(t *testing.T) {
	var got seen
	srv := newServer(t, func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		got = seen{r.Method, r.URL.Path, r.URL.RawQuery, r.Header.Get(bridgeHeader), r.Header.Get("Authorization"), r.Header.Get("Content-Type"), string(b)}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Set-Cookie", "x=y")
		w.WriteHeader(201)
		_, _ = w.Write([]byte(`{"ok":"<b>"}`))
	})
	withBridge(t, srv.URL, time.Second)
	q := url.Values{"path": {"/api/labels"}, "kind": {"auth"}}
	resp := global.handleManagement(mgmtPayload(t, "POST", "/v0/management/usage-server/api", q, `{"a":1}`))
	if resp.StatusCode != 201 || string(resp.Body) != `{"ok":"<b>"}` {
		t.Fatalf("resp %d %s", resp.StatusCode, resp.Body)
	}
	if got.method != "POST" || got.path != "/api/labels" || got.query != "kind=auth" || got.body != `{"a":1}` {
		t.Fatalf("server saw %+v", got)
	}
	if got.token != "bridge-secret" {
		t.Fatal("bridge token missing")
	}
	if got.auth != "" {
		t.Fatal("management key forwarded to the server")
	}
	if resp.Headers.Get("Set-Cookie") != "" || resp.Headers.Get("Content-Type") != "application/json" {
		t.Fatalf("headers %v", resp.Headers)
	}
}

func TestBridgeDashboard(t *testing.T) {
	var path string
	srv := newServer(t, func(w http.ResponseWriter, r *http.Request) {
		path = r.URL.Path
		w.Header().Set("Content-Type", "text/html")
		_, _ = w.Write([]byte("<html></html>"))
	})
	withBridge(t, srv.URL, time.Second)
	resp := global.handleManagement(mgmtPayload(t, "GET", "/v0/resource/plugins/usage-server/dashboard", nil, ""))
	if resp.StatusCode != 200 || path != "/ui/" || resp.Headers.Get("Content-Type") != "text/html" {
		t.Fatalf("resp %d path %s", resp.StatusCode, path)
	}
}

func TestBridgeErrors(t *testing.T) {
	slow := newServer(t, func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-time.After(2 * time.Second):
		}
	})
	redirect := newServer(t, func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "http://example.invalid/", http.StatusFound)
	})
	huge := newServer(t, func(w http.ResponseWriter, r *http.Request) {
		chunk := make([]byte, 1<<20)
		for i := 0; i < 33; i++ {
			if _, err := w.Write(chunk); err != nil {
				return
			}
		}
	})
	dead := httptest.NewServer(http.NotFoundHandler())
	deadURL := dead.URL
	dead.Close()

	q := url.Values{"path": {"/api/health"}}
	cases := []struct {
		name   string
		base   string
		method string
		q      url.Values
		want   int
		tmo    time.Duration
	}{
		{"timeout", slow.URL, "GET", q, 504, 200 * time.Millisecond},
		{"unreachable", deadURL, "GET", q, 502, 200 * time.Millisecond},
		{"redirect not followed", redirect.URL, "GET", q, 302, 200 * time.Millisecond},
		{"too large", huge.URL, "GET", q, 502, 5 * time.Second},
		{"bad path", redirect.URL, "GET", url.Values{"path": {"/api/../ui/"}}, 400, 200 * time.Millisecond},
		{"no path", redirect.URL, "GET", nil, 400, 200 * time.Millisecond},
		{"bad method", redirect.URL, "DELETE", q, 405, 200 * time.Millisecond},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			withBridge(t, c.base, c.tmo)
			start := time.Now()
			resp := global.handleManagement(mgmtPayload(t, c.method, "/v0/management/usage-server/api", c.q, ""))
			if resp.StatusCode != c.want {
				t.Fatalf("status %d want %d (%s)", resp.StatusCode, c.want, resp.Body)
			}
			if el := time.Since(start); el > c.tmo+time.Second {
				t.Fatalf("took %v", el)
			}
		})
	}
}

func TestBridgeDisabled(t *testing.T) {
	global.bridge.Store(nil)
	resp := global.handleManagement(mgmtPayload(t, "GET", "/v0/management/usage-server/api", nil, ""))
	if resp.StatusCode != 503 {
		t.Fatalf("status %d", resp.StatusCode)
	}
	if reg := global.managementRoutes(); len(reg.Routes) != 0 || len(reg.Resources) != 0 {
		t.Fatalf("routes registered with bridge off: %+v", reg)
	}
}

func TestManagementRoutes(t *testing.T) {
	withBridge(t, "http://usage-server:8080", time.Second)
	reg := global.managementRoutes()
	if len(reg.Routes) != 2 || len(reg.Resources) != 1 {
		t.Fatalf("%+v", reg)
	}
	for _, r := range reg.Routes {
		if r.Path != apiRoute || r.Menu != "" {
			t.Fatalf("route %+v", r)
		}
	}
	if reg.Resources[0].Path != dashboardRoute || reg.Resources[0].Menu != "Usage" {
		t.Fatalf("resource %+v", reg.Resources[0])
	}
	raw, _ := json.Marshal(reg)
	if !strings.Contains(string(raw), `"routes"`) || !strings.Contains(string(raw), `"resources"`) {
		t.Fatalf("json keys: %s", raw)
	}
}
