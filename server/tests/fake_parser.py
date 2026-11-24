"""Stands in for the parser subprocess: hangs on files named *hang*, crashes
(SIGSEGV) on *crash*, else delegates to the real parser."""
import os
import signal
import sys
import time

files = [os.path.basename(a) for a in sys.argv[1:] if a.endswith(".jsonl")]
if any("hang" in f for f in files):
    time.sleep(60)
if any("crash" in f for f in files):
    os.kill(os.getpid(), signal.SIGSEGV)
from usage_server.transcripts.parse import main
sys.exit(main(sys.argv[1:]))
