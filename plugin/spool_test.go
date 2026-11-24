package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func testConfig(dir string) Config {
	cfg := defaultConfig()
	cfg.Enabled = true
	cfg.SpoolDir = dir
	cfg.QueueSize = 64
	cfg.FlushIntervalMS = 10
	return cfg
}

func fastTuning(cfg Config) spoolTuning {
	t := defaultTuning(cfg)
	t.flushEvery = 10 * time.Millisecond
	t.maintainEvery = 20 * time.Millisecond
	t.statusEvery = 20 * time.Millisecond
	t.errorBackoff = 50 * time.Millisecond
	t.restartBackoff = 10 * time.Millisecond
	return t
}

func readAllLines(t *testing.T, dir string) []string {
	t.Helper()
	var out []string
	for _, f := range listSpoolFiles(dir) {
		fh, err := os.Open(f.path)
		if err != nil {
			t.Fatal(err)
		}
		sc := bufio.NewScanner(fh)
		sc.Buffer(make([]byte, 1<<20), 4<<20)
		for sc.Scan() {
			out = append(out, sc.Text())
		}
		fh.Close()
	}
	return out
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func line(i int) []byte { return []byte(fmt.Sprintf(`{"v":1,"request_id":"r%05d"}`, i)) }

func TestSpoolWritesAndCloseDrains(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig(dir)
	cfg.QueueSize = 1000
	w, err := newSpoolWriterTuned(cfg, fastTuning(cfg))
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 500; i++ {
		w.enqueue(line(i))
	}
	w.close()
	lines := readAllLines(t, dir)
	if len(lines) != 500 {
		t.Fatalf("got %d lines", len(lines))
	}
	for i, l := range lines {
		if l != string(line(i)) {
			t.Fatalf("line %d = %s", i, l)
		}
	}
	if w.stats.written.Load() != 500 || w.stats.dropped.Load() != 0 {
		t.Fatalf("stats written=%d dropped=%d", w.stats.written.Load(), w.stats.dropped.Load())
	}
	// Enqueue after close drops and counts, never blocks.
	w.enqueue(line(9999))
	if w.stats.dropped.Load() != 1 {
		t.Fatal("enqueue after close must count a drop")
	}
	var st pluginStatus
	raw, err := os.ReadFile(filepath.Join(dir, statusFile))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(raw, &st); err != nil {
		t.Fatal(err)
	}
	if st.Written != 500 || st.QueueCap != 1000 || st.CurrentFile != "" {
		t.Fatalf("status %+v", st)
	}
	if _, err := os.Stat(filepath.Join(dir, statusFile+".tmp")); !os.IsNotExist(err) {
		t.Fatal("temp status file left behind")
	}
}

func TestSpoolFileNames(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig(dir)
	w, _ := newSpoolWriterTuned(cfg, fastTuning(cfg))
	w.enqueue(line(1))
	w.close()
	files := listSpoolFiles(dir)
	if len(files) != 1 {
		t.Fatalf("files %v", files)
	}
	name := filepath.Base(files[0].path)
	var ts, suffix string
	if n, _ := fmt.Sscanf(strings.TrimSuffix(strings.TrimPrefix(name, "usage-"), ".jsonl"), "%15s-%8s", &ts, &suffix); n != 2 {
		t.Fatalf("bad name %s", name)
	}
	if _, err := time.Parse("20060102T150405", ts); err != nil {
		t.Fatalf("bad timestamp in %s: %v", name, err)
	}
}

func TestSpoolRotatesBySize(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig(dir)
	cfg.MaxFileMB = 1
	cfg.QueueSize = 4096
	w, _ := newSpoolWriterTuned(cfg, fastTuning(cfg))
	big := []byte(`{"pad":"` + strings.Repeat("x", 100*1024) + `"}`)
	for i := 0; i < 25; i++ { // ~2.5 MB
		w.enqueue(big)
	}
	w.close()
	files := listSpoolFiles(dir)
	if len(files) < 3 {
		t.Fatalf("want >=3 files, got %d", len(files))
	}
	for _, f := range files {
		if f.size > 1024*1024 {
			t.Fatalf("%s is %d bytes, over the 1 MB limit", f.path, f.size)
		}
	}
	if got := len(readAllLines(t, dir)); got != 25 {
		t.Fatalf("lines %d", got)
	}
}

func TestSpoolRotatesByAge(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig(dir)
	tune := fastTuning(cfg)
	tune.maxFileAge = 30 * time.Millisecond
	w, _ := newSpoolWriterTuned(cfg, tune)
	w.enqueue(line(1))
	waitFor(t, "first file", func() bool { return len(listSpoolFiles(dir)) == 1 })
	time.Sleep(1100 * time.Millisecond) // next file gets a later second in its name
	w.enqueue(line(2))
	w.close()
	if n := len(listSpoolFiles(dir)); n != 2 {
		t.Fatalf("want 2 files, got %d", n)
	}
}

func writeFile(t *testing.T, path string, size int, age time.Duration) {
	t.Helper()
	if err := os.WriteFile(path, make([]byte, size), 0o644); err != nil {
		t.Fatal(err)
	}
	mt := time.Now().Add(-age)
	if err := os.Chtimes(path, mt, mt); err != nil {
		t.Fatal(err)
	}
}

func TestSpoolRetention(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig(dir)
	cfg.RetentionDays = 2
	cfg.MaxTotalMB = 1
	w := &spoolWriter{cfg: cfg, queue: make(chan []byte, 1)}
	day := 24 * time.Hour
	old := filepath.Join(dir, "usage-20260101T000000-aaaaaaaa.jsonl")
	a := filepath.Join(dir, "usage-20260102T000000-bbbbbbbb.jsonl")
	b := filepath.Join(dir, "usage-20260103T000000-cccccccc.jsonl")
	open := filepath.Join(dir, "usage-20260104T000000-dddddddd.jsonl")
	other := filepath.Join(dir, "notes.txt")
	writeFile(t, old, 10, 3*day)
	writeFile(t, a, 600*1024, day)
	writeFile(t, b, 300*1024, time.Hour)
	writeFile(t, open, 900*1024, 10*day) // open file: never deleted, even when old
	writeFile(t, other, 10, 30*day)
	w.maintain(open)
	exists := func(p string) bool { _, err := os.Stat(p); return err == nil }
	if exists(old) {
		t.Fatal("file past retention kept")
	}
	if !exists(open) {
		t.Fatal("open file deleted")
	}
	if exists(a) || exists(b) {
		t.Fatal("size cap: both closed files should go (open file alone is 900 KB of 1 MB)")
	}
	if !exists(other) {
		t.Fatal("non-spool file deleted")
	}
}

func TestSpoolRetentionKeepsNewestUnderCap(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig(dir)
	cfg.MaxTotalMB = 1
	w := &spoolWriter{cfg: cfg, queue: make(chan []byte, 1)}
	a := filepath.Join(dir, "usage-20260102T000000-bbbbbbbb.jsonl")
	b := filepath.Join(dir, "usage-20260103T000000-cccccccc.jsonl")
	writeFile(t, a, 600*1024, time.Hour)
	writeFile(t, b, 600*1024, time.Hour)
	w.maintain("")
	if _, err := os.Stat(a); !os.IsNotExist(err) {
		t.Fatal("oldest file should be deleted first")
	}
	if _, err := os.Stat(b); err != nil {
		t.Fatal("newest file should be kept")
	}
}

func TestSpoolQueueOverflowDropsWithoutBlocking(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig(dir)
	cfg.QueueSize = 8
	tune := fastTuning(cfg)
	release := make(chan struct{})
	var once sync.Once
	tune.beforeWrite = func([]byte) { <-release } // stall the writer
	w, _ := newSpoolWriterTuned(cfg, tune)
	start := time.Now()
	for i := 0; i < 1000; i++ {
		w.enqueue(line(i))
	}
	if el := time.Since(start); el > 200*time.Millisecond {
		t.Fatalf("enqueue blocked for %v", el)
	}
	dropped := w.stats.dropped.Load()
	// One record may be held by the stalled writer, plus 8 in the queue.
	if dropped < 1000-9 || dropped > 1000-8 {
		t.Fatalf("dropped %d", dropped)
	}
	once.Do(func() { close(release) })
	w.close()
	if got := int64(len(readAllLines(t, dir))); got+dropped != 1000 {
		t.Fatalf("written %d + dropped %d != 1000", got, dropped)
	}
}

func TestSpoolCloseIsBoundedWhenWriterHangs(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig(dir)
	tune := fastTuning(cfg)
	tune.closeWait = 100 * time.Millisecond
	hang := make(chan struct{})
	tune.beforeWrite = func([]byte) { <-hang }
	w, _ := newSpoolWriterTuned(cfg, tune)
	defer func() { close(hang); <-w.done }()
	w.enqueue(line(1))
	time.Sleep(20 * time.Millisecond)
	start := time.Now()
	w.close()
	w.flushNow()
	if el := time.Since(start); el > 1500*time.Millisecond {
		t.Fatalf("close+flush took %v", el)
	}
}

func TestSpoolRecoversFromPanic(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig(dir)
	tune := fastTuning(cfg)
	tune.beforeWrite = func(l []byte) {
		if strings.Contains(string(l), "r00003") {
			panic("boom")
		}
	}
	w, _ := newSpoolWriterTuned(cfg, tune)
	for i := 0; i < 3; i++ {
		w.enqueue(line(i))
	}
	waitFor(t, "first lines", func() bool { return w.stats.written.Load() == 3 })
	w.enqueue(line(3)) // panics; lost
	waitFor(t, "panic counted", func() bool { return w.stats.panics.Load() == 1 })
	for i := 4; i < 10; i++ {
		w.enqueue(line(i))
	}
	w.close()
	lines := readAllLines(t, dir)
	if len(lines) != 9 {
		t.Fatalf("want 9 lines after restart, got %d: %v", len(lines), lines)
	}
	for _, l := range lines {
		if strings.Contains(l, "r00003") {
			t.Fatal("panicking record written")
		}
	}
}

func TestSpoolDirMissingCountsErrorsAndBacksOff(t *testing.T) {
	base := t.TempDir()
	dir := filepath.Join(base, "spool")
	cfg := testConfig(dir)
	tune := fastTuning(cfg)
	tune.errorBackoff = time.Hour
	w, _ := newSpoolWriterTuned(cfg, tune)
	time.Sleep(30 * time.Millisecond)
	if err := os.RemoveAll(dir); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 50; i++ {
		w.enqueue(line(i))
	}
	waitFor(t, "drops", func() bool { return w.stats.dropped.Load() == 50 })
	if e := w.stats.writeErrors.Load(); e != 1 {
		t.Fatalf("want exactly one open attempt during back-off, got %d errors", e)
	}
	w.close()
}

func TestSpoolDiskFullDropsHandleAndRemovesEmptyFile(t *testing.T) {
	devFull, err := os.OpenFile("/dev/full", os.O_WRONLY, 0)
	if err != nil {
		t.Skip("/dev/full not available")
	}
	dir := t.TempDir()
	cfg := testConfig(dir)
	w := &spoolWriter{cfg: cfg, queue: make(chan []byte, 1), tune: fastTuning(cfg)}
	empty := filepath.Join(dir, "usage-20260101T000000-00000000.jsonl")
	writeFile(t, empty, 0, 0)
	st := &writerState{cur: &openFile{path: empty, f: devFull, buf: bufio.NewWriterSize(devFull, 64), opened: time.Now()}}
	w.write(st, line(1))
	w.write(st, line(2))
	w.flushFile(st)
	if st.cur != nil {
		t.Fatal("broken handle kept")
	}
	if w.stats.writeErrors.Load() < 1 || w.stats.dropped.Load() != 2 || w.stats.written.Load() != 0 {
		t.Fatalf("errors=%d dropped=%d written=%d", w.stats.writeErrors.Load(), w.stats.dropped.Load(), w.stats.written.Load())
	}
	if _, err := os.Stat(empty); !os.IsNotExist(err) {
		t.Fatal("empty failed file left behind")
	}
	// During back-off, records are dropped without opening files.
	w.write(st, line(3))
	if st.cur != nil || len(listSpoolFiles(dir)) != 0 {
		t.Fatal("opened a file during back-off")
	}
	time.Sleep(60 * time.Millisecond)
	w.write(st, line(4))
	if st.cur == nil {
		t.Fatal("did not reopen after back-off")
	}
	w.closeFile(st)
	if got := readAllLines(t, dir); len(got) != 1 || got[0] != string(line(4)) {
		t.Fatalf("lines %v", got)
	}
}

func TestSpoolConcurrentEnqueue(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig(dir)
	cfg.QueueSize = 100000
	w, _ := newSpoolWriterTuned(cfg, fastTuning(cfg))
	var wg sync.WaitGroup
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for i := 0; i < 500; i++ {
				w.enqueue(line(g*1000 + i))
				if i%100 == 0 {
					w.flushNow()
				}
			}
		}(g)
	}
	wg.Wait()
	w.close()
	if n := len(readAllLines(t, dir)); n != 4000 {
		t.Fatalf("lines %d", n)
	}
}
