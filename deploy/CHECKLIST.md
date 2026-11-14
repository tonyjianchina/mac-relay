# Deploy checklist (relay host)

Run these steps by hand from a machine that has SSH access to the server. Do not touch the existing app's service, env file, or install dir.

1. `sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak.$(date +%F)`
2. **Remove the stale block** between `# BEGIN yobiru-selfhost-relay` and its END marker (a `handle_path /yobiru-relay/*` block). Nothing serves it anymore.
3. Install the service as described in README "Server install". It binds `127.0.0.1:18790`. First check the port is free: `ss -ltnp | grep 18790`.
4. Paste `deploy/Caddyfile.snippet` into the site block, run `caddy validate --config /etc/caddy/Caddyfile`, then `sudo systemctl reload caddy`.
5. Smoke test from outside: `curl https://<host>/relay/health` should return `{"ok": true, "soft_body": 16777216, "max_body": 536870912}` when `RELAY_MAX_SOFT_BODY` and `RELAY_MAX_BODY` are left at the defaults (16MiB multipart cap, 512MiB hard cap). Without the bearer, `curl https://<host>/relay/tasks/next` should return 401.
6. Copy the secret to the box and to the Mac over a private channel. Never commit it.
7. Rollback: `systemctl disable --now mac-relay`, remove the snippet, reload caddy.
8. Box backup channel (optional): make sure the box can reach the relay host's Tailscale address (`curl http://100.x.y.z:18791/health`), add `RELAY_FALLBACK_URL=http://100.x.y.z:18791` to the box env (a comma-separated list; the agent consumes every channel at once, see `docs/channels.md`), stop the old `relay-box-agent` (`pkill -f bin/relay-box-agent`), start it again, and check there is exactly one instance (`pgrep -fc bin/relay-box-agent`).
9. Upgrade an existing relay: deploy `relay/server.py` and `mac/relay-worker.py` together and restart both. No new unit setting is required. Worker outputs are `PUT /tasks/{id}/files/{name}?kind=out` (streamed, up to 512MiB) followed by a JSON `POST /tasks/{id}/result` with `files` naming those objects. A multipart body over 16MiB (`RELAY_MAX_SOFT_BODY`) returns 413 and the server closes the connection. **Task input attachments from `relay-send` stay multipart and stay capped at 16MiB.** Only worker outputs use streaming PUT. An old worker can still post a multipart result that fits in 16MiB; a larger one gets 413 until that worker is updated. Caddy `request_body max_size 512MB` matches the hard cap and does not raise the multipart cap.

9. Upgrading the VPS relay to protocol v2 (optional): replace `relay/server.py` and restart only the `mac-relay` unit. `GET /status` (authenticated) then lists `features`; Caddy routes do not change. Until then the VPS channel works in legacy mode (single PUT per file, no resume, multipart attachments up to 16MiB).
