package main

import (
	"encoding/json"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func call(t *testing.T, method string, payload any) envelope {
	t.Helper()
	var raw []byte
	if payload != nil {
		var err error
		if raw, err = json.Marshal(payload); err != nil {
			t.Fatal(err)
		}
	}
	out, _ := dispatch(method, raw)
	var env envelope
	if err := json.Unmarshal(out, &env); err != nil {
		t.Fatalf("bad envelope %s: %v", out, err)
	}
	return env
}

func TestLifecycle(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "spool")
	yml := "enabled: true\npriority: 0\nspool-dir: " + dir + "\nqueue-size: 32\nflush-interval-ms: 10\n"
	env := call(t, "plugin.register", lifecycleRequest{ConfigYAML: []byte(yml), SchemaVersion: 10})
	if !env.OK {
		t.Fatalf("register: %+v", env.Error)
	}
	var reg registration
	_ = json.Unmarshal(env.Result, &reg)
	if reg.SchemaVersion != 6 || reg.Capabilities["usage_plugin"] != true || reg.Capabilities["management_api"] != true {
		t.Fatalf("registration %s", env.Result)
	}
	// Mirror the host's validPlugin(): these metadata fields must be non-empty.
	for _, k := range []string{"Name", "Version", "Author", "GitHubRepository"} {
		if v, _ := reg.Metadata[k].(string); strings.TrimSpace(v) == "" {
			t.Fatalf("metadata %s is empty; the host would reject the plugin", k)
		}
	}
	w := global.currentSpool()
	if w == nil {
		t.Fatal("spool not started")
	}
	payload := hostRecord(t, nil)
	if out, ok := dispatch("usage.handle", payload); !ok {
		t.Fatalf("usage.handle failed: %s", out)
	}
	if env := call(t, "plugin.quiesce", nil); !env.OK {
		t.Fatal("quiesce")
	}
	waitFor(t, "record written", func() bool { return w.stats.written.Load() == 1 })

	// Same config: the writer is kept.
	call(t, "plugin.reconfigure", lifecycleRequest{ConfigYAML: []byte(yml)})
	if global.currentSpool() != w {
		t.Fatal("writer replaced without a config change")
	}
	// Changed config: a new writer.
	call(t, "plugin.reconfigure", lifecycleRequest{ConfigYAML: []byte(yml + "max-file-mb: 2\n")})
	if global.currentSpool() == w || global.currentSpool() == nil {
		t.Fatal("writer not replaced")
	}
	// Disabled: no writer, usage.handle still succeeds quickly.
	call(t, "plugin.reconfigure", lifecycleRequest{ConfigYAML: []byte("enabled: false\n")})
	if global.currentSpool() != nil {
		t.Fatal("writer still running when disabled")
	}
	start := time.Now()
	if _, ok := dispatch("usage.handle", payload); !ok || time.Since(start) > 50*time.Millisecond {
		t.Fatal("usage.handle must succeed fast when disabled")
	}
	call(t, "plugin.register", lifecycleRequest{ConfigYAML: []byte(yml)})
	if env := call(t, "plugin.shutdown", nil); !env.OK || global.currentSpool() != nil {
		t.Fatal("shutdown")
	}
	shutdown() // idempotent
}

func TestDispatchBadInput(t *testing.T) {
	if out, ok := dispatch("nope", nil); ok {
		t.Fatalf("unknown method accepted: %s", out)
	}
	// A garbage lifecycle payload leaves the plugin disabled, never panics.
	env := call(t, "plugin.register", nil)
	if !env.OK || global.currentSpool() != nil {
		t.Fatal("bad payload")
	}
	out, _ := dispatch("plugin.register", []byte("{garbage"))
	if !json.Valid(out) {
		t.Fatal("invalid envelope")
	}
	if out, ok := dispatch("management.handle", []byte("{garbage")); !ok || !json.Valid(out) {
		t.Fatal("management.handle garbage")
	}
}

// The hot path must not wait for a slow writer close during reconfigure.
func TestUsageHandleNotBlockedByReconfigure(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig(dir)
	tune := fastTuning(cfg)
	tune.closeWait = 2 * time.Second
	hang := make(chan struct{})
	tune.beforeWrite = func([]byte) { <-hang }
	w, _ := newSpoolWriterTuned(cfg, tune)
	defer func() { close(hang); <-w.done }()
	global.spool.Store(w)
	w.enqueue(line(1)) // writer now hangs, so close() will wait the full 2s
	time.Sleep(20 * time.Millisecond)
	done := make(chan struct{})
	go func() { global.apply(Config{Enabled: false}); close(done) }()
	time.Sleep(50 * time.Millisecond)
	payload := hostRecord(t, nil)
	start := time.Now()
	for i := 0; i < 100; i++ {
		dispatch("usage.handle", payload)
	}
	if el := time.Since(start); el > 200*time.Millisecond {
		t.Fatalf("usage.handle blocked for %v during reconfigure", el)
	}
	<-done
}
