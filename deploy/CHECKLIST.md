# Deploy checklist (relay host)

Run these steps by hand from a machine that has SSH access to the server. Do not touch the existing app's service, env file, or install dir.

1. `sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak.$(date +%F)`
2. **Remove the stale block** between `# BEGIN yobiru-selfhost-relay` and its END marker (a `handle_path /yobiru-relay/*` block). Nothing serves it anymore.
3. Install the service as described in README "Server install". It binds `127.0.0.1:18790`. First check the port is free: `ss -ltnp | grep 18790`.
4. Paste `deploy/Caddyfile.snippet` into the site block, run `caddy validate --config /etc/caddy/Caddyfile`, then `sudo systemctl reload caddy`.
5. Smoke test from outside: `curl https://<host>/relay/health` should return `{"ok": true}`. Without the bearer, `curl https://<host>/relay/tasks/next` should return 401.
6. Copy the secret to the box and to the Mac over a private channel. Never commit it.
7. Rollback: `systemctl disable --now mac-relay`, remove the snippet, reload caddy.
