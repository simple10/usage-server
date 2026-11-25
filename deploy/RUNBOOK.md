# Cutover and rollback runbook

This runbook puts the usage-server plugin into a production CLIProxyAPI and takes it out again. Replace every `<placeholder>`. Keep real values out of git.

Read [docs/SPEC.md](../docs/SPEC.md) §7 first. Phases 0–4 must have passed on a side-by-side test proxy.

## What changes in production

- Two new volume mounts on the proxy service: the plugin folder (read-only) and the spool folder.
- A `plugins` block in the proxy config.
- One proxy restart, to load the plugin.
- A new `usage-server` container. The proxy does not depend on it.

## Before you start

1. Back up the proxy config:
   `cp <proxy-home>/config.yaml <proxy-home>/config.yaml.bak-<date>`
2. Check that the image version you will run is the version the plugin was tested against:
   `docker inspect -f '{{.Config.Image}}' <proxy-container>`
3. Build the plugin on the target host and run its tests:
   `scripts/build-plugin.sh test`
4. Build the server image:
   `docker build -f deploy/Dockerfile.server -t usage-server:<version> .`
5. Make two random secrets and write them to a private env file (mode 600). Do not print them.
   - `USAGE_BRIDGE_TOKEN`: shared with the plugin's `bridge-token`.
   - `USAGE_API_TOKENS`: for standalone dashboard access.
6. Pick a quiet time. The restart drops in-flight requests.

## Cutover

1. Create the folders:
   `mkdir -p <proxy-home>/plugins <proxy-home>/usage-spool <data-dir>`
2. Copy the plugin. The file name sets the plugin ID:
   `cp plugin/build/usage-server.so <proxy-home>/plugins/usage-server.so`
3. Start the server first. It waits for spool files and does no harm alone:
   add the `usage-server` service from `deploy/docker-compose.example.yml`, then `docker compose up -d usage-server`.
4. Check it:
   `curl -s -H "Authorization: Bearer <api-token>" http://<server-host>:<port>/api/health`
   It should return `"ok": true` and a price count above 0.
5. Add the two volume lines to the proxy service (see the example compose file).
6. Add the plugin block to the proxy config. If your config still has the stock block (`plugins: enabled: false, dir: "plugins"`), `python3 deploy/edit_config.py enable <proxy-home>/config.yaml <private-dir>/usage-server.env` does this for you and prints no secrets. Otherwise write it by hand. Write the file in place: if the config is bind-mounted as a single file, an editor that saves by rename hides the change from the container.

   ```yaml
   plugins:
     enabled: true
     dir: plugins
     configs:
       usage-server:
         enabled: true
         spool-dir: /CLIProxyAPI/usage-spool
         server-url: http://usage-server:8080   # or http://127.0.0.1:<port> with host networking
         bridge-token: <bridge-token>
         menu-label: Usage
   ```

7. Recreate the proxy once: `docker compose up -d --no-deps --force-recreate <proxy-service>`. Do not use `docker compose restart`: it keeps the old container, so the new volumes are not mounted and the plugin is not found.
8. Check the proxy log for these two lines and no plugin warnings:
   - `pluginhost: plugin loaded plugin_id=usage-server`
   - `pluginhost: plugin registered plugin_id=usage-server`
9. Send one small request through the proxy. Within a few seconds:
   - a `usage-*.jsonl` file appears in `<proxy-home>/usage-spool`, and `_plugin_status.json` updates every 10 seconds;
   - `/api/health` on the server shows the request count going up.
10. Open the proxy management panel. A **Usage** entry appears under Plugins. Open it and enter the management key once.

## Watch for a day

- `_plugin_status.json`: `dropped`, `write_errors` and `panics` stay at 0, and `updated_at` stays fresh.
- Spool folder size stays under `max-total-mb`.
- Proxy latency and error rate do not change.

## Rollback

Use the first step that fits. Each was tested on the test proxy.

1. **Turn the plugin off without a restart.** Set `plugins.configs.usage-server.enabled: false` (write in place, or `edit_config.py disable`; `edit_config.py enable` turns it on again). The proxy reloads the config and unloads the plugin. Requests continue. The menu entry and bridge routes go away (404).
2. **Remove the plugin.** Move `usage-server.so` out of `<proxy-home>/plugins` and recreate the proxy. The proxy starts normally, even with the `plugins.configs.usage-server` block still present.
3. **Full revert.** Put the plugins block back to stock (`edit_config.py remove`, or restore `config.yaml.bak-<date>`) and recreate the proxy. The two volume lines can stay; they do nothing while the plugin is off. Remove them later if you like.

The server can stay down or be removed at any time. It has no effect on the proxy: the bridge returns 502 (server down) or 504 after `bridge-timeout-ms` (server hung), and the spool keeps filling up to its retention limits.

Records produced while the plugin is off are not recorded. Spool files already written stay until retention deletes them.

## Upgrading the plugin

The proxy keeps the old plugin image loaded when the file is replaced. Copy the new `.so` over the old one, then restart the proxy (`docker compose restart` is enough here, because the mounts do not change).

## Known limits

- When the spool disk is full, the plugin drops records (and counts them), but it cannot update `_plugin_status.json` either. The dashboard then shows the plugin as **stale**. Free space; the plugin then opens a new file on the next record after its 5-second back-off. (The back-off and reopen are covered by unit tests; on the test proxy the full-disk part was verified live, the recovery part was not.)
- Costs are estimates at API list prices from models.dev, including for subscription traffic.
