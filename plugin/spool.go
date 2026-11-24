package main

import (
	"bufio"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// spoolWriter appends usage records to JSON-lines files in a directory.
//
// The host thread only does a non-blocking channel send. One background
// goroutine owns the open file: it batches writes, rotates by size and age,
// deletes old closed files, and publishes a small status file. If the queue is
// full or the disk fails, records are dropped and counted. Nothing blocks the
// proxy and nothing panics out of this goroutine.
type spoolWriter struct {
	cfg   Config
	queue chan []byte
	flush chan chan struct{}
	stop  chan struct{}
	done  chan struct{}
	once  sync.Once
	stats spoolStats
	tune  spoolTuning
}

// spoolTuning holds timings that tests shorten. Production uses defaults.
type spoolTuning struct {
	flushEvery     time.Duration
	maintainEvery  time.Duration
	statusEvery    time.Duration
	maxFileAge     time.Duration
	errorBackoff   time.Duration
	restartBackoff time.Duration
	closeWait      time.Duration
	// beforeWrite, when set, runs in the writer goroutine before each record.
	// Tests use it to inject panics and stalls.
	beforeWrite func(line []byte)
}

type spoolStats struct {
	written      atomic.Int64
	dropped      atomic.Int64
	writeErrors  atomic.Int64
	decodeErrors atomic.Int64
	panics       atomic.Int64
}

const (
	spoolPrefix   = "usage-"
	spoolSuffix   = ".jsonl"
	statusFile    = "_plugin_status.json"
	flushBatch    = 256
	writeBufBytes = 64 * 1024
)

func defaultTuning(cfg Config) spoolTuning {
	return spoolTuning{
		flushEvery:     time.Duration(cfg.FlushIntervalMS) * time.Millisecond,
		maintainEvery:  30 * time.Second,
		statusEvery:    10 * time.Second,
		maxFileAge:     24 * time.Hour,
		errorBackoff:   5 * time.Second,
		restartBackoff: time.Second,
		closeWait:      3 * time.Second,
	}
}

func newSpoolWriter(cfg Config) (*spoolWriter, error) {
	return newSpoolWriterTuned(cfg, defaultTuning(cfg))
}

func newSpoolWriterTuned(cfg Config, tune spoolTuning) (*spoolWriter, error) {
	if err := os.MkdirAll(cfg.SpoolDir, 0o755); err != nil {
		return nil, fmt.Errorf("create spool dir %s: %w", cfg.SpoolDir, err)
	}
	w := &spoolWriter{
		cfg:   cfg,
		queue: make(chan []byte, cfg.QueueSize),
		flush: make(chan chan struct{}),
		stop:  make(chan struct{}),
		done:  make(chan struct{}),
		tune:  tune,
	}
	go w.supervise()
	return w, nil
}

func (w *spoolWriter) cfgChanged(cfg Config) bool {
	c := w.cfg
	return c.SpoolDir != cfg.SpoolDir || c.MaxFileMB != cfg.MaxFileMB || c.QueueSize != cfg.QueueSize ||
		c.RetentionDays != cfg.RetentionDays || c.MaxTotalMB != cfg.MaxTotalMB || c.FlushIntervalMS != cfg.FlushIntervalMS
}

// enqueue never blocks. A full queue drops the record.
func (w *spoolWriter) enqueue(line []byte) {
	select {
	case <-w.stop:
		w.stats.dropped.Add(1)
		return
	default:
	}
	select {
	case w.queue <- line:
	default:
		w.stats.dropped.Add(1)
	}
}

// flushNow asks the writer to flush and waits at most one second.
func (w *spoolWriter) flushNow() {
	ack := make(chan struct{})
	select {
	case w.flush <- ack:
	case <-w.done:
		return
	case <-time.After(time.Second):
		return
	}
	select {
	case <-ack:
	case <-time.After(time.Second):
	}
}

// close stops the writer, drains what is queued, and waits at most closeWait.
func (w *spoolWriter) close() {
	w.once.Do(func() { close(w.stop) })
	select {
	case <-w.done:
	case <-time.After(w.tune.closeWait):
	}
}

// supervise restarts the run loop after a panic, with a short pause.
func (w *spoolWriter) supervise() {
	defer close(w.done)
	defer func() { _ = recover() }() // last line of defence; run() recovers itself
	for {
		if w.runSafely() {
			return
		}
		select {
		case <-w.stop:
			// Drain what we can after a panic during shutdown, without hooks.
			w.tune.beforeWrite = nil
			w.runSafely()
			return
		case <-time.After(w.tune.restartBackoff):
		}
	}
}

func (w *spoolWriter) runSafely() (stopped bool) {
	defer func() {
		if r := recover(); r != nil {
			w.stats.panics.Add(1)
			logger.Printf("spool writer recovered from panic: %v", r)
			stopped = false
		}
	}()
	w.run()
	return true
}

type openFile struct {
	path    string
	f       *os.File
	buf     *bufio.Writer
	size    int64
	opened  time.Time
	pending int
}

// writerState is owned by the run goroutine.
type writerState struct {
	cur        *openFile
	retryAfter time.Time // after a disk error, drop records until then
}

func (w *spoolWriter) run() {
	flushTick := time.NewTicker(w.tune.flushEvery)
	defer flushTick.Stop()
	maintainTick := time.NewTicker(w.tune.maintainEvery)
	defer maintainTick.Stop()
	statusTick := time.NewTicker(w.tune.statusEvery)
	defer statusTick.Stop()

	st := &writerState{}
	defer func() { w.closeFile(st) }()

	w.maintain("")
	w.writeStatus("")
	for {
		select {
		case line := <-w.queue:
			w.write(st, line)
		case ack := <-w.flush:
			w.flushFile(st)
			close(ack)
		case <-flushTick.C:
			w.flushFile(st)
		case <-maintainTick.C:
			if st.cur != nil && time.Since(st.cur.opened) > w.tune.maxFileAge {
				w.closeFile(st)
			}
			w.maintain(currentPath(st.cur))
		case <-statusTick.C:
			w.writeStatus(currentPath(st.cur))
		case <-w.stop:
			for {
				select {
				case line := <-w.queue:
					w.write(st, line)
				default:
					w.closeFile(st)
					w.writeStatus("")
					return
				}
			}
		}
	}
}

func currentPath(cur *openFile) string {
	if cur == nil {
		return ""
	}
	return cur.path
}

func (w *spoolWriter) write(st *writerState, line []byte) {
	if hook := w.tune.beforeWrite; hook != nil {
		hook(line)
	}
	if st.cur == nil && time.Now().Before(st.retryAfter) {
		w.stats.dropped.Add(1)
		return
	}
	maxBytes := int64(w.cfg.MaxFileMB) * 1024 * 1024
	if st.cur != nil && st.cur.size > 0 && st.cur.size+int64(len(line))+1 > maxBytes {
		w.closeFile(st)
	}
	if st.cur == nil {
		cur, err := w.openNew()
		if err != nil {
			w.stats.writeErrors.Add(1)
			w.stats.dropped.Add(1)
			st.retryAfter = time.Now().Add(w.tune.errorBackoff)
			return
		}
		st.cur = cur
	}
	cur := st.cur
	if _, err := cur.buf.Write(line); err != nil {
		w.failFile(st, err, 1)
		return
	}
	if err := cur.buf.WriteByte('\n'); err != nil {
		w.failFile(st, err, 1)
		return
	}
	cur.size += int64(len(line)) + 1
	cur.pending++
	if cur.pending >= flushBatch {
		w.flushFile(st)
	}
}

// failFile drops the broken file handle and counts the records that were
// buffered but not yet on disk. New records are dropped for errorBackoff,
// then the next record opens a new file.
func (w *spoolWriter) failFile(st *writerState, err error, extra int) {
	cur := st.cur
	st.cur = nil
	st.retryAfter = time.Now().Add(w.tune.errorBackoff)
	w.stats.writeErrors.Add(1)
	lost := int64(extra)
	if cur != nil {
		lost += int64(cur.pending)
		logger.Printf("spool write failed on %s: %v", filepath.Base(cur.path), err)
		if cur.f != nil {
			_ = cur.f.Close()
		}
		// A file that never got any data (typical on a full disk) is removed,
		// so repeated failures do not leave empty files behind.
		if info, statErr := os.Stat(cur.path); statErr == nil && info.Size() == 0 {
			_ = os.Remove(cur.path)
		}
	}
	w.stats.dropped.Add(lost)
}

func (w *spoolWriter) openNew() (*openFile, error) {
	now := time.Now().UTC()
	suffix := make([]byte, 4)
	_, _ = rand.Read(suffix)
	name := fmt.Sprintf("%s%s-%s%s", spoolPrefix, now.Format("20060102T150405"), hex.EncodeToString(suffix), spoolSuffix)
	path := filepath.Join(w.cfg.SpoolDir, name)
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND|os.O_EXCL, 0o644)
	if err != nil {
		logger.Printf("open spool file: %v", err)
		return nil, err
	}
	return &openFile{path: path, f: f, buf: bufio.NewWriterSize(f, writeBufBytes), opened: now}, nil
}

// flushFile writes buffered records to the file. Records count as written
// only once they reach the file.
func (w *spoolWriter) flushFile(st *writerState) {
	cur := st.cur
	if cur == nil || cur.pending == 0 {
		return
	}
	if err := cur.buf.Flush(); err != nil {
		w.failFile(st, err, 0)
		return
	}
	w.stats.written.Add(int64(cur.pending))
	cur.pending = 0
}

func (w *spoolWriter) closeFile(st *writerState) {
	if st.cur == nil {
		return
	}
	w.flushFile(st)
	if st.cur != nil {
		if st.cur.f != nil {
			_ = st.cur.f.Close()
		}
		st.cur = nil
	}
}

type spoolFile struct {
	path    string
	size    int64
	modTime time.Time
}

func listSpoolFiles(dir string) []spoolFile {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var out []spoolFile
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasPrefix(name, spoolPrefix) || !strings.HasSuffix(name, spoolSuffix) {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		out = append(out, spoolFile{path: filepath.Join(dir, name), size: info.Size(), modTime: info.ModTime()})
	}
	// Names start with a UTC timestamp, so name order is age order.
	sort.Slice(out, func(i, j int) bool { return out[i].path < out[j].path })
	return out
}

// maintain deletes closed files past the retention age, then the oldest
// closed files while the total size is above the cap. The open file is kept.
func (w *spoolWriter) maintain(current string) {
	files := listSpoolFiles(w.cfg.SpoolDir)
	cutoff := time.Now().Add(-time.Duration(w.cfg.RetentionDays) * 24 * time.Hour)
	maxTotal := int64(w.cfg.MaxTotalMB) * 1024 * 1024
	var total int64
	kept := files[:0]
	for _, f := range files {
		if f.path != current && f.modTime.Before(cutoff) {
			_ = os.Remove(f.path)
			continue
		}
		total += f.size
		kept = append(kept, f)
	}
	for _, f := range kept {
		if total <= maxTotal {
			break
		}
		if f.path == current {
			continue
		}
		if os.Remove(f.path) == nil {
			total -= f.size
		}
	}
}

type pluginStatus struct {
	Version      string `json:"version"`
	PID          int    `json:"pid"`
	UpdatedAt    string `json:"updated_at"`
	CurrentFile  string `json:"current_file,omitempty"`
	QueueLen     int    `json:"queue_len"`
	QueueCap     int    `json:"queue_cap"`
	Written      int64  `json:"written"`
	Dropped      int64  `json:"dropped"`
	WriteErrors  int64  `json:"write_errors"`
	DecodeErrors int64  `json:"decode_errors"`
	Panics       int64  `json:"panics"`
}

func (w *spoolWriter) snapshot(current string) pluginStatus {
	st := pluginStatus{
		Version:      pluginVersion,
		PID:          os.Getpid(),
		UpdatedAt:    time.Now().UTC().Format(time.RFC3339),
		QueueLen:     len(w.queue),
		QueueCap:     cap(w.queue),
		Written:      w.stats.written.Load(),
		Dropped:      w.stats.dropped.Load(),
		WriteErrors:  w.stats.writeErrors.Load(),
		DecodeErrors: w.stats.decodeErrors.Load(),
		Panics:       w.stats.panics.Load() + global.exportPanics.Load(),
	}
	if current != "" {
		st.CurrentFile = filepath.Base(current)
	}
	return st
}

// writeStatus publishes counters for the server's health view. It writes a
// temp file and renames it, so readers never see a partial file.
func (w *spoolWriter) writeStatus(current string) {
	raw, err := json.Marshal(w.snapshot(current))
	if err != nil {
		return
	}
	tmp := filepath.Join(w.cfg.SpoolDir, statusFile+".tmp")
	if err := os.WriteFile(tmp, raw, 0o644); err != nil {
		_ = os.Remove(tmp)
		return
	}
	_ = os.Rename(tmp, filepath.Join(w.cfg.SpoolDir, statusFile))
}
