"""Parser subprocess: python -m usage_server.transcripts.parse --harness claude <file>...

Prints one JSON line per file: {"path", "ok": true, "summary"} or
{"path", "ok": false, "error"}. Each file is isolated by try/except; a crash
or hang of the whole process is handled by the parent (timeout, retry one
file at a time).
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import traceback
from pathlib import Path

from . import adapter


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--harness", required=True)
    ap.add_argument("--max-mem-mb", type=int, default=1024)
    ap.add_argument("files", nargs="+")
    args = ap.parse_args(argv)
    if args.max_mem_mb > 0:
        try:
            lim = args.max_mem_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (lim, lim))
        except (ValueError, OSError):
            pass
    ad = adapter(args.harness)
    out = sys.stdout
    for f in args.files:
        try:
            rec = {"path": f, "ok": True, "summary": ad.parse(Path(f))}
        except MemoryError:
            rec = {"path": f, "ok": False, "error": "out of memory"}
        except Exception as e:
            rec = {"path": f, "ok": False, "error": f"{type(e).__name__}: {e}"[:500],
                   "trace": traceback.format_exc(limit=3)[-1000:]}
        out.write(json.dumps(rec, separators=(",", ":")) + "\n")
        out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
