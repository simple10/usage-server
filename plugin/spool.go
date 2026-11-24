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
}

type spoolStats struct {
	written      atomic.Int64
	dropped      atomic.Int64
	writeErrors  atomic.Int64
	decodeErrors atomic.Int64
	panics       atomic.Int64
}

const (
	spoolPrefix      = "usage-"
	spoolSuffix      = ".jsonl"
	statusFile       = "_plugin_status.json"
	maintainInterval = 30 * time.Second
	statusInterval   = 10 * time.Second
	maxFileAge       = 24 * time.Hour
)

func newSpoolWriter(cfg Config) (*spoolWriter, error) {
	if err := os.MkdirAll(cfg.SpoolDir, 0o755); err != nil {
		return nil, fmt.Errorf("create spool dir %s: %w", cfg.SpoolDir, err)
	}
	w := &spoolWriter{
		cfg:   cfg,
		queue: make(chan []byte, cfg.QueueSize),
		flush: make(chan chan struct{}),
		stop:  make(chan struct{}),
		done:  make(chan struct{}),
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

// close stops the writer, drains what is queued, and waits at most 3 seconds.
func (w *spoolWriter) close() {
	w.once.Do(func() { close(w.stop) })
	select {
	case <-w.done:
	case <-time.After(3 * time.Second):
	}
}

// supervise restarts the run loop after a panic, with a short pause.
func (w *spoolWriter) supervise() {
	defer close(w.done)
	for {
		if w.runSafely() {
			return
		}
		select {
		case <-w.stop:
			return
		case <-time.After(time.Second):
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

func (w *spoolWriter) run() {
	flushEvery := time.Duration(w.cfg.FlushIntervalMS) * time.Millisecond
	flushTick := time.NewTicker(flushEvery)
	defer flushTick.Stop()
	maintainTick := time.NewTicker(maintainInterval)
	defer maintainTick.Stop()
	statusTick := time.NewTicker(statusInterval)
	defer statusTick.Stop()

	var cur *openFile
	defer func() { w.closeFile(cur) }()

	w.maintain("")
	w.writeStatus("")
	for {
		select {
		case line := <-w.queue:
			cur = w.write(cur, line)
		case ack := <-w.flush:
			w.flushFile(cur)
			close(ack)
		case <-flushTick.C:
			w.flushFile(cur)
		case <-maintainTick.C:
			if cur != nil && time.Since(cur.opened) > maxFileAge {
				w.closeFile(cur)
				cur = nil
			}
			w.maintain(currentPath(cur))
		case <-statusTick.C:
			w.writeStatus(currentPath(cur))
		case <-w.stop:
			for {
				select {
				case line := <-w.queue:
					cur = w.write(cur, line)
				default:
					w.flushFile(cur)
					w.writeStatus(currentPath(cur))
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

func (w *spoolWriter) write(cur *openFile, line []byte) *openFile {
	maxBytes := int64(w.cfg.MaxFileMB) * 1024 * 1024
	if cur != nil && cur.size+int64(len(line))+1 > maxBytes {
		w.closeFile(cur)
		cur = nil
	}
	if cur == nil {
		var err error
		cur, err = w.openNew()
		if err != nil {
			w.stats.writeErrors.Add(1)
			w.stats.dropped.Add(1)
			return nil
		}
	}
	if _, err := cur.buf.Write(line); err != nil {
		return w.failFile(cur)
	}
	if err := cur.buf.WriteByte('\n'); err != nil {
		return w.failFile(cur)
	}
	cur.size += int64(len(line)) + 1
	cur.pending++
	w.stats.written.Add(1)
	if cur.pending >= 256 {
		w.flushFile(cur)
	}
	return cur
}

// failFile drops the broken file handle; the next record opens a new file.
func (w *spoolWriter) failFile(cur *openFile) *openFile {
	w.stats.writeErrors.Add(1)
	w.stats.dropped.Add(1)
	if cur != nil && cur.f != nil {
		_ = cur.f.Close()
	}
	return nil
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
	return &openFile{path: path, f: f, buf: bufio.NewWriterSize(f, 64*1024), opened: now}, nil
}

func (w *spoolWriter) flushFile(cur *openFile) {
	if cur == nil || cur.pending == 0 {
		return
	}
	if err := cur.buf.Flush(); err != nil {
		w.stats.writeErrors.Add(1)
		logger.Printf("flush spool file: %v", err)
	}
	cur.pending = 0
}

func (w *spoolWriter) closeFile(cur *openFile) {
	if cur == nil {
		return
	}
	w.flushFile(cur)
	if cur.f != nil {
		_ = cur.f.Close()
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

// writeStatus publishes counters for the server's health view. It writes a
// temp file and renames it, so readers never see a partial file.
func (w *spoolWriter) writeStatus(current string) {
	st := pluginStatus{
		Version:      pluginVersion,
		PID:          os.Getpid(),
		UpdatedAt:    time.Now().UTC().Format(time.RFC3339),
		CurrentFile:  filepath.Base(current),
		QueueLen:     len(w.queue),
		QueueCap:     cap(w.queue),
		Written:      w.stats.written.Load(),
		Dropped:      w.stats.dropped.Load(),
		WriteErrors:  w.stats.writeErrors.Load(),
		DecodeErrors: w.stats.decodeErrors.Load(),
		Panics:       w.stats.panics.Load(),
	}
	if current == "" {
		st.CurrentFile = ""
	}
	raw, err := json.Marshal(st)
	if err != nil {
		return
	}
	tmp := filepath.Join(w.cfg.SpoolDir, statusFile+".tmp")
	if err := os.WriteFile(tmp, raw, 0o644); err != nil {
		return
	}
	_ = os.Rename(tmp, filepath.Join(w.cfg.SpoolDir, statusFile))
}
