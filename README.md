# usage-server

Usage tracking and estimated cost for [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI). It breaks down usage by proxy client key, upstream credential, model, session, project, skill and subagent.

It has two parts:

- **`plugin/`.** A small CLIProxyAPI plugin written in Go. It writes each usage record to a local spool file and shows the dashboard inside the proxy's management panel. It only observes, never blocks requests, and makes no network calls on the request path.
- **`server/`.** A Python service. It reads the spool files into SQLite, prices tokens with [models.dev](https://models.dev), parses local agent transcripts (Claude Code first), and serves a JSON API and a dashboard. It can also run on its own, without the proxy.

Status: tested on a side-by-side test proxy (phases 0–4 in [docs/SPEC.md](docs/SPEC.md)); not yet in production. See [deploy/RUNBOOK.md](deploy/RUNBOOK.md) for cutover and rollback.

## Layout

```
plugin/      Go plugin (c-shared library loaded by CLIProxyAPI)
server/      Python 3.15 service (Starlette + uvicorn, SQLite)
deploy/      server Dockerfile, compose example, cutover and rollback runbook
scripts/     plugin build, public-safety check
docs/        specification
reference/   local copies of reference scripts (gitignored, not published)
```

## Build the plugin

Build on Linux with the same libc and architecture as the proxy image. The official image is Debian 12 (glibc):

```sh
scripts/build-plugin.sh test   # go vet, go test -race, then build plugin/build/usage-server.so
```

It runs `CGO_ENABLED=1 go build -buildmode=c-shared` inside `golang:1.26-bookworm`. Set `REPO_URL` to put your repository URL in the plugin metadata.

The file name sets the plugin ID: `usage-server.so` gives `usage-server`.

## Build and run the server

```sh
cd server && uv sync && uv run pytest        # tests
docker build -f deploy/Dockerfile.server -t usage-server:dev .
```

The image runs `python -m usage_server.main` as a non-root user. See SPEC §5.2 for mounts and environment variables, and `deploy/docker-compose.example.yml` for a compose service.
