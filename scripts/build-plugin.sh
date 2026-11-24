#!/bin/sh
# Build the plugin .so inside golang:<ver>-bookworm (glibc, matches the
# official proxy image). Runs as the calling user so build output is not root-owned.
# Usage: scripts/build-plugin.sh [test]
set -eu
cd "$(dirname "$0")/../plugin"
GO_IMAGE="${GO_IMAGE:-golang:1.26-bookworm}"
REPO_URL="${REPO_URL:-}"
LDFLAGS=""
[ -n "$REPO_URL" ] && LDFLAGS="-X main.repoURL=$REPO_URL"
CMD="CGO_ENABLED=1 go build -trimpath -buildmode=c-shared -ldflags '$LDFLAGS' -o build/usage-server.so ."
[ "${1:-}" = "test" ] && CMD="go vet ./... && go test -race -count=1 ./... && $CMD"
docker run --rm --user "$(id -u):$(id -g)" \
  -e HOME=/tmp -e GOCACHE=/tmp/gocache -e GOMODCACHE=/tmp/gomod \
  -v usage-server-gocache:/tmp/gocache -v usage-server-gomod:/tmp/gomod \
  -v "$PWD":/src -w /src "$GO_IMAGE" sh -c "$CMD"
ls -l build/usage-server.so
