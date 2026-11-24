package main

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"strings"
	"sync"
	"time"

	"gopkg.in/yaml.v3"
)

// pluginName is shown in the proxy's plugin list. The plugin ID the host uses
// comes from the library's file name (usage-server.so → "usage-server").
const (
	pluginName    = "usage-server"
	pluginVersion = "0.1.0"
	// schemaVersion 6 is the first host schema that returns plugin management
	// JSON bodies without HTML-escaping. We need nothing newer.
	schemaVersion = 6
)

// Config is the YAML block under plugins.configs.<id> in the proxy config.
type Config struct {
	Enabled bool `yaml:"enabled"`

	// SpoolDir is where usage records are appended as JSON lines.
	SpoolDir string `yaml:"spool-dir"`
	// MaxFileMB starts a new spool file once the current one reaches this size.
	MaxFileMB int `yaml:"max-file-mb"`
	// RetentionDays deletes closed spool files older than this.
	RetentionDays int `yaml:"retention-days"`
	// MaxTotalMB deletes the oldest closed spool files above this total size.
	MaxTotalMB int `yaml:"max-total-mb"`
	// QueueSize bounds the in-memory queue between the host and the writer.
	QueueSize int `yaml:"queue-size"`
	// FlushIntervalMS flushes buffered records at least this often.
	FlushIntervalMS int `yaml:"flush-interval-ms"`

	// ServerURL is the usage server the management bridge forwards to.
	// Empty disables the bridge routes and the dashboard menu.
	ServerURL string `yaml:"server-url"`
	// BridgeToken is sent to the server so it can trust bridged requests.
	BridgeToken string `yaml:"bridge-token"`
	// BridgeTimeoutMS bounds every bridged request.
	BridgeTimeoutMS int `yaml:"bridge-timeout-ms"`
	// MenuLabel is the label of the dashboard entry in the management panel.
	MenuLabel string `yaml:"menu-label"`
}

func defaultConfig() Config {
	return Config{
		SpoolDir:        "usage-spool",
		MaxFileMB:       64,
		RetentionDays:   14,
		MaxTotalMB:      1024,
		QueueSize:       8192,
		FlushIntervalMS: 1000,
		BridgeTimeoutMS: 5000,
		MenuLabel:       "Usage",
	}
}

func parseConfig(raw []byte) (Config, error) {
	cfg := defaultConfig()
	if len(strings.TrimSpace(string(raw))) > 0 {
		if err := yaml.Unmarshal(raw, &cfg); err != nil {
			return defaultConfig(), fmt.Errorf("parse plugin config: %w", err)
		}
	}
	d := defaultConfig()
	if cfg.SpoolDir == "" {
		cfg.SpoolDir = d.SpoolDir
	}
	if cfg.MaxFileMB <= 0 {
		cfg.MaxFileMB = d.MaxFileMB
	}
	if cfg.RetentionDays <= 0 {
		cfg.RetentionDays = d.RetentionDays
	}
	if cfg.MaxTotalMB <= 0 {
		cfg.MaxTotalMB = d.MaxTotalMB
	}
	if cfg.QueueSize <= 0 {
		cfg.QueueSize = d.QueueSize
	}
	if cfg.FlushIntervalMS <= 0 {
		cfg.FlushIntervalMS = d.FlushIntervalMS
	}
	if cfg.BridgeTimeoutMS <= 0 {
		cfg.BridgeTimeoutMS = d.BridgeTimeoutMS
	}
	if cfg.MenuLabel == "" {
		cfg.MenuLabel = d.MenuLabel
	}
	cfg.ServerURL = strings.TrimRight(strings.TrimSpace(cfg.ServerURL), "/")
	return cfg, nil
}

// state is the plugin's process-wide state. The host may call into the plugin
// from many goroutines at once, so every field is guarded.
type state struct {
	mu     sync.Mutex
	cfg    Config
	spool  *spoolWriter
	bridge *bridge
}

var global state

var logger = log.New(os.Stderr, "[usage-server plugin] ", log.LstdFlags)

type envelope struct {
	OK     bool            `json:"ok"`
	Result json.RawMessage `json:"result,omitempty"`
	Error  *envelopeError  `json:"error,omitempty"`
}

type envelopeError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func okEnvelope(result any) []byte {
	raw, err := json.Marshal(result)
	if err != nil {
		return errorEnvelope("encode_failed", err.Error())
	}
	out, _ := json.Marshal(envelope{OK: true, Result: raw})
	return out
}

func errorEnvelope(code, message string) []byte {
	out, _ := json.Marshal(envelope{OK: false, Error: &envelopeError{Code: code, Message: message}})
	return out
}

// dispatch handles one host call. It never panics (callers also recover) and
// reports ok=false only for calls the host should treat as failed.
func dispatch(method string, payload []byte) ([]byte, bool) {
	switch method {
	case "plugin.register", "plugin.reconfigure":
		return lifecycle(payload), true
	case "plugin.quiesce":
		global.flush()
		return okEnvelope(struct{}{}), true
	case "plugin.shutdown":
		shutdown()
		return okEnvelope(struct{}{}), true
	case "usage.handle":
		handleUsage(payload)
		// Always succeed: a failed usage call only makes the host log noise.
		return okEnvelope(struct{}{}), true
	case "management.register":
		return okEnvelope(global.managementRoutes()), true
	case "management.handle":
		return okEnvelope(global.handleManagement(payload)), true
	default:
		return errorEnvelope("unknown_method", "unknown method: "+method), false
	}
}

type lifecycleRequest struct {
	ConfigYAML    []byte `json:"config_yaml"`
	SchemaVersion uint32 `json:"schema_version"`
}

type registration struct {
	SchemaVersion uint32         `json:"schema_version"`
	Metadata      map[string]any `json:"metadata"`
	Capabilities  map[string]any `json:"capabilities"`
}

func lifecycle(payload []byte) []byte {
	var req lifecycleRequest
	if len(payload) > 0 {
		if err := json.Unmarshal(payload, &req); err != nil {
			logger.Printf("bad lifecycle payload: %v", err)
		}
	}
	cfg, err := parseConfig(req.ConfigYAML)
	if err != nil {
		logger.Printf("%v (using defaults)", err)
	}
	global.apply(cfg)
	return okEnvelope(registration{
		SchemaVersion: schemaVersion,
		Metadata: map[string]any{
			"Name":             pluginName,
			"Version":          pluginVersion,
			"Author":           "usage-server",
			"GitHubRepository": "",
			"Logo":             "",
			"ConfigFields":     []any{},
		},
		Capabilities: map[string]any{
			"usage_plugin":   true,
			"management_api": true,
		},
	})
}

// apply starts, stops or reconfigures the spool writer and the bridge.
func (s *state) apply(cfg Config) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.cfg = cfg
	if !cfg.Enabled {
		if s.spool != nil {
			s.spool.close()
			s.spool = nil
		}
		s.bridge = nil
		return
	}
	if s.spool == nil || s.spool.cfgChanged(cfg) {
		if s.spool != nil {
			s.spool.close()
		}
		w, err := newSpoolWriter(cfg)
		if err != nil {
			logger.Printf("spool disabled: %v", err)
			s.spool = nil
		} else {
			s.spool = w
		}
	}
	if cfg.ServerURL != "" {
		s.bridge = newBridge(cfg.ServerURL, cfg.BridgeToken, time.Duration(cfg.BridgeTimeoutMS)*time.Millisecond)
	} else {
		s.bridge = nil
	}
}

func (s *state) currentSpool() *spoolWriter {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.spool
}

func (s *state) currentBridge() (*bridge, Config) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.bridge, s.cfg
}

func (s *state) flush() {
	if w := s.currentSpool(); w != nil {
		w.flushNow()
	}
}

func shutdown() {
	global.mu.Lock()
	defer global.mu.Unlock()
	if global.spool != nil {
		global.spool.close()
		global.spool = nil
	}
	global.bridge = nil
}
