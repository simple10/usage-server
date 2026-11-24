# usage-server

Usage tracking and estimated cost for [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI). It breaks down usage by proxy client key, upstream credential, model, session, project, skill and subagent.

It has two parts:

- **`plugin/`.** A small CLIProxyAPI plugin written in Go. It writes each usage record to a local spool file and shows the dashboard inside the proxy's management panel. It only observes, never blocks requests, and makes no network calls on the request path.
- **`server/`.** A Python service. It reads the spool files into SQLite, prices tokens with [models.dev](https://models.dev), parses local agent transcripts (Claude Code first), and serves a JSON API and a dashboard. It can also run on its own, without the proxy.

Status: early draft. See [docs/SPEC.md](docs/SPEC.md) for the design, the facts about the proxy host it relies on, and the build phases.

## Layout

```
plugin/      Go plugin (c-shared library loaded by CLIProxyAPI)
server/      Python 3.15 service (not yet written)
deploy/      Dockerfiles and compose examples (not yet written)
docs/        specification
reference/   local copies of reference scripts (gitignored, not published)
```

## Build the plugin

Build on Linux with the same libc and architecture as the proxy image. The official image is Debian 12 (glibc):

```sh
docker run --rm -v "$PWD/plugin":/src -w /src golang:1.26-bookworm \
  sh -c 'CGO_ENABLED=1 go build -buildmode=c-shared -o build/usage-server.so .'
```

The file name sets the plugin ID: `usage-server.so` gives `usage-server`.
