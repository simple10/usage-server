"""Turn the usage-server plugin on or off in the proxy config, in place.
Usage: edit_config.py enable|disable|check <config> <env-file> [--dry-run OUT]
Env: USAGE_PORT (default 18090) sets the server-url port. Prints no secrets.
Only edits the stock form of the `plugins:` block; otherwise it refuses."""
import os, sys
mode, cfg, envf = sys.argv[1], sys.argv[2], sys.argv[3]
out = sys.argv[5] if len(sys.argv) > 5 and sys.argv[4] == "--dry-run" else cfg
port = os.environ.get("USAGE_PORT", "18090")
OFF = 'plugins:\n    enabled: false\n    dir: "plugins"\n'
env = dict(l.strip().split("=", 1) for l in open(envf) if "=" in l)
ON = ('plugins:\n    enabled: true\n    dir: "plugins"\n    configs:\n        usage-server:\n'
      '            enabled: true\n            spool-dir: /CLIProxyAPI/usage-spool\n'
      '            max-file-mb: 64\n            retention-days: 14\n            max-total-mb: 1024\n'
      '            queue-size: 8192\n            flush-interval-ms: 1000\n'
      f'            server-url: http://127.0.0.1:{port}\n'
      f'            bridge-token: {env["USAGE_BRIDGE_TOKEN"]}\n'
      '            bridge-timeout-ms: 5000\n            menu-label: Usage\n')
s = open(cfg).read()
state = "off" if s.count(OFF) == 1 else "on" if s.count(ON) == 1 else "unknown"
if mode == "check":
    print("plugin block state:", state); sys.exit(0 if state != "unknown" else 1)
if mode == "enable":
    if state == "on": print("already enabled"); sys.exit(0)
    if state != "off": print("ERROR: plugins block not in the expected form; not editing"); sys.exit(1)
    s2 = s.replace(OFF, ON, 1)
elif mode == "disable":
    if state != "on": print("ERROR: usage-server block not in the expected form; not editing"); sys.exit(1)
    s2 = s.replace(ON, ON.replace("        usage-server:\n            enabled: true\n", "        usage-server:\n            enabled: false\n"), 1)
else:
    print("mode must be enable, disable or check"); sys.exit(2)
# Write in place (the file is bind-mounted; a rename would not be seen).
flags = os.O_WRONLY | os.O_CREAT | (0 if out == cfg else os.O_TRUNC)
fd = os.open(out, flags, 0o600)
with os.fdopen(fd, "r+" if out == cfg else "w") as f:
    f.seek(0); f.write(s2); f.truncate()
print(f"{mode}d usage-server plugin in {'place' if out == cfg else out}")
