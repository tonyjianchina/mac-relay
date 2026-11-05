# mac-relay without a VPS

The primary install is still a Linux VPS: systemd runs `relay/server.py` on `127.0.0.1:18790`, and Caddy terminates HTTPS and strips `handle_path /relay/*`. That path is [README.md](../README.md) (“Server install”) and [deploy/CHECKLIST.md](../deploy/CHECKLIST.md).

This page is for when you do not want that VPS. Three layouts, in the order to consider them. All three run the same server. None of them is a different product mode: there is no “Tailscale mode” or “tunnel mode” in the code. You set bind, port, and the base URLs the box and the Mac already understand.

Use the first layout that matches the network you have.

| | VPS + Caddy | 1. Mac + Tailscale | 2. Box + Tailscale | 3. Tunnel |
|---|---|---|---|---|
| Where the process runs | VPS | Mac | Box | Mac or box |
| TLS | Caddy | none in mac-relay | none in mac-relay | the tunnel, not mac-relay |
| Client base URL | `https://<host>/relay` | `http://<mac-100.x>:18790` | box `http://127.0.0.1:18790`, Mac `http://<box-100.x>:18790` | `https://<tunnel-host>` (no `/relay` if the tunnel targets `:18790` directly) |
| Reachable from | the public hostname you publish | the tailnet | the tailnet | the internet (Funnel, quick tunnel, or a named tunnel hostname) |
| Queue while the Mac sleeps | yes | no | yes | only if the server is not the Mac |

Tradeoffs against the VPS path are in each section below.

## Shared rules

### The server you are configuring

`relay/server.py` is a stdlib `ThreadingHTTPServer`. It reads:

| Env | Required | Default | Role |
|---|---|---|---|
| `RELAY_SECRET_FILE` | yes | — | bearer secret file. The process exits if this is unset, unreadable, or shorter than 16 bytes after stripping whitespace |
| `RELAY_DATA_DIR` | no | `./data` | SQLite database and task files. Relative paths follow the process working directory |
| `RELAY_BIND` | no | `127.0.0.1` | listen address |
| `RELAY_PORT` | no | `18790` | listen port |

The shipped unit `deploy/mac-relay.service` sets `RELAY_BIND=127.0.0.1` on purpose, so that only a local reverse proxy can reach it. The default is not a tailnet listener. If a Tailscale peer or any other machine must connect to this process, set `RELAY_BIND=0.0.0.0` or to that interface’s address before you start it. Check from the *other* machine. `curl` to `127.0.0.1` succeeds for both `127.0.0.1` and `0.0.0.0`, so it does not prove the bind changed.

`0.0.0.0` accepts connections on every IPv4 interface, including a LAN or a public NIC, not only Tailscale. A Tailscale ACL does not filter those other interfaces. Prefer a host firewall that allows TCP `18790` only from the tailnet, or bind the Tailscale address itself. Binding only `100.x.y.z` means `http://127.0.0.1:18790` does not connect; every local client must use `http://100.x.y.z:18790` too.

The server does not speak TLS. `https://` in a client URL works only when something else terminates TLS and forwards HTTP to this process (Caddy on the VPS path, or a tunnel in layout 3).

The server does not read `RELAY_URL`. That variable is only for the box tools.

### Secret

Generate one secret and put the same bytes on the server, the box, and the Mac worker. The server compares `Authorization: Bearer …` with the stripped file contents (`hmac.compare_digest`). The box reads `RELAY_SECRET_FILE`. The worker reads `secret_file` in `worker.json`.

```sh
openssl rand -hex 32 > /path/to/secret
chmod 600 /path/to/secret
```

`openssl rand -hex 32` writes 64 hex characters (32 bytes of entropy). Readers strip whitespace, so the trailing newline still leaves 64 bytes, which clears the 16-byte minimum. Do not commit the file. `.gitignore` ignores a file named `secret` and `*.secret` only inside this repo. Copy it over SSH or Tailscale, not through the git remote.

### URL shape

Clients join `base.rstrip("/") + path`. `path` is `/health`, `/tasks`, `/tasks/next`, `/results/next`, and so on. The base URL must be whatever is mapped onto the server’s root.

- VPS + Caddy: base is `https://<host>/relay`, because `handle_path /relay/*` strips that prefix before proxying to `127.0.0.1:18790`. Smoke test from the checklist is `https://<host>/relay/health`.
- No reverse proxy: base is the origin root, for example `http://100.x.y.z:18790` or `http://127.0.0.1:18790`. Do not append `/relay`. Nothing in `server.py` strips it, so `/relay/health` is a 404.
- Tunnel pointed at `http://127.0.0.1:18790`: same rule. The public origin is the base, with no `/relay`, unless you also put the Caddy snippet in front.

`examples/box.env.example` and `examples/worker.example.json` show the VPS URLs, including a fallback on port `18791`. That port is the checklist’s second Caddy listener, not a second port inside `server.py`. Do not copy those URLs into the layouts below.

### Failover

Failover is optional and only does something when a second base URL is set.

- Box: `RELAY_FALLBACK_URL`. Unset means there is no fallback. `relay-box-agent` switches after `RELAY_FAIL_THRESHOLD` consecutive primary failures (default 3). A failure is a connection error or an HTTP status ≥ 500. HTTP 4xx resets the failure count; it does not switch. The agent then stays on the fallback and probes `primary + /health` at most once per `RELAY_COOLDOWN_S` (default 300) before switching back. The mode is in memory; restart the agent and it starts on the primary. `relay-send` forces the threshold to 1 and, if that single failure switched it, retries the enqueue once on the fallback. Enqueue is idempotent on task id.
- Mac worker: `fallback_url` in `worker.json`, with `fail_threshold` (default 3) and `cooldown_s` (default 300). Same sticky rule. The worker stores the mode in `<state_dir>/conn.json`. After a restart it keeps using the fallback until a probe succeeds, or until you `touch <state_dir>/reset-primary` or `kill -HUP <worker-pid>`. If you remove `fallback_url` but leave `conn.json` saying `"mode": "fallback"`, the next request looks up a missing URL. Delete `conn.json` or touch `reset-primary` when you go back to one URL.

On a single-endpoint layout, omit `RELAY_FALLBACK_URL` and `fallback_url`. Do not point them at the same URL as the primary.

Fallback requests use an opener with `ProxyHandler({})`, so they ignore `HTTP_PROXY` / the macOS system proxy. Primary requests use the default urllib opener, which follows the environment and, on macOS, the system proxy. Optional worker field `fallback_source_ip` binds the source address of fallback `http://` connections (the Mac’s own Tailscale IPv4) so scoped routing can prefer the Tailscale utun. It is not applied to the primary URL, and the handler does not cover `https://` fallbacks. The box client has no `fallback_source_ip`.

A real run recorded in [TESTS-REAL.md](../TESTS-REAL.md) failed when a Mac system proxy (Shadowrocket) captured `100.64.0.0/10`. The proxy bypass above was added for the fallback URL only. A tailnet address used as `RELAY_URL` or `primary_url` does not get that bypass.

### Smoke test

`GET /health` is unauthenticated and returns `{"ok": true}`. Every other route without the bearer returns 401. An open `/health` does not open the queue.

```sh
curl -sS "http://127.0.0.1:18790/health"          # on the server machine
curl -sS -o /dev/null -w '%{http_code}\n' \
  "http://127.0.0.1:18790/tasks/next"             # expect 401
```

Replace the origin with the base URL that client actually uses. You want the JSON body, not an HTML 404 from a proxy.

The box agent and the worker are otherwise unchanged. Wake files, tool argv templates, and the LaunchAgent for `mac/relay-worker.py` are in the README. Tool templates stay in `examples/worker.example.json`.

## 1. Relay on the Mac, box over Tailscale

The server and the worker both run on the Mac. The box and the Mac are on the same tailnet. The box enqueues to the Mac’s Tailscale IPv4 address. No public port is required for mac-relay itself.

| | Value |
|---|---|
| Server | Mac, `RELAY_BIND=0.0.0.0`, `RELAY_PORT=18790` |
| Box `RELAY_URL` | `http://<mac-100.x>:18790` |
| Box `RELAY_FALLBACK_URL` | unset |
| Worker `primary_url` | `http://127.0.0.1:18790` (preferred) or the same `http://<mac-100.x>:18790` |
| Worker `fallback_url` | unset |

Prefer `http://127.0.0.1:18790` for the worker. It is the same process, and the primary URL does not bypass a system proxy. Use the Tailscale URL for the worker only when you have a reason to leave the machine; it is a valid base URL if `RELAY_BIND` includes that address.

### Prerequisites

- Python 3 on the Mac (`/usr/bin/python3`, the same interpreter the worker plist uses).
- Tailscale up on the Mac and on the box, and a tailnet ACL that allows the box to open TCP `18790` on the Mac. ACL syntax is Tailscale’s: <https://tailscale.com/kb/1018/acls>.
- The Mac stays awake and on the network. `KeepAlive` in launchd restarts a dead process. It does not prevent sleep. Lid close and idle sleep take the server, and therefore the queue, with them. Staying awake is a macOS setting (`caffeinate`, or Energy Saver), not a mac-relay flag.
- A clone of this repo on the Mac, for `relay/server.py`. The worker copy installed by the README is only `mac/relay-worker.py`.

### Secret, bind, port

```sh
mkdir -p ~/.config/mac-relay/data ~/Library/Logs/mac-relay
openssl rand -hex 32 > ~/.config/mac-relay/secret
chmod 600 ~/.config/mac-relay/secret
```

Copy that file to the box path you will put in `RELAY_SECRET_FILE`, mode `600`. The worker on this Mac can use the same `~/.config/mac-relay/secret` path. Keep the server data directory and the worker `state_dir` separate. The server stores `relay.db` under `RELAY_DATA_DIR`. The worker stores task dirs under `state_dir`.

Set `RELAY_DATA_DIR` to an absolute path. A LaunchAgent’s working directory is `/` unless you set one, so the default `./data` would be `/data`.

`RELAY_BIND=0.0.0.0` is what makes the box’s `http://<mac-100.x>:18790` reach the process and what makes the worker’s `http://127.0.0.1:18790` work at the same time. Binding only the Tailscale address drops localhost. Binding the default `127.0.0.1` drops the box.

### Run the server

Foreground, until the smoke test passes. Stop it with Ctrl-C before you load the agent, so the port is free.

```sh
export RELAY_SECRET_FILE="$HOME/.config/mac-relay/secret"
export RELAY_DATA_DIR="$HOME/.config/mac-relay/data"
export RELAY_BIND=0.0.0.0
export RELAY_PORT=18790
/usr/bin/python3 /path/to/mac-relay/relay/server.py
```

Stderr should include `mac-relay listening on 0.0.0.0 18790`. On the Mac:

```sh
curl -sS http://127.0.0.1:18790/health
curl -sS "http://$(tailscale ip -4):18790/health"
```

Both must print `{"ok": true}`. If only localhost works, the process is still bound to `127.0.0.1`.

Then a LaunchAgent, separate from the worker agent. launchd does not expand `~` or `$HOME`. Replace `YOU` and the clone path.

`~/Library/LaunchAgents/com.you.mac-relay.server.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.you.mac-relay.server</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/YOU/src/mac-relay/relay/server.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>RELAY_BIND</key><string>0.0.0.0</string>
    <key>RELAY_PORT</key><string>18790</string>
    <key>RELAY_DATA_DIR</key><string>/Users/YOU/.config/mac-relay/data</string>
    <key>RELAY_SECRET_FILE</key><string>/Users/YOU/.config/mac-relay/secret</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/YOU/Library/Logs/mac-relay/server.log</string>
  <key>StandardErrorPath</key><string>/Users/YOU/Library/Logs/mac-relay/server.log</string>
</dict>
</plist>
```

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.you.mac-relay.server.plist
```

If you change the plist later: `launchctl bootout gui/$(id -u)/com.you.mac-relay.server`, then `bootstrap` again. If the log says `RELAY_SECRET_FILE is required` or `secret too short`, `KeepAlive` will restart that exit in a loop until the file is fixed.

Install the worker with the README “Mac install” steps, and use the `worker.json` below instead of the example URLs. The worker plist template is `mac/com.example.mac-relay.worker.plist`. Its label must not be the server label.

### box.env

On the box, `~/.mac-relay.env` (the file the README sources before `bin/relay-box-agent` and `bin/relay-send`):

```sh
RELAY_URL=http://100.x.y.z:18790
RELAY_SECRET_FILE=/path/to/relay/secret
RELAY_RESULTS_DIR=/workspace/relay-results
RELAY_WAKE_DIR=/workspace/relay/wake
```

`100.x.y.z` is the Mac’s address from `tailscale ip -4`, not the box’s, and not a `*.ts.net` name unless you have deliberately put a TLS proxy in front. There is no `/relay` suffix and no `:18791`. Do not set `RELAY_FALLBACK_URL`.

Wake files are the same as the README: `/workspace/relay/wake/<slug>.url` and `<slug>.key`. Then:

```sh
set -a; . ~/.mac-relay.env; set +a
nohup bin/relay-box-agent >> /workspace/relay/agent.log 2>&1 &
```

Restart the agent after any env change. `RELAY_BIND` does not belong in this file; the box does not run the server in this layout.

### worker.json

`~/.config/mac-relay/worker.json`:

```json
{
  "primary_url": "http://127.0.0.1:18790",
  "secret_file": "~/.config/mac-relay/secret",
  "state_dir": "~/Library/Application Support/mac-relay",
  "worker_name": "mac",
  "fail_threshold": 3,
  "cooldown_s": 300,
  "max_backoff_s": 60,
  "tools": {
    "hermes": ["/path/to/hermes", "run", "--prompt-file", "{prompt_file}", "--out", "{outbox}"],
    "deepseek": ["/path/to/deepseek-harness", "--prompt-file", "{prompt_file}", "--inbox", "{inbox}"],
    "codex": ["codex", "exec", "-m", "gpt-6.1-sol", "{prompt}"]
  }
}
```

Leave `fallback_url` and `fallback_source_ip` out. The `tools` values are the placeholders from `examples/worker.example.json`; replace them with the commands you actually run. `secret_file` and `state_dir` expand `~`. The server LaunchAgent environment does not.

The other legal `primary_url` is `http://100.x.y.z:18790` (this Mac’s Tailscale address). It has to survive the system proxy. `127.0.0.1` is the one to use unless that is unsuitable.

### Failover

Not used. One base URL on each side. The box and the worker already retry their own long-poll with backoff when that URL is down. They will not switch anywhere else unless you set a second URL yourself.

A second URL is worth adding only when you actually have one (layout 3). Inventing a fallback that points at the same host does nothing useful.

### Pros and cons

Compared with the VPS path: there is no second machine, no Caddy, and no public DNS name. The queue exists only while this Mac is awake and the process is running. Traffic to the box is HTTP inside Tailscale, not the HTTPS vhost from the checklist. The server is not bound to localhost, so the Mac firewall (and not only the tailnet ACL) decides who else on the LAN can open port `18790`. A VPN or proxy that shadows `100.64.0.0/10` can make the Mac’s Tailscale address unreachable from the box; the worker proxy-bypass does not apply, because the box is using its primary URL.

### Smoke

On the Mac, both curls in “Run the server” return `{"ok": true}`.

On the box, with the tailnet up:

```sh
curl -sS --max-time 5 http://100.x.y.z:18790/health
curl -sS -o /dev/null -w '%{http_code}\n' --max-time 5 http://100.x.y.z:18790/tasks/next
```

Expect `{"ok": true}` and `401`. Connection refused means nothing is listening on that address (bind still localhost, or the server is stopped). A timeout means routing, ACL, firewall, or the Mac is asleep. Then `bin/relay-send <slug> codex 'ping'` should print a task id, and the worker log should show it starting.

## 2. Relay on the box, Mac over Tailscale

The server runs on the Linux box (the machine that already runs `relay-box-agent`). The Mac does not host the queue. It only long-polls over Tailscale. No public port is required.

| | Value |
|---|---|
| Server | box, `RELAY_BIND=0.0.0.0`, `RELAY_PORT=18790` |
| Box `RELAY_URL` | `http://127.0.0.1:18790` |
| Box `RELAY_FALLBACK_URL` | unset |
| Worker `primary_url` | `http://<box-100.x>:18790` |
| Worker `fallback_url` | unset |

### Prerequisites

- Python 3 on the box, and a writable data directory.
- Tailscale up on the box and on the Mac. The Mac must be allowed to open TCP `18790` on the box (<https://tailscale.com/kb/1018/acls>).
- The box stays on. The Mac only needs to be on the tailnet while you want tasks to run.

### Secret, bind, port

Same secret file rules as above. Two different env blocks on the box: one for the server process, one for the agent. Do not put `RELAY_URL` in the server env and expect the server to use it.

The listen address has to include both localhost (the agent) and the Tailscale address (the Mac). That is `RELAY_BIND=0.0.0.0`. The unit file in the repo still says `127.0.0.1`. You have to change it. Binding only the Tailscale address would make `RELAY_URL=http://127.0.0.1:18790` fail.

`0.0.0.0` also listens on any public or LAN address the box has. Firewall TCP `18790` so only the tailnet can open it, or accept that those interfaces can reach `/health` and try the bearer token.

### Run the server

**systemd (same unit as the VPS install, different bind).** Follow the README “Server install” through the secret and `deploy/mac-relay.service` (`User=mac-relay`, `RELAY_DATA_DIR=/var/lib/mac-relay`, `RELAY_SECRET_FILE=/etc/mac-relay/secret`). The unit sets `ProtectHome=true`, so those paths have to stay on `/etc` and `/var/lib`, not under `/home`.

Before `systemctl enable --now`, edit the installed unit so this line replaces the localhost one:

```
Environment=RELAY_BIND=0.0.0.0
```

`sudo systemctl daemon-reload && sudo systemctl restart mac-relay`. Do not paste `deploy/Caddyfile.snippet`. There is no `/relay` prefix in this layout, and you do not create the checklist’s port `18791`.

**Without systemd.** A long-running process under the account you already use:

```sh
install -d -m 700 "$HOME/.config/mac-relay"
openssl rand -hex 32 > "$HOME/.config/mac-relay/secret"
chmod 600 "$HOME/.config/mac-relay/secret"
export RELAY_SECRET_FILE="$HOME/.config/mac-relay/secret"
export RELAY_DATA_DIR="$HOME/.config/mac-relay/data"
export RELAY_BIND=0.0.0.0
export RELAY_PORT=18790
nohup python3 /path/to/mac-relay/relay/server.py >> "$HOME/.config/mac-relay/server.log" 2>&1 &
```

Either way, on the box:

```sh
curl -sS http://127.0.0.1:18790/health
```

That must print `{"ok": true}`. It does not prove the bind. From the Mac, `curl -sS --max-time 5 http://<box-100.x>:18790/health` does. Read `<box-100.x>` with `tailscale ip -4` on the box.

### box.env

```sh
RELAY_URL=http://127.0.0.1:18790
RELAY_SECRET_FILE=/etc/mac-relay/secret
RELAY_RESULTS_DIR=/workspace/relay-results
RELAY_WAKE_DIR=/workspace/relay/wake
```

If the server is the non-systemd process, `RELAY_SECRET_FILE` is `$HOME/.config/mac-relay/secret` instead. The agent and the server must be able to read it. Do not set `RELAY_FALLBACK_URL`.

Source the file and start `bin/relay-box-agent` as in the README. Wake files are unchanged.

### worker.json

Follow the README “Mac install”. `~/.config/mac-relay/worker.json`, with `tools` copied from `examples/worker.example.json` (not repeated here):

```json
{
  "primary_url": "http://100.x.y.z:18790",
  "secret_file": "~/.config/mac-relay/secret",
  "state_dir": "~/Library/Application Support/mac-relay",
  "worker_name": "mac",
  "fail_threshold": 3,
  "cooldown_s": 300,
  "max_backoff_s": 60,
  "tools": {}
}
```

Replace `100.x.y.z` with the box Tailscale address and replace `tools` with your real argv templates. An empty `tools` object starts the worker but fails every task (`no command template`). Omit `fallback_url` and `fallback_source_ip`. Delete a leftover `<state_dir>/conn.json` from an older config that had a fallback.

This `primary_url` is an `http://100.x` URL, so it uses the default opener and will follow a Mac system proxy. If Shadowrocket or another VPN routes `100.64.0.0/10` into the proxy, the worker cannot reach the box. Excluding the tailnet in that proxy is an OS setting. mac-relay will not do it for a primary URL. `fallback_source_ip` does not apply, because there is no fallback.

### Failover

Not used. The Mac has one URL. The box talks to localhost. When the Mac is offline the server keeps queued tasks in SQLite; they do not run until the worker long-polls again. When the box is offline, nothing is queued anywhere else.

### Pros and cons

Compared with the VPS path: the always-on machine is the box you already have, not a VPS, and there is still no public listener if the firewall and the ACL are tight. You give up Caddy’s HTTPS vhost and the `/relay` prefix. The Mac reaches the box with cleartext HTTP inside Tailscale, and that address is the worker’s primary URL, so a system proxy can break it (see shared rules). The box has to stay reachable on the tailnet. Mac asleep or offline leaves tasks queued, which matches the VPS path; it does not match layout 1, where an asleep Mac is a down server.

### Smoke

On the box: `curl -sS http://127.0.0.1:18790/health` → `{"ok": true}`.

On the Mac: `curl -sS --max-time 5 http://100.x.y.z:18790/health` → `{"ok": true}`, and the same URL with path `/tasks/next` and no bearer → `401`. Then a `relay-send` from the box should be claimed by the worker.

## 3. Local relay plus a tunnel

The server still runs on the Mac or on the box, bound where the tunnel can reach it. Cloudflare Tunnel or Tailscale Funnel presents an HTTPS URL. Use this when the box cannot join the tailnet, or when you need a stable HTTPS base URL and you are not running the VPS + Caddy path.

The server still has no TLS and no `/relay` route. The tunnel terminates TLS and forwards to `http://127.0.0.1:18790`. Client base URLs are the tunnel origin, with no `/relay` suffix and no `:18790`, as long as the tunnel publishes the server root. If you also put `deploy/Caddyfile.snippet` in front, the base URL gains `/relay` and the tunnel must target Caddy, not the Python port. These steps do not do that.

| | Tunnel only | Tunnel and Tailscale both work |
|---|---|---|
| Server bind | `127.0.0.1` (the default) | `0.0.0.0`, so `100.x:18790` is reachable too |
| Co-located client (server on the same machine) | `http://127.0.0.1:18790` | `http://127.0.0.1:18790` |
| Remote client primary | `https://<tunnel-host>` | `https://<tunnel-host>` |
| Remote client fallback | unset | `http://<server-100.x>:18790` |

Prefer localhost for whichever of the box agent or the Mac worker shares a machine with the server. That connection does not depend on the tunnel. The other side uses the public URL. Private Tailscale is better than the public URL when the remote side can use it; the failover row is how to keep both, and it matches the code (public primary, tailnet fallback, fallback skips the proxy).

### Prerequisites

- A running server from layout 1 or layout 2, or the same `python3 relay/server.py` command, initially with the default bind.
- `cloudflared` or the Tailscale CLI, installed from that product’s docs. This repo does not ship either binary.
- The bearer secret, as in the shared rules. A tunnel does not authenticate mac-relay. `/health` is public. Everything else needs the bearer.

Point the tunnel at localhost only after `curl -sS http://127.0.0.1:18790/health` on that machine returns `{"ok": true}`.

If the remote side will also use `http://<server-100.x>:18790` as a fallback, switch `RELAY_BIND` to `0.0.0.0` and re-check that URL from the remote machine. Funnel and `cloudflared` as documented below connect to `127.0.0.1`, so a process bound only to the Tailscale address will not receive their traffic. `0.0.0.0` covers both.

### Cloudflare Tunnel

Docs: <https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/> and, for a hostname that survives restarts, <https://developers.cloudflare.com/tunnel/setup/>.

Quick tunnel, on the same machine as the server, after replacing the port in Cloudflare’s example:

```sh
cloudflared tunnel --url http://127.0.0.1:18790
```

`cloudflared` prints a `https://<random>.trycloudflare.com` URL. That origin is the client base URL. It stops working when the process stops, and the hostname changes the next time you start it. Cloudflare documents quick tunnels as a test tool: no uptime guarantee, and more than 200 in-flight requests get HTTP 429. A 429 is a 4xx, so the box agent and the worker count it as an application response, not as a failover failure. They log an error and retry with backoff. They do not switch URLs because of it.

Do not put Cloudflare’s quick-tunnel `--allowed-mail` flag in front of this service. Cloudflare’s docs describe it as a browser one-time PIN, and they say it does not support non-interactive clients. `relay-send`, `relay-box-agent`, and `relay-worker.py` are non-interactive. They only send `Authorization: Bearer`.

For a stable name, create a named tunnel in the Cloudflare dashboard or API and set the published service URL to `http://127.0.0.1:18790`. The hostname you attach is the base URL. Login, DNS, and Cloudflare’s config file are covered in their setup doc; this page does not add flags beyond that service URL.

Quick tunnels do not support Server-Sent Events. mac-relay does not use SSE. It holds a long-poll open for 50 seconds (`wait=50` in the worker and the box agent; the server caps `wait` at 55). That number is hardcoded. There is no env or `worker.json` field for it. If a tunnel closes those connections early, the clients treat it as a connection error, back off (the worker’s cap is `max_backoff_s`, default 60; the box agent’s cap in `bin/relay-box-agent` is 60 seconds), and try again.

### Tailscale Funnel

Docs: <https://tailscale.com/kb/1223/funnel>. Current docs expose a local port with:

```sh
tailscale funnel 18790
```

The CLI prints a public `https://<node>.<tailnet>.ts.net` URL whose `/` proxies to `http://127.0.0.1:18790`. That origin, with no path suffix and no port, is the client base URL. The documented command runs in the foreground (`Press Ctrl+C to exit`). Leave it running the way the CLI on that machine actually supports; check `tailscale funnel --help` before adding flags. Tailscale 1.52 changed this CLI.

Funnel requirements and limits that matter here, from those docs:

- MagicDNS and HTTPS certificates enabled, and Funnel allowed for the node (the admin console can turn this on).
- The public listeners are ports 443, 8443, and 10000. `tailscale funnel 18790` in the current example publishes the local port on the HTTPS origin, so clients do not put `:18790` on the `https://` URL.
- Anyone on the internet who has the URL can open it. Funnel is not an ACL-limited tailnet address.
- On macOS, Funnel needs an open-source Tailscale variant. The sandboxed Mac App Store client cannot serve Funnel.
- Funnel has non-configurable bandwidth limits.

Tailscale Serve (`tailscale serve 18790`, <https://tailscale.com/kb/1242/tailscale-serve>) proxies the same local port but only inside the tailnet. Layouts 1 and 2 do not need it; they use `http://100.x:18790` directly. Serve and Funnel cannot own the same public port at once. The later command wins, and a `serve` on that port makes it private.

### box.env and worker.json

**Server on the Mac, box not on the tailnet.** The box has no `100.x` route, so it uses the tunnel. The worker is co-located, so it uses localhost.

Box `~/.mac-relay.env`:

```sh
RELAY_URL=https://<tunnel-host>
RELAY_SECRET_FILE=/path/to/relay/secret
RELAY_RESULTS_DIR=/workspace/relay-results
RELAY_WAKE_DIR=/workspace/relay/wake
```

Worker `primary_url`:

```json
"primary_url": "http://127.0.0.1:18790"
```

No `RELAY_FALLBACK_URL`, no `fallback_url`. `<tunnel-host>` is the quick-tunnel hostname, the named-tunnel hostname, or `<node>.<tailnet>.ts.net`, with no `/relay` and no port.

**Both the tunnel and Tailscale can reach the server.** Remote client (the box if the server is on the Mac, or the Mac worker if the server is on the box):

```sh
RELAY_URL=https://<tunnel-host>
RELAY_FALLBACK_URL=http://100.x.y.z:18790
RELAY_FAIL_THRESHOLD=3
RELAY_COOLDOWN_S=300
```

```json
{
  "primary_url": "https://<tunnel-host>",
  "fallback_url": "http://100.x.y.z:18790",
  "fallback_source_ip": "100.a.b.c",
  "secret_file": "~/.config/mac-relay/secret",
  "state_dir": "~/Library/Application Support/mac-relay",
  "worker_name": "mac",
  "fail_threshold": 3,
  "cooldown_s": 300,
  "max_backoff_s": 60,
  "tools": {}
}
```

`100.x.y.z` is the server machine’s Tailscale address. `100.a.b.c` is the Mac’s own Tailscale address, and only on the worker, and only when `fallback_url` is `http://`. Omit `fallback_source_ip` rather than copying the descriptive text from `examples/worker.example.json`. The co-located client still uses `http://127.0.0.1:18790` alone. Replace the empty `tools` object from `examples/worker.example.json`.

This split is deliberate: the tailnet URL sits on the fallback because that path skips the system proxy. Putting `http://100.x:18790` on `primary_url` and the tunnel on `fallback_url` inverts the implemented bypass.

The box can be pointed at `https://<tunnel-host>` even when the server runs on the box. That hairpins through the tunnel. Prefer `http://127.0.0.1:18790` for the co-located agent so a tunnel outage does not stop local enqueue.

### Failover

Unset unless the remote client has both URLs in the second example. Then the usual rule applies: three consecutive connection errors or HTTP 5xx on the tunnel URL, then sticky use of `http://100.x:18790`, then a primary `/health` probe after `cooldown_s`. `relay-send` tries the fallback on the first failure. A dead tunnel and a dead tailnet together have nowhere else to go. There is no third URL.

The co-located localhost client does not fail over. If you previously saved a fallback mode, clear `<state_dir>/conn.json` or touch `reset-primary` after you remove `fallback_url`.

### Pros and cons

Compared with the VPS path: you do not run a VPS or Caddy, and you do get an HTTPS URL. TLS stops at Cloudflare or at Tailscale’s Funnel proxy; `server.py` still speaks HTTP on localhost. Hardening is whatever the tunnel product does, plus the bearer secret, not `deploy/mac-relay.service`’s `ProtectSystem` / dedicated user, unless you also installed that unit. Funnel and a Cloudflare quick tunnel are reachable by anyone who has the URL. The secret is mandatory, and it is the only auth these clients send. A quick-tunnel hostname is not stable. Funnel on macOS needs the open-source client. This is not a substitute for the checklist’s public vhost, body-size limit, and long-poll timeouts in `deploy/Caddyfile.snippet`.

Where the queue lives follows the machine running `server.py`. Mac asleep: the queue is down if the server is on the Mac (layout 1), and up if the server is on the box (layout 2). The tunnel being down blocks remote clients only. Localhost clients still talk to the process.

### Smoke

On the server machine:

```sh
curl -sS http://127.0.0.1:18790/health
```

From any machine that should use the tunnel, using the printed origin and no extra path:

```sh
curl -sS --max-time 20 "https://<tunnel-host>/health"
curl -sS -o /dev/null -w '%{http_code}\n' --max-time 20 "https://<tunnel-host>/tasks/next"
```

Expect `{"ok": true}` and `401`. If `/health` is 404 HTML, the tunnel is not aimed at the server root, or a client has a `/relay` prefix the server never had. If you configured the tailnet fallback, `curl -sS --max-time 5 http://100.x.y.z:18790/health` from the remote machine must also return `{"ok": true}` before you set `RELAY_FALLBACK_URL` / `fallback_url`. Then `relay-send` through `RELAY_URL`.
