package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
	"time"
)

// hostRecord builds a payload shaped like the host's pluginapi.UsageRecord
// JSON (Go field names, durations as nanoseconds, time as RFC 3339).
func hostRecord(t *testing.T, mut func(m map[string]any)) []byte {
	t.Helper()
	m := map[string]any{
		"RequestID": "req-1", "TraceID": "abcd1234", "SelectionID": "sel",
		"Provider": "claude", "BaseURL": "https://upstream.example", "ExecutorType": "claude",
		"Model": "claude-haiku-4-5", "Alias": "haiku", "APIKey": "sk-test-0123456789abcd",
		"IsNativeKey": true, "AccessProvider": "config-inline",
		"SessionID": "sess-1", "ParentSessionID": "",
		"AuthID": "auth-1", "AuthIndex": "3", "AuthType": "apikey",
		"Source": "sk-upstream-secret-0123456789", "ReasoningEffort": "low", "ServiceTier": "", "EffectiveServiceTier": "",
		"ResponseServiceTier": "", "ResponseModel": "claude-haiku-4-5-20251001",
		"Generate": true, "Stream": true,
		"RequestedAt": "2026-11-24T10:00:00.123456789+01:00",
		"Latency": int64(1500 * time.Millisecond), "TTFT": int64(200 * time.Millisecond),
		"Failed": false, "Failure": map[string]any{"StatusCode": 0, "Body": ""},
		"Detail": map[string]any{
			"InputTokens": 10, "OutputTokens": 20, "ReasoningTokens": 0, "CachedTokens": 5,
			"CacheReadTokens": 5, "CacheCreationTokens": 7, "TotalTokens": 42,
			"CacheCreation5mTokens": 3, "CacheCreation1hTokens": 4, "TextInputTokens": nil,
		},
		"ResponseHeaders": http.Header{
			"Anthropic-Ratelimit-Unified-5h-Utilization": {"0.25"},
			"X-Codex-Primary-Used-Percent":               {"12"},
			"Set-Cookie":                                 {"secret"},
			"Request-Id":                                 {"x"},
		},
	}
	if mut != nil {
		mut(m)
	}
	raw, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func decodeRecord(t *testing.T, payload []byte) (spoolRecord, []byte) {
	t.Helper()
	var in hostUsageRecord
	if err := json.Unmarshal(payload, &in); err != nil {
		t.Fatal(err)
	}
	rec := toSpoolRecord(&in)
	line, err := json.Marshal(rec)
	if err != nil {
		t.Fatal(err)
	}
	return rec, line
}

func TestToSpoolRecord(t *testing.T) {
	rec, line := decodeRecord(t, hostRecord(t, nil))
	sum := sha256.Sum256([]byte("sk-test-0123456789abcd"))
	if rec.APIKeyHash != hex.EncodeToString(sum[:]) {
		t.Fatalf("hash %s", rec.APIKeyHash)
	}
	if rec.APIKeyHint != "…abcd" {
		t.Fatalf("hint %q", rec.APIKeyHint)
	}
	if strings.Contains(string(line), "sk-test-0123456789abcd") {
		t.Fatal("raw key leaked into spool line")
	}
	if strings.Contains(string(line), "upstream-secret") || rec.Source != "" {
		t.Fatal("Source (may be an upstream key) leaked into spool line")
	}
	srcSum := sha256.Sum256([]byte("sk-upstream-secret-0123456789"))
	if rec.SourceHash != hex.EncodeToString(srcSum[:]) || rec.SourceHint != "…6789" {
		t.Fatalf("source hash/hint %q %q", rec.SourceHash, rec.SourceHint)
	}
	if rec.V != 1 || rec.RequestID != "req-1" || rec.TraceID != "abcd1234" {
		t.Fatalf("ids: %+v", rec)
	}
	if rec.TS != "2026-11-24T09:00:00.123456789Z" {
		t.Fatalf("ts must be UTC: %s", rec.TS)
	}
	if rec.LatencyMS != 1500 || rec.TTFTMS != 200 {
		t.Fatalf("durations: %d %d", rec.LatencyMS, rec.TTFTMS)
	}
	if rec.Input != 10 || rec.Output != 20 || rec.CacheRead != 5 || rec.Cached != 5 || rec.CacheWrite != 7 || rec.Total != 42 {
		t.Fatalf("tokens: %+v", rec)
	}
	if rec.CacheWrite5m == nil || *rec.CacheWrite5m != 3 || rec.CacheWrite1h == nil || *rec.CacheWrite1h != 4 {
		t.Fatalf("ttl split: %v %v", rec.CacheWrite5m, rec.CacheWrite1h)
	}
	if rec.Executor != "claude" || rec.Provider != "claude" || rec.Model != "claude-haiku-4-5" || rec.Alias != "haiku" ||
		rec.ResponseModel != "claude-haiku-4-5-20251001" || rec.SessionID != "sess-1" || rec.AuthID != "auth-1" ||
		rec.AuthIndex != "3" || rec.AuthType != "apikey" || rec.AccessProvider != "config-inline" || !rec.Stream || !rec.Generate {
		t.Fatalf("fields: %+v", rec)
	}
	want := map[string]string{
		"anthropic-ratelimit-unified-5h-utilization": "0.25",
		"x-codex-primary-used-percent":               "12",
	}
	if len(rec.RateLimit) != len(want) {
		t.Fatalf("ratelimit: %v", rec.RateLimit)
	}
	for k, v := range want {
		if rec.RateLimit[k] != v {
			t.Fatalf("ratelimit %s: %v", k, rec.RateLimit)
		}
	}
	if rec.Failed || rec.Status != 0 || rec.Error != "" {
		t.Fatal("failure fields set on success")
	}
	// Field names are the server contract.
	var m map[string]any
	_ = json.Unmarshal(line, &m)
	for _, k := range []string{"v", "request_id", "ts", "api_key_hash", "input", "output", "latency_ms", "generate", "cache_write_5m", "cache_write_1h", "ratelimit"} {
		if _, ok := m[k]; !ok {
			t.Fatalf("missing %s in %s", k, line)
		}
	}
}

func TestToSpoolRecordFailureAndMissingSplit(t *testing.T) {
	long := strings.Repeat("é", 600)
	rec, _ := decodeRecord(t, hostRecord(t, func(m map[string]any) {
		m["Failed"] = true
		m["Failure"] = map[string]any{"StatusCode": 529, "Body": long}
		d := m["Detail"].(map[string]any)
		d["CacheCreation5mTokens"] = nil
		d["CacheCreation1hTokens"] = nil
		m["RequestedAt"] = "0001-01-01T00:00:00Z"
		delete(m, "ResponseHeaders")
	}))
	if !rec.Failed || rec.Status != 529 {
		t.Fatalf("failure: %+v", rec)
	}
	if !strings.HasSuffix(rec.Error, "…") || len(rec.Error) > maxErrorChars+len("…") {
		t.Fatalf("error not cut: %d", len(rec.Error))
	}
	if !json.Valid([]byte(`"` + rec.Error + `"`)) || strings.ContainsRune(rec.Error, '�') {
		t.Fatal("error cut inside a rune")
	}
	if rec.CacheWrite5m != nil || rec.CacheWrite1h != nil {
		t.Fatal("missing split must stay absent")
	}
	if rec.TS == "0001-01-01T00:00:00Z" {
		t.Fatal("zero time must be replaced")
	}
	if rec.RateLimit != nil {
		t.Fatal("ratelimit without headers")
	}
}

func TestSafeSource(t *testing.T) {
	cases := []struct{ in, raw string; hashed bool }{
		{"", "", false},
		{"user@example.com", "user@example.com", false},
		{" user@example.com ", "user@example.com", false},
		{"sk-ant-api03-abcdefghijklmnop", "", true},
		{"my-gcp-project", "", true},
		{"@", "", true},
		{"a@", "", true},
		{"http://x@y", "", true},
	}
	for _, c := range cases {
		raw, hash, _ := safeSource(c.in)
		if raw != c.raw || (hash != "") != c.hashed {
			t.Errorf("%q: raw=%q hash=%q", c.in, raw, hash)
		}
	}
}

func TestHashKey(t *testing.T) {
	cases := []struct{ key, hint string }{
		{"", ""},
		{"short", ""},
		{"elevenchars", ""},
		{"twelve-chars", "…hars"},
		{"ключ-ключ-ключ", "…ключ"},
	}
	for _, c := range cases {
		h, hint := hashKey(c.key)
		if c.key == "" {
			if h != "" || hint != "" {
				t.Fatal("empty key must give empty hash")
			}
			continue
		}
		sum := sha256.Sum256([]byte(c.key))
		if h != hex.EncodeToString(sum[:]) {
			t.Fatalf("%q: hash mismatch", c.key)
		}
		if hint != c.hint {
			t.Fatalf("%q: hint %q want %q", c.key, hint, c.hint)
		}
	}
	// The hash is of the exact key, spaces included.
	a, _ := hashKey("abc ")
	b, _ := hashKey("abc")
	if a == b {
		t.Fatal("hash must not trim")
	}
}

func TestHandleUsageDecodeError(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig(dir)
	w, err := newSpoolWriterTuned(cfg, fastTuning(cfg))
	if err != nil {
		t.Fatal(err)
	}
	global.spool.Store(w)
	defer func() { global.spool.Store(nil); w.close() }()
	handleUsage([]byte("{not json"))
	handleUsage(nil)
	if got := w.stats.decodeErrors.Load(); got != 2 {
		t.Fatalf("decode errors %d", got)
	}
	// Disabled plugin: handleUsage is a no-op.
	global.spool.Store(nil)
	handleUsage(hostRecord(t, nil))
}
