package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"strings"
	"time"
)

// hostUsageRecord mirrors the fields of the host's pluginapi.UsageRecord that
// we keep. The host encodes Go field names as JSON keys (no tags).
type hostUsageRecord struct {
	RequestID            string
	TraceID              string
	SelectionID          string
	Provider             string
	BaseURL              string
	ExecutorType         string
	Model                string
	Alias                string
	APIKey               string
	IsNativeKey          bool
	AccessProvider       string
	SessionID            string
	ParentSessionID      string
	AuthID               string
	AuthIndex            string
	AuthType             string
	Source               string
	ReasoningEffort      string
	ServiceTier          string
	EffectiveServiceTier string
	ResponseServiceTier  string
	ResponseModel        string
	Generate             *bool
	Stream               bool
	RequestedAt          time.Time
	Latency              time.Duration
	TTFT                 time.Duration
	Failed               bool
	Failure              struct {
		StatusCode int
		Body       string
	}
	Detail struct {
		InputTokens           int64
		OutputTokens          int64
		ReasoningTokens       int64
		CachedTokens          int64
		CacheReadTokens       int64
		CacheCreationTokens   int64
		TotalTokens           int64
		CacheCreation5mTokens *int64
		CacheCreation1hTokens *int64
	}
	ResponseHeaders http.Header
}

// spoolRecordVersion changes when spoolRecord changes incompatibly.
const spoolRecordVersion = 1

// spoolRecord is one JSON line in the spool. The raw client key never leaves
// the process: it is replaced by a SHA-256 hash and a short hint.
type spoolRecord struct {
	V                    int               `json:"v"`
	RequestID            string            `json:"request_id"`
	TraceID              string            `json:"trace_id,omitempty"`
	TS                   string            `json:"ts"`
	Provider             string            `json:"provider,omitempty"`
	Executor             string            `json:"executor,omitempty"`
	BaseURL              string            `json:"base_url,omitempty"`
	Model                string            `json:"model,omitempty"`
	Alias                string            `json:"alias,omitempty"`
	ResponseModel        string            `json:"response_model,omitempty"`
	APIKeyHash           string            `json:"api_key_hash,omitempty"`
	APIKeyHint           string            `json:"api_key_hint,omitempty"`
	AccessProvider       string            `json:"access_provider,omitempty"`
	SessionID            string            `json:"session_id,omitempty"`
	ParentSessionID      string            `json:"parent_session_id,omitempty"`
	AuthID               string            `json:"auth_id,omitempty"`
	AuthIndex            string            `json:"auth_index,omitempty"`
	AuthType             string            `json:"auth_type,omitempty"`
	Source               string            `json:"source,omitempty"`
	ReasoningEffort      string            `json:"reasoning_effort,omitempty"`
	ServiceTier          string            `json:"service_tier,omitempty"`
	EffectiveServiceTier string            `json:"effective_service_tier,omitempty"`
	Stream               bool              `json:"stream,omitempty"`
	Generate             bool              `json:"generate"`
	Failed               bool              `json:"failed,omitempty"`
	Status               int               `json:"status,omitempty"`
	Error                string            `json:"error,omitempty"`
	LatencyMS            int64             `json:"latency_ms"`
	TTFTMS               int64             `json:"ttft_ms,omitempty"`
	Input                int64             `json:"input"`
	Output               int64             `json:"output"`
	Reasoning            int64             `json:"reasoning,omitempty"`
	Cached               int64             `json:"cached,omitempty"`
	CacheRead            int64             `json:"cache_read,omitempty"`
	CacheWrite           int64             `json:"cache_write,omitempty"`
	CacheWrite5m         *int64            `json:"cache_write_5m,omitempty"`
	CacheWrite1h         *int64            `json:"cache_write_1h,omitempty"`
	Total                int64             `json:"total,omitempty"`
	RateLimit            map[string]string `json:"ratelimit,omitempty"`
}

const maxErrorChars = 512

// keptHeaderPrefixes selects response headers worth keeping, such as the
// subscription utilization headers. Everything else is dropped.
var keptHeaderPrefixes = []string{
	"anthropic-ratelimit-unified-",
	"x-codex-",
}

func hashKey(key string) (hash, hint string) {
	key = strings.TrimSpace(key)
	if key == "" {
		return "", ""
	}
	sum := sha256.Sum256([]byte(key))
	hash = hex.EncodeToString(sum[:])
	if len(key) >= 12 {
		hint = "…" + key[len(key)-4:]
	}
	return hash, hint
}

func toSpoolRecord(in *hostUsageRecord) spoolRecord {
	hash, hint := hashKey(in.APIKey)
	ts := in.RequestedAt
	if ts.IsZero() {
		ts = time.Now()
	}
	generate := true
	if in.Generate != nil {
		generate = *in.Generate
	}
	out := spoolRecord{
		V:                    spoolRecordVersion,
		RequestID:            in.RequestID,
		TraceID:              in.TraceID,
		TS:                   ts.UTC().Format(time.RFC3339Nano),
		Provider:             in.Provider,
		Executor:             in.ExecutorType,
		BaseURL:              in.BaseURL,
		Model:                in.Model,
		Alias:                in.Alias,
		ResponseModel:        in.ResponseModel,
		APIKeyHash:           hash,
		APIKeyHint:           hint,
		AccessProvider:       in.AccessProvider,
		SessionID:            in.SessionID,
		ParentSessionID:      in.ParentSessionID,
		AuthID:               in.AuthID,
		AuthIndex:            in.AuthIndex,
		AuthType:             in.AuthType,
		Source:               in.Source,
		ReasoningEffort:      in.ReasoningEffort,
		ServiceTier:          in.ServiceTier,
		EffectiveServiceTier: in.EffectiveServiceTier,
		Stream:               in.Stream,
		Generate:             generate,
		Failed:               in.Failed,
		LatencyMS:            in.Latency.Milliseconds(),
		TTFTMS:               in.TTFT.Milliseconds(),
		Input:                in.Detail.InputTokens,
		Output:               in.Detail.OutputTokens,
		Reasoning:            in.Detail.ReasoningTokens,
		Cached:               in.Detail.CachedTokens,
		CacheRead:            in.Detail.CacheReadTokens,
		CacheWrite:           in.Detail.CacheCreationTokens,
		CacheWrite5m:         in.Detail.CacheCreation5mTokens,
		CacheWrite1h:         in.Detail.CacheCreation1hTokens,
		Total:                in.Detail.TotalTokens,
	}
	if in.Failed {
		out.Status = in.Failure.StatusCode
		out.Error = truncate(in.Failure.Body, maxErrorChars)
	}
	for name, values := range in.ResponseHeaders {
		lower := strings.ToLower(name)
		for _, prefix := range keptHeaderPrefixes {
			if strings.HasPrefix(lower, prefix) && len(values) > 0 {
				if out.RateLimit == nil {
					out.RateLimit = map[string]string{}
				}
				out.RateLimit[lower] = truncate(values[0], 128)
				break
			}
		}
	}
	return out
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	// Cut on a rune boundary.
	for n > 0 && n < len(s) && (s[n]&0xC0) == 0x80 {
		n--
	}
	return s[:n] + "…"
}

// handleUsage decodes one record and hands it to the spool writer without
// blocking. Any problem drops the record and is counted, never raised.
func handleUsage(payload []byte) {
	w := global.currentSpool()
	if w == nil {
		return
	}
	var in hostUsageRecord
	if err := json.Unmarshal(payload, &in); err != nil {
		w.stats.decodeErrors.Add(1)
		return
	}
	rec := toSpoolRecord(&in)
	line, err := json.Marshal(rec)
	if err != nil {
		w.stats.decodeErrors.Add(1)
		return
	}
	w.enqueue(line)
}
