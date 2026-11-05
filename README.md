# mac-relay

A small, self-hosted task relay. Box agents put tasks on a queue, and a Mac worker runs them with local tools (hermes, a DeepSeek harness, or `codex exec`). Results and files come back to the box, and the bot that sent the task gets exactly one wake call.

```
box bot ──relay-send──▶ relay server (VPS, behind Caddy /relay/) ◀──long-poll── Mac worker
   ▲                          │                                         runs tool, uploads result
   └──one wake◀──relay-box-agent (long-poll results, download files)
```

* Python 3 standard library only. No other dependencies.
* State is kept in SQLite under `RELAY_DATA_DIR`. Enqueueing is idempotent on task id, and the first result for a task is the only one stored.
* Workers wait on long-polls, not timers. There is no heartbeat routine and no cron job.
* Auth uses a bearer secret read from `RELAY_SECRET_FILE`. The secret is never logged.

## API
| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Unauthenticated liveness check |
| POST | `/tasks` | Enqueue (multipart or JSON): `id, sender, tool∈{hermes,deepseek,codex}, prompt`, plus file parts as attachments |
| GET | `/tasks/next?worker=mac&wait=50` | Worker long-poll. Returns 200 with a task, or 204 |
| POST | `/tasks/{id}/started` | Worker confirms the claim. An unconfirmed claim is redelivered after `RELAY_CLAIM_TTL` (20 s) |
| POST | `/tasks/{id}/result` | Multipart `status=done|failed`, `summary`, and file parts. The first result wins |
| GET | `/tasks/{id}` | Task status |
| GET | `/results/next?wait=50` | Box-agent long-poll for finished, undelivered results |
| POST | `/results/{id}/ack` | Box agent marks a result as delivered |
| GET | `/files/{id}/in|out/{name}` | Download an attachment or an output file |

## Server install (Linux, systemd + Caddy)
```sh
sudo useradd --system --home /var/lib/mac-relay mac-relay
sudo git clone <this repo> /opt/mac-relay        # or copy the files
sudo install -d -m 700 -o mac-relay /etc/mac-relay
openssl rand -hex 32 | sudo tee /etc/mac-relay/secret >/dev/null
sudo chown mac-relay /etc/mac-relay/secret && sudo chmod 600 /etc/mac-relay/secret
sudo cp deploy/mac-relay.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now mac-relay
curl -s http://127.0.0.1:18790/health
```
Paste `deploy/Caddyfile.snippet` (`handle_path /relay/*` → `127.0.0.1:18790`) into the site block. Then run `caddy validate --config /etc/caddy/Caddyfile` and `systemctl reload caddy`. See `deploy/CHECKLIST.md`.

## Without a VPS

The install above is the primary path: a Linux VPS, systemd, and Caddy in front of a server bound to `127.0.0.1`. The three layouts below are alternatives when you do not want that VPS. They run the same `relay/server.py`. It has no TLS. Unless you set `RELAY_BIND`, it listens only on `127.0.0.1`, not on the tailnet. Nothing in the server adds a `/relay` prefix — that prefix exists only where Caddy `handle_path /relay/*` strips it. Steps, URLs, and the tradeoff against this VPS path are in [docs/no-vps.md](docs/no-vps.md) (Chinese: [docs/no-vps.zh-CN.md](docs/no-vps.zh-CN.md)).

1. **Relay on the Mac.** The box and the Mac are on the same Tailscale. The box uses `RELAY_URL=http://<mac-100.x>:18790`. The Mac has to stay awake. There is no public listener if the tailnet ACL and the Mac firewall are tight.
2. **Relay on the box.** The box runs the server and uses `RELAY_URL=http://127.0.0.1:18790`. The Mac worker uses `primary_url=http://<box-100.x>:18790`. The queue stays up when the Mac sleeps; tasks do not run until the worker is online.
3. **Local relay plus a tunnel.** The server stays on the Mac or the box. Cloudflare Tunnel or Tailscale Funnel gives the other side an HTTPS base URL. When the tunnel targets port 18790 directly, that URL has no `/relay` suffix. The bearer secret is mandatory, because the URL is reachable past the tailnet.

On a single URL, leave `RELAY_FALLBACK_URL` / `fallback_url` unset. Set them only when you really have two base URLs, for example a tunnel URL plus `http://100.x:18790`.

## Box install
```sh
cp examples/box.env.example ~/.mac-relay.env   # set RELAY_URL=https://<host>/relay
# copy the shared secret into the file named by RELAY_SECRET_FILE (chmod 600)
mkdir -p /workspace/relay/wake
echo 'https://<bot webhook url>' > /workspace/relay/wake/<slug>.url
echo '<bot webhook key>'          > /workspace/relay/wake/<slug>.key
set -a; . ~/.mac-relay.env; set +a
nohup bin/relay-box-agent >> /workspace/relay/agent.log 2>&1 &
bin/relay-send <slug> codex 'summarise this' --file notes.md   # prints the task id
```
For each task, the agent writes `/workspace/relay-results/<id>/{result.json, files…}` and sends one POST to the sender's webhook. The POST carries `Authorization: Bearer <key>` and the body `{task_id, status, summary, sender, result_path}`. If a slug has no `.url`/`.key` file, the agent logs one error and does not retry. Wakes are at-most-once: a marker is written before the POST.

**Box fallback:** set `RELAY_FALLBACK_URL` (for example `http://100.x.y.z:18791`, a Tailscale address of the relay host). `relay-box-agent` uses the same policy as the worker: it switches after `RELAY_FAIL_THRESHOLD` (default 3) consecutive primary failures (connection error or HTTP 5xx), stays on the fallback, and probes the primary `/health` once per `RELAY_COOLDOWN_S` (default 300) before switching back. Fallback requests bypass any proxy. The log shows `connection -> fallback` / `connection -> primary`. `relay-send` also retries once on `RELAY_FALLBACK_URL` if the primary fails (enqueue is idempotent by task id). Restart the agent after changing the env.

## Mac install
```sh
mkdir -p ~/.config/mac-relay ~/Library/Logs/mac-relay
cp mac/relay-worker.py ~/.config/mac-relay/
cp examples/worker.example.json ~/.config/mac-relay/worker.json   # edit URLs + tool commands
# put the shared secret in ~/.config/mac-relay/secret (chmod 600)
sed -e 's#__LABEL__#com.you.mac-relay.worker#' -e "s#__INSTALL_DIR__#$HOME/.config/mac-relay#" \
    -e "s#__CONFIG_PATH__#$HOME/.config/mac-relay/worker.json#" -e "s#__LOG_DIR__#$HOME/Library/Logs/mac-relay#" \
    -e "s#__PATH__#/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin#" \
    mac/com.example.mac-relay.worker.plist > ~/Library/LaunchAgents/com.you.mac-relay.worker.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.you.mac-relay.worker.plist
```
Tool templates are argv lists. They can use `{prompt} {prompt_file} {inbox} {outbox} {task_id}`. Each task runs detached, in its own session, with no timeout, and with `cwd` set to its outbox. It also gets the env vars `RELAY_INBOX`, `RELAY_OUTBOX`, and `RELAY_TASK_ID`. Every file the tool writes to the outbox is uploaded, together with `stdout.txt` and `stderr.txt`. The summary is the last non-empty stdout line. On failure it is `exit N: <last stderr line>`.

**Restarts:** a task keeps running if the worker dies. After a restart, the worker uploads every finished task that has no `uploaded` marker. If a process vanished, for example after a reboot, the worker reports it once as `failed`.

**Fallback:** set `fallback_url` (for example a Tailscale address) to use when the primary is down. The worker switches to it only after `fail_threshold` consecutive primary failures, and then stays on it. It goes back to the primary only when a `/health` probe succeeds after `cooldown_s`, or when you reset it manually with `touch <state_dir>/reset-primary` or `kill -HUP <pid>`. Retry backoff is exponential and capped at `max_backoff_s`.

## Tests
`python3 tests/selftest.py` runs everything over loopback, with stub tools and a stub webhook, and writes `TESTS.md`. `--quick` shortens the long task.

## License
MIT
