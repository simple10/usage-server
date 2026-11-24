#!/bin/sh
# Build everything a host needs, on that host: the server image and the plugin .so.
# Needs only docker. Version comes from the VERSION file.
# Usage: scripts/build-host.sh [--no-test]
#   REVISION=<git sha>   recorded in the image label (default: git HEAD, else "unknown")
#   IMAGE=<name:tag>     image tag (default: usage-server:<version>)
# Output: image usage-server:<version>, plugin/build/usage-server.so
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VERSION="$(cat VERSION)"
REVISION="${REVISION:-$(git rev-parse HEAD 2>/dev/null || echo unknown)}"
if [ "${1:-}" = "--no-test" ]; then "$ROOT/scripts/build-plugin.sh"; else "$ROOT/scripts/build-plugin.sh" test; fi
docker build -f deploy/Dockerfile.server \
  --build-arg "VERSION=$VERSION" --build-arg "REVISION=$REVISION" \
  -t "${IMAGE:-usage-server:$VERSION}" .
echo "built usage-server:$VERSION ($REVISION) and plugin/build/usage-server.so"
