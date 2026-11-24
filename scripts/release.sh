#!/bin/sh
# Cut a release: bump VERSION, test, commit, tag vX.Y.Z, push.
# Usage: scripts/release.sh X.Y.Z [--no-test]
# Consumers (e.g. stack-cli) pin the tag and build it on the target host.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
V="${1:-}"
echo "$V" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' || { echo "usage: $0 X.Y.Z [--no-test]" >&2; exit 2; }
[ "$(git branch --show-current)" = "main" ] || { echo "release from main only" >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "working tree not clean" >&2; exit 1; }
git fetch -q origin main
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || { echo "main differs from origin/main; pull or push first" >&2; exit 1; }
git rev-parse -q --verify "refs/tags/v$V" >/dev/null && { echo "tag v$V exists" >&2; exit 1; }
if [ "${2:-}" != "--no-test" ]; then
  "$ROOT/scripts/build-plugin.sh" test
  (cd server && uv run pytest -q)
fi
CUR="$(cat VERSION)"
if [ "$CUR" != "$V" ]; then
  echo "$V" > VERSION
  sed -i.bak "s/^version = \"$CUR\"/version = \"$V\"/" server/pyproject.toml && rm -f server/pyproject.toml.bak
  git add VERSION server/pyproject.toml
  git commit -q -m "release v$V"
fi
git tag -a "v$V" -m "usage-server v$V"
git push -q origin main "v$V"
echo "released v$V"
