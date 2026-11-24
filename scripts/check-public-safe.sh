#!/bin/sh
# Fails if staged files (or files given as args) contain box-specific strings.
# Patterns (extended regex, one per line) are read from
# deploy/local/forbidden-patterns.txt, which is gitignored on purpose.
set -eu
cd "$(dirname "$0")/.."
PATTERNS=deploy/local/forbidden-patterns.txt
[ -f "$PATTERNS" ] || { echo "missing $PATTERNS" >&2; exit 2; }
if [ "$#" -gt 0 ]; then files="$*"; else files=$(git diff --cached --name-only --diff-filter=ACM); fi
[ -n "$files" ] || exit 0
found=0
for f in $files; do
  [ -f "$f" ] || continue
  if grep -nIEf "$PATTERNS" "$f" >/dev/null 2>&1; then
    echo "forbidden pattern in $f:" >&2
    grep -nIEf "$PATTERNS" "$f" | cut -d: -f1 | sed "s/^/  line /" | head -5 >&2
    found=1
  fi
done
exit $found
