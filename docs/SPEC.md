# usage-server — specification

Status: draft v0.1. The plugin is a first draft that has never been built or tested. The server does not exist yet.

## 1. Goal

Show token usage and estimated cost for traffic that goes through [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) ("the proxy"). Break it down by:

- proxy client key (the key a client uses to connect to the proxy)
- upstream credential (a subscription OAuth account or an API key)
- provider and model
- session
- project, skill and subagent, for sessions whose transcripts are on the same host

The work is in two parts:

1. **Observe only.** The system never blocks, delays or changes a proxied request.
2. **Never put the proxy at risk.** No failure in this system may crash, hang or slow down the proxy.

Out of scope for v1:
- routing or limit enforcement
- budget tracking for API keys
- harnesses other than Claude Code. The design must leave room for them (see §8).

## 2. Architecture

```
 clients ──► CLIProxyAPI ──► upstream providers
               │   ▲
  usage.handle │   │ /v0/management/usage-server/api  (proxy checks the management key)
               ▼   │
         usage-server plugin (.so, Go)
          - spool writer: hash key → JSONL files (non-blocking)
          - bridge: dashboard menu + forward API calls (timeout)
               │                     │
     spool dir │ (shared volume)     │ HTTP on the private network
               ▼                     ▼
         usage-server (Python, container)
          - spool reader → SQLite   (the only writer to the DB)
          - models.dev price sync
          - transcript scanner → parser subprocesses (crash and timeout isolated)
          - JSON API + dashboard (standalone, or inside the proxy panel)
               │
          usage.db      /transcripts/<harness>/   (read-only)
```

Why this split:

- **The plugin is small and does no network I/O on the request path.** A crash in native plugin code kills the proxy, and the host sets no timeout on plugin calls. So the plugin only queues records in memory and appends them to a file.
- **A spool file keeps the two processes apart.** The server can be down for days and lose nothing, up to the plugin's retention limit. The proxy does not notice when the server is down.
- **Each piece of data has one writer.** The plugin writes the spool files. The server only reads them, and the server is the only writer to SQLite.
- **Risky work runs in subprocesses.** Transcript parsing runs in a subprocess with a timeout, so a bad transcript can crash or hang only that subprocess.

## 3. Facts about the proxy host (verified in CLIProxyAPI v8.0.80 source)

- **Plugin format.** A plugin is a native shared library that the host loads with `dlopen`.
  - It exports `cliproxy_plugin_init(host_api*, plugin_api*)`.
  - Every call is `call(method, json) → {ok, result, error}`.
  - ABI version 1 must match exactly.
  - The schema version is negotiated. We declare 6, the first version that returns management JSON bodies without HTML-escaping.
  - The proxy must be built with cgo. The official image is.
- **Plugin ID and config.**
  - The plugin ID is the file name: `usage-server.so` gives `usage-server`.
  - The host looks for the file in `<plugins.dir>/<goos>/<goarch>/` first, then in `<plugins.dir>/`.
  - The plugin receives the YAML subtree under `plugins.configs.usage-server` as `config_yaml` (base64 in JSON).
- **Methods this plugin uses:**
  - `plugin.register`, `plugin.reconfigure`, `plugin.quiesce`, `plugin.shutdown`
  - `usage.handle`
  - `management.register`, `management.handle`
- **The `usage.handle` payload** is `pluginapi.UsageRecord`, with Go field names as JSON keys (the struct has no JSON tags). It includes:
  - `APIKey`: the raw client key. It must never be written as is.
  - `AuthID`, `AuthIndex`, `AuthType`
  - `Provider`, `BaseURL`, `Model`, `Alias`, `ResponseModel`
  - `SessionID`, `ParentSessionID`
  - `RequestedAt`, `Latency` and `TTFT` (durations in nanoseconds)
  - `Failed` and `Failure{StatusCode, Body}`
  - `Detail{InputTokens, OutputTokens, ReasoningTokens, CachedTokens, CacheReadTokens, CacheCreationTokens, TotalTokens, CacheCreation5mTokens*, CacheCreation1hTokens*}`
  - `ResponseHeaders`
- **Usage delivery.**
  - Records arrive after the request finishes, through one shared dispatcher worker with a 4,096-record buffer for each plugin.
  - A slow `usage.handle` stalls usage delivery for **every** consumer. So the handler must return at once.
- **Management routes.**
  - Routes must be exact paths under `/v0/management/`. Wildcards and `:` are not allowed.
  - The host checks the management key before it calls the plugin.
  - The request passed to the plugin is `{Method, Path, Headers, Query, Body(base64)}`.
  - The response is `{StatusCode, Headers, Body(base64)}`.
- **Resource routes.**
  - They live under `/v0/resource/plugins/<id>/<path>` and are **not** authenticated.
  - A resource with a `Menu` label shows up in the management panel. The panel shows the page in an iframe on the same origin, with no `sandbox` attribute.
- **Failure behaviour.**
  - A Go panic on the host side of a call disables the plugin ("fuses" it), and requests continue.
  - A crash in native plugin code (a segfault, or a Go panic that escapes an export) **kills the proxy process**.
  - Plugin calls have no timeouts.
- **Hot reload.**
  - A config reload calls `plugin.reconfigure`.
  - When the library file is replaced, the old image stays loaded on purpose. Restart the proxy after you upgrade the plugin.
- **Session IDs.**
  - The host fills `SessionID` from client session headers. Claude Code sends one.
  - It is not yet confirmed whether the value equals the Claude Code transcript file name, or carries a prefix such as `claude:`. Test this in phase 0.
- **Built-in alternative we decided not to use.**
  - `observability.usage.usage-statistics-enabled` turns on a built-in queue that is read with `GET /v0/management/observability/usage/queue`.
  - Reading a record removes it, and records last at most 1 hour.
  - Its records have a few fields the plugin hook does not get: `node_kind`, `is_fork`, `is_compaction` and `endpoint`.

## 4. Plugin (`plugin/`, Go)

### 4.1 Config

The config lives under `plugins.configs.usage-server` in the proxy config:

```yaml
plugins:
  enabled: true
  dir: plugins
  configs:
    usage-server:
      enabled: true
      spool-dir: /CLIProxyAPI/usage-spool   # shared volume
      max-file-mb: 64                       # rotate at this size (also every 24h)
      retention-days: 14                    # delete closed files older than this
      max-total-mb: 1024                    # delete oldest closed files above this
      queue-size: 8192                      # in-memory queue; overflow is dropped and counted
      flush-interval-ms: 1000
      server-url: http://usage-server:8080  # empty disables the bridge and the menu
      bridge-token: <random secret shared with the server>
      bridge-timeout-ms: 5000
      menu-label: Usage
```

### 4.2 Spool writer rules

1. `usage.handle` decodes the record, hashes the key, encodes one JSON line, and does a **non-blocking** channel send. If the queue is full, it drops the record and adds 1 to `dropped`.
2. One goroutine owns the file. It batches writes, flushes every second or every 256 records, and rotates at the size limit or after 24 hours.
3. File names are `usage-<UTC yyyymmddThhmmss>-<8 hex>.jsonl`. Sorting by name sorts by age. The random suffix stops two plugin instances from writing the same file.
4. Retention: delete closed files past `retention-days`, then delete the oldest closed files while the total is above `max-total-mb`. Never delete the open file.
5. Every 10 seconds, write `_plugin_status.json` with these counters: written, dropped, write errors, decode errors, panics, queue length. Write it to a temp file and rename it, so readers never see a partial file.
6. Every exported function and every goroutine recovers from panics. The writer goroutine restarts after a panic.
7. On a disk error, drop the file handle, count the error, and open a new file on the next record.
8. `plugin.shutdown` and `enabled: false` drain the queue and close the file, waiting at most 3 seconds.

### 4.3 Spool record (v1)

The record is one JSON object on one line. These field names are the contract between the plugin and the server.

| field | notes |
|---|---|
| `v` | record version, `1` |
| `request_id`, `trace_id` | `request_id` is unique for each execution |
| `ts` | RFC 3339, UTC, when the request was received |
| `provider`, `executor`, `base_url` | |
| `model`, `alias`, `response_model` | |
| `api_key_hash` | SHA-256 hex of the client key |
| `api_key_hint` | `…` plus the last 4 characters. Only set when the key has 12 characters or more. |
| `access_provider` | |
| `session_id`, `parent_session_id` | as the host gives them |
| `auth_id`, `auth_index`, `auth_type` | upstream credential. An OAuth `auth_id` may contain an account email. It is local data, but never commit it. |
| `source`, `reasoning_effort`, `service_tier`, `effective_service_tier` | |
| `stream`, `generate` | |
| `failed`, `status`, `error` | `error` is cut to 512 characters |
| `latency_ms`, `ttft_ms` | |
| `input`, `output`, `reasoning`, `cached`, `cache_read`, `cache_write`, `cache_write_5m`, `cache_write_1h`, `total` | token counts |
| `ratelimit` | Response headers that start with `anthropic-ratelimit-unified-` or `x-codex-`, with lowercase names. These hold the subscription utilization. |

### 4.4 Bridge

- **Resource `/dashboard`** (with a menu label). It forwards `GET` to `<server-url>/ui/`. The page contains no data.
- **Routes `GET` and `POST` on `/v0/management/usage-server/api?path=/api/...&...`.**
  - The plugin checks that `path` starts with `/api/` and contains no `..`.
  - It forwards the request to `<server-url><path>` with the other query parameters and the body.
  - It adds the header `X-Usage-Bridge-Token: <bridge-token>`. It does **not** forward the management key.
- **Timeouts and errors.** Each forward has a timeout of `bridge-timeout-ms`. The route returns 504 on a timeout and 502 when the server can't be reached. Redirects are not followed, and response bodies are capped at 32 MB.

### 4.5 Build

- Build with `CGO_ENABLED=1 go build -buildmode=c-shared -o usage-server.so .`, on the same libc and architecture as the proxy image. The official image is Debian 12 (glibc).
- Build inside a `golang:<ver>-bookworm` container on the target host.
- Do not build on macOS for deployment.

## 5. Server (`server/`, Python 3.15)

### 5.1 Runtime

- Python 3.15. Dependencies are managed with uv (`pyproject.toml` + `uv.lock`).
- The container image is built in two stages:
  1. A build stage runs `uv sync --frozen`.
  2. The final stage is `python:3.15-slim` with the venv copied in, without uv.
- Libraries: Starlette and uvicorn. Use the standard library for everything else (`sqlite3`, `concurrent.futures`, `subprocess`).
- The container runs as a non-root user. That user's UID must be able to read the transcript mounts.

### 5.2 Mounts and environment

| mount | mode | content |
|---|---|---|
| `/spool` | read-only | the plugin's spool folder |
| `/transcripts/claude` | read-only | Claude Code `projects/` folder |
| `/transcripts/<harness>` | read-only | for later harnesses (codex, …) |
| `/data` | read-write | `usage.db`, reader positions, price cache |

| env | default | meaning |
|---|---|---|
| `USAGE_DATA_DIR` | `/data` | |
| `USAGE_SPOOL_DIR` | `/spool` | |
| `USAGE_TRANSCRIPTS_DIR` | `/transcripts` | one subfolder for each harness |
| `USAGE_BRIDGE_TOKEN` | — | must match the plugin's `bridge-token` |
| `USAGE_API_TOKENS` | — | comma-separated tokens for standalone access |
| `USAGE_CPA_URL` | — | optional. Lets standalone users log in with the proxy management key (see §5.7). |
| `USAGE_TRANSCRIPT_SCAN_SECONDS` | `30` | |
| `USAGE_TRANSCRIPT_BACKFILL_DAYS` | `30` | how far back the first scan goes |
| `USAGE_RAW_RETENTION_DAYS` | `180` | raw request rows. Daily rollups are kept forever. |
| `USAGE_PRICES_URL` | `https://models.dev/api.json` | |

### 5.3 Components

All components run in one process. Threads share one writer connection behind a lock, and readers use their own connections. SQLite runs in WAL mode with `busy_timeout` set.

1. **Spool reader** (thread).
   - Every 2 seconds it lists `usage-*.jsonl` in name order.
   - For each file, it reads from the saved offset up to the last complete line.
   - It inserts the rows (`INSERT OR IGNORE` on `request_id`) and saves `(file, inode, offset)` in the same transaction.
   - It skips bad lines and counts them.
   - It forgets offsets for files that no longer exist.
   - It reads `_plugin_status.json` for the health view.
2. **Price sync** (thread). Once a day, and at startup when the cache is empty, it fetches the models.dev data and saves each provider's model prices. If the fetch fails, it keeps the last good copy.
3. **Transcript scanner** (thread).
   - Every `USAGE_TRANSCRIPT_SCAN_SECONDS`, each harness adapter lists its session files.
   - Sessions whose `(mtime, size)` changed go into a parse queue. Within the backfill window, newest sessions go first.
4. **Parser pool.**
   - The parser runs as a subprocess: `python -m usage_server.transcripts.parse --harness claude <file>...`, which prints one JSON line per session.
   - Batches hold about 20 files, with a timeout for each batch.
   - On a timeout or crash, the batch is retried one file at a time. A file that fails on its own is marked `parse_error` and is not retried until it changes.
   - At most 2 subprocesses run at once.
5. **API and UI** (uvicorn). Requests only read the database, except the label and re-parse endpoints.

### 5.4 Database schema (v1)

```sql
requests(
  id INTEGER PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, ts REAL NOT NULL,   -- unix seconds
  provider TEXT, executor TEXT, base_url TEXT, model TEXT, alias TEXT, response_model TEXT,
  api_key_hash TEXT, api_key_hint TEXT, access_provider TEXT,
  session_id TEXT, parent_session_id TEXT,
  auth_id TEXT, auth_index TEXT, auth_type TEXT, source TEXT,
  reasoning_effort TEXT, service_tier TEXT,
  stream INT, failed INT, status INT, error TEXT, latency_ms INT, ttft_ms INT,
  input INT, output INT, reasoning INT, cache_read INT, cache_write INT,
  cache_write_5m INT, cache_write_1h INT, total INT,
  ratelimit TEXT)                                  -- JSON
-- indexes: ts; (session_id, ts); (api_key_hash, ts); (auth_id, ts); (model, ts)

daily_rollup(day TEXT, api_key_hash, auth_id, provider, model, session_id,
  requests, failed, input, output, cache_read, cache_write_5m, cache_write_1h,
  PRIMARY KEY(day, api_key_hash, auth_id, provider, model, session_id))

spool_offsets(file TEXT PRIMARY KEY, inode INT, offset INT, updated REAL)
prices(provider TEXT, model TEXT, input REAL, output REAL, cache_read REAL, cache_write REAL,
  tiers TEXT, fetched_at REAL, PRIMARY KEY(provider, model))   -- $ per million tokens
labels(kind TEXT, key TEXT, label TEXT, PRIMARY KEY(kind, key)) -- kind: api_key | auth

sessions(harness TEXT, session_id TEXT, path TEXT, mtime REAL, size INT,
  parsed_at REAL, parse_error TEXT,
  title TEXT, cwd TEXT, project TEXT, git_branch TEXT, first_ts REAL, last_ts REAL,
  turns INT, peak_context INT, compactions INT, cache_rebuilds INT, tool_errors INT,
  usage_by_model TEXT,                              -- JSON {model: {input, output, cache_read, cache_write_5m, cache_write_1h}}
  tools TEXT,                                        -- JSON {tool: count}
  PRIMARY KEY(harness, session_id))
session_agents(harness, session_id, agent_id, agent_type, description, model,
  turns, usage_by_model TEXT, PRIMARY KEY(harness, session_id, agent_id))
session_skills(harness, session_id, skill, loads INT, turns INT, usage_by_model TEXT,
  PRIMARY KEY(harness, session_id, skill))
session_prs(harness, session_id, repo TEXT, number INT, url TEXT, action TEXT, ts REAL,
  PRIMARY KEY(harness, session_id, url, action))     -- action: opened | merged
schema_version(version INT)
```

Rules for the schema:
- Store tokens, never dollars. Compute cost when a request is served (§5.6), so a price update also corrects past costs.
- Migrations run in order at startup, inside a transaction, and are tracked in `schema_version`.

### 5.5 API

All endpoints are JSON and live under `/api/`. They take `from` and `to` as unix seconds or ISO dates. The default range is the last 7 days.

| method | path | returns |
|---|---|---|
| GET | `/api/health` | plugin status file, spool lag, last price sync, parser queue |
| GET | `/api/summary` | totals (requests, tokens, estimated cost, failures) and the top 5 for each dimension |
| GET | `/api/breakdown?group=api_key\|auth\|provider\|model\|session\|project&limit=` | one row per group with tokens, cost and requests |
| GET | `/api/timeseries?bucket=hour\|day&group=&top=` | stacked series for charts |
| GET | `/api/sessions?q=&harness=&has_transcript=` | session list: proxy totals joined with transcript data |
| GET | `/api/session?id=` | one session: proxy totals by model and upstream, plus transcript details (agents, skills, tools, PRs) |
| POST | `/api/session/reparse?id=` | queue a re-parse. Returns at once. |
| GET | `/api/labels` / POST `/api/labels` | read or set labels for key hashes and auth IDs |
| GET | `/api/prices` | price table and unknown models |

Every resource is named with a query parameter, never a path segment. That keeps every route an exact path, which the bridge forwards as is.

### 5.6 Cost

- **Price lookup.**
  - Map the proxy provider to a models.dev provider: `claude`→`anthropic`, `codex`→`openai`, `gemini`→`google`. Other providers are configurable.
  - Normalize the model name: strip `[1m]`-style suffixes and provider prefixes. Try the exact name first, then the name without its date suffix.
  - If the provider has no match, search the other providers.
- **Formula.**
  `cost = input·in + output·out + cache_read·cr + cache_write_5m·cw + cache_write_1h·(2·in)`, divided by 1,000,000.
  - models.dev `cache_write` is the 5-minute price.
  - If the 5-minute and 1-hour split is unknown, price every cache write at the 5-minute rate.
- **Tiers.** Apply models.dev `tiers` (context-size price tiers) when one request's input is above the tier size.
- **Missing prices.** A model with no price shows a cost of "—" and is listed under `/api/prices` as unknown.
- **Labelling.** Every cost in the UI is labelled as an estimate. Subscription traffic is shown at API list prices.

### 5.7 Auth

Requests arrive in one of 3 ways:

1. **Through the bridge.** The request carries `X-Usage-Bridge-Token`, which must equal `USAGE_BRIDGE_TOKEN` (compared in constant time). The proxy has already checked the management key.
2. **Standalone with a token.** The request carries `Authorization: Bearer <token>`, and the token is one of `USAGE_API_TOKENS`.
3. **Standalone with the management key** (optional). The server checks the key once by calling a cheap management endpoint on `USAGE_CPA_URL`, then caches the answer for 5 minutes. This only works when the proxy accepts management calls from the server's address, which may need `management.allow-remote: true`.

`/ui/` and its assets need no auth and contain no data. All `/api/*` endpoints need one of the 3 methods.

### 5.8 UI

- **One HTML page** with inline JavaScript and CSS, no build step and no CDN. It must work offline inside the panel's iframe.
- **Mode detection.**
  - If the page runs under `/v0/resource/plugins/`, it calls `/v0/management/usage-server/api?path=...` with `Authorization: Bearer <management key>`.
  - Otherwise it calls `/api/...` with a server token.
  - It asks for the key or token once and keeps it in `localStorage` under its own name.
- **Views:**
  - **Overview:** totals, cost by day as a stacked bar chart, and the top client keys, upstreams and models.
  - **Breakdown:** group by any dimension, with filters and sorting.
  - **Sessions:** a searchable list. A session's detail view has proxy totals, a cost table for each agent, skills, tool counts, pull requests opened and merged, a timeline, and a re-parse button.
  - **Settings:** labels, price status and plugin health.
- **Theme:** light and dark, following `prefers-color-scheme`.

## 6. Transcript adapters (`server/usage_server/transcripts/`)

Interface for each harness:

```python
class Adapter(Protocol):
    harness: str                                   # "claude"
    def discover(self, root: Path) -> Iterable[SessionFile]: ...   # id, path, mtime, size
    def parse(self, path: Path) -> SessionSummary: ...              # runs in the parser subprocess
    def session_key(self, proxy_session_id: str) -> str | None: ... # proxy SessionID → transcript id
```

### Claude adapter (v1)

**Where the files are.** Under `/transcripts/claude/<project-slug>/`:
- The session transcript is `<session-id>.jsonl`.
- Subagent transcripts are `<session-id>/subagents/agent-<id>.jsonl`, each with a `.meta.json` beside it (`agentType`, `description`, `model`).

**What to port.** The parsing logic comes from internal reference scripts. Local copies are in `reference/`, which git ignores. Copy and adapt them. Never import them at runtime.

**Rules the reference parser follows** (keep them):
- Claude Code writes one line per content block, and **every line repeats the message's `usage`**. Sum usage once per `message.id`, or every count comes out about 1.7 times too high.
- `cache_creation.ephemeral_1h_input_tokens` and `ephemeral_5m_input_tokens` split cache writes by TTL. If there is no split, count the whole write as 5-minute.
- Skills come from two places:
  - loaded: `tool_use` with `name == "Skill"` and `input.skill`
  - token attribution: the assistant line's `attributionSkill`
- `custom-title` lines hold the session title.
- `system` lines with `subtype == "compact_boundary"` mark compactions. `compactMetadata.preTokens` holds the context size just before.
- `cwd` and `gitBranch` appear on entries. Derive `project` from `cwd`:
  - Strip worktree suffixes such as `/.claude/worktrees/<name>` and `/.t3/worktrees/<repo>/<id>`.
  - Then take the repository folder name.
- The peak context of one turn is `input + cache_read + cache_write`.
- A cache rebuild is a turn that writes at least 30,000 cache tokens after a gap longer than the cache TTL (5 minutes, or 1 hour when `promptCacheRequested == "1h"`).

**Pull requests.**
- *Opened:* a `Bash` `tool_use` whose command contains `gh pr create`, and whose matching `tool_result` has a URL like `https://github.com/<owner>/<repo>/pull/<n>`.
- *Merged:* a `gh pr merge` call whose result does not report an error. Read the PR number and repo from the command or the output.
- An optional later step checks the state with the GitHub API.

**Join with the proxy data.** The adapter's `session_key()` turns the proxy's `session_id` into a transcript session ID. Strip any `claude:` prefix, and confirm the format in phase 0.

**Privacy.** Never copy message text into the database. Titles, cwd, subagent descriptions and command names are fine.

## 7. Deployment

### 7.1 Production (after cutover)

Add to the existing proxy compose service:

```yaml
    volumes:
      - <proxy-home>/plugins:/CLIProxyAPI/plugins:ro
      - <proxy-home>/usage-spool:/CLIProxyAPI/usage-spool
```

Add a new service on the same network:

```yaml
  usage-server:
    image: usage-server:<ver>
    user: "<uid that can read the transcripts>"
    environment: [USAGE_BRIDGE_TOKEN=..., USAGE_API_TOKENS=...]
    volumes:
      - <proxy-home>/usage-spool:/spool:ro
      - <claude-home>/.claude/projects:/transcripts/claude:ro
      - <data-dir>:/data
```

Then turn on `plugins.enabled` and add the `plugins.configs.usage-server` block (§4.1). Restart the proxy once to load the plugin.

**Rollback:** set `plugins.configs.usage-server.enabled: false`, or remove the `.so` file and restart. The server can stay down without any effect on the proxy.

### 7.2 Side-by-side test (before cutover)

Run a **separate test proxy** with its own compose project, port and data folder. Do not touch the production proxy.

- **Do not give the test proxy copies of the production OAuth credentials.**
  - Subscription refresh tokens rotate when used. If a second proxy refreshes them, it can invalidate the production proxy's tokens.
  - Instead, set up the test proxy with a single Claude API-key credential whose `base-url` is the **production proxy**, using an existing production client key.
  - Traffic then flows test proxy → production proxy → upstream, and the test proxy holds no OAuth state.
  - Usage records from the test proxy show the production proxy as the upstream, which is enough to test the plugin end to end.
- **Write the test proxy config from scratch** (port, access key, management key, plugins, the one credential). Do not copy the production config. Secrets must never be printed while the config is generated.
- **Point a few clients, or a test script, at the test proxy port.** Then check:
  - Spool files appear and `_plugin_status.json` updates.
  - The server ingests the records, and the session IDs join with the transcripts.
  - The dashboard works both standalone and in the test proxy's management panel.
  - Stopping the server, then filling the disk or corrupting a spool line, has no effect on test proxy traffic.
  - Sending `SIGSTOP` to the server has no effect on test proxy latency.

## 8. Later harnesses

- Each harness gets a folder `/transcripts/<harness>` and an adapter.
- Proxy-side data does not depend on the harness. `provider` and `executor` already tell Codex traffic from Claude traffic.
- The `sessions` table is keyed by `(harness, session_id)`.

## 9. Phases

| phase | work | done when |
|---|---|---|
| 0 | Build the plugin on the target host. Run the test proxy (§7.2) with the plugin writing the spool only. | Spool records are correct, and the session ID format is confirmed. |
| 1 | Server core: spool reader, schema, prices, `/api/*`, dashboard (standalone) | Breakdowns by key, upstream, model and session match the spool. |
| 2 | Claude adapter, session views, parser pool | Session detail shows agents, skills, tools and PRs, with costs. |
| 3 | Bridge on: menu in the panel, re-parse button | The dashboard works inside the test proxy's panel. |
| 4 | Robustness tests (§7.2), then cutover (§7.1) | Production proxy runs the plugin. Rollback has been tested. |

## 10. Open questions

1. The `SessionID` format the host sends for Claude Code requests, and for subagent requests (`ParentSessionID`?).
2. Whether a request that the proxy retries on another credential produces one usage record or several.
3. A license for the public repo.
