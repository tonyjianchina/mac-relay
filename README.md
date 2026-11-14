# mac-relay

A small, self-hosted task relay. Box agents put tasks on a queue, and a Mac worker runs them with local tools (hermes, a DeepSeek harness, or `codex exec`). Results and files come back to the box. `relay-send` waits until the Mac has accepted the task (or it has already finished). The bot that sent the task gets one `done`/`failed` wake with files. A `started` event is not sent to the sender webhook.

```
box bot ──relay-send──▶ relay server (VPS, behind Caddy /relay/) ◀──long-poll── Mac worker
   ▲                          │                                         runs tool, uploads result
   └──done/failed wake◀──relay-box-agent (long-poll results)
```

* Several relay servers can be used at once (for example a box relay over Tailscale as primary and a VPS relay as backup). Every component talks to every channel; there is no sticky failover state. See [docs/channels.md](docs/channels.md).
* Large files: chunked, resumable, sha256-checked uploads; Range-resumed, verified downloads. A partial file never appears under its final name.
* Python 3 standard library only. No other dependencies.
* State is kept in SQLite under `RELAY_DATA_DIR`. Enqueueing is idempotent on task id, and the first result for a task is the only one stored.
* Workers wait on long-polls, not timers. There is no heartbeat routine and no cron job.
* Auth uses a bearer secret read from `RELAY_SECRET_FILE`. The secret is never logged.

## API
| Method | Path | Purpose |
|---|---|---|
| GET | `/status` | Authenticated. Protocol version, `features`, and worker last-seen seconds. 404 on servers older than v2 |
| GET | `/health` | Unauthenticated. Default body `{"ok": true, "soft_body": 16777216, "max_body": 536870912}` |
| POST | `/tasks` | Enqueue (multipart or JSON): `id, sender, tool∈{hermes,deepseek,codex}, prompt`, plus file parts as attachments. Multipart is capped at `RELAY_MAX_SOFT_BODY` (16MiB) |
| GET | `/tasks/next?worker=mac&wait=50` | Worker long-poll. Returns 200 with a task, or 204 |
| POST | `/tasks/{id}/started` | Worker confirms the claim (`state=running`, visible on `GET /tasks/{id}`). Does not queue a wake. Unconfirmed claims redeliver after `RELAY_CLAIM_TTL` (20 s) |
| PUT | `/tasks/{id}/files/{name}?kind=out\|in` | Stream one file to disk (`application/octet-stream`, `Content-Length` required, up to `RELAY_MAX_BODY`). The task must already exist. Worker outputs use `kind=out` |
| POST | `/tasks/{id}/result` | JSON `{"status":"done"\|"failed","summary":"…","files":["name.bin"]}` and/or a multipart body at or under the soft cap. `files[]` records names already stored by PUT. The first result wins |
| GET | `/tasks/{id}` | Task status |
| GET | `/results/next?wait=50` | Box-agent long-poll for undelivered `done`/`failed` results |
| POST | `/results/{id}/ack` | Ack a `done`/`failed` delivery. `{"event":"started"}` is accepted and does not mark the result delivered |
| GET | `/files/{id}/in\|out/{name}` | Download an attachment or an output file, streamed in 1MiB chunks |

Task ids and sender slugs stay ASCII: `[A-Za-z0-9._-]{1,128}`. Attachment and result **file names** are one path segment and may be non-ASCII (`验收结果.md`). A name is `400 {"error":"bad name"}` when it is empty, `.`, or `..`, when it contains a slash, a backslash, or NUL, or when it is longer than 128 characters (or 247 UTF-8 bytes).

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

`/health` on a default install is `{"ok": true, "soft_body": 16777216, "max_body": 536870912}`.

## File sizes

Two limits, both bytes, both optional env vars on the server:

| Env | Default | Applies to |
|---|---|---|
| `RELAY_MAX_SOFT_BODY` | 16777216 (16MiB) | `multipart/*` only. A larger `Content-Length` returns 413 with `Connection: close`. The body is not read and is not parsed |
| `RELAY_MAX_BODY` | 536870912 (512MiB) | Every other body, including `PUT /tasks/{id}/files/{name}` |

Worker outputs use the PUT path. For each stdout, stderr, and outbox file, `mac/relay-worker.py` streams the file with `PUT /tasks/{id}/files/{name}?kind=out`, then POSTs `application/json` to `/tasks/{id}/result`:

```json
{"status": "done", "summary": "last stdout line", "files": ["stdout.txt", "notes.txt"]}
```

`files` is an array of names already on disk under that task's `out` directory. A name that was not PUT is ignored. `max_soft_file` in `worker.json` (default 16MiB) is written to the worker log at startup. It does not change the upload path: the worker always uses PUT + JSON.

**Task input attachments (`bin/relay-send --file`)**: on a v2 server the task is created `staged`, each file goes up with chunked resumable PUT (`kind=in`, sha256 checked), then `POST /tasks/{id}/commit` makes it claimable. Old servers still get multipart, capped at 16MiB. Chunked PUT, Range downloads, and the other v2 calls are described in [docs/channels.md](docs/channels.md).

Upgrade a relay that is already running: replace `relay/server.py` and `mac/relay-worker.py`, then restart the server and the worker in the same window. The systemd unit does not need a new setting. An old worker can still POST a multipart result of at most 16MiB. A larger multipart result gets 413 until that worker is updated, and the server closes the connection instead of buffering it. Caddy's `request_body max_size 512MB` matches `RELAY_MAX_BODY`. It does not lift the 16MiB multipart cap.

## Without a VPS

The install above is the primary path: a Linux VPS, systemd, and Caddy in front of a server bound to `127.0.0.1`. The three layouts below are alternatives when you do not want that VPS. They run the same `relay/server.py`. It has no TLS. Unless you set `RELAY_BIND`, it listens only on `127.0.0.1`, not on the tailnet. Nothing in the server adds a `/relay` prefix — that prefix exists only where Caddy `handle_path /relay/*` strips it. Steps, URLs, and the tradeoff against this VPS path are in [docs/no-vps.md](docs/no-vps.md) (Chinese: [docs/no-vps.zh-CN.md](docs/no-vps.zh-CN.md)).

1. **Relay on the Mac.** The box and the Mac are on the same Tailscale. The box uses `RELAY_URL=http://<mac-100.x>:18790`. The Mac has to stay awake. There is no public listener if the tailnet ACL and the Mac firewall are tight.
2. **Relay on the box.** The box runs the server and uses `RELAY_URL=http://127.0.0.1:18790`. The Mac worker uses `primary_url=http://<box-100.x>:18790`. The queue stays up when the Mac sleeps; tasks do not run until the worker is online.
3. **Local relay plus a tunnel.** The server stays on the Mac or the box. Cloudflare Tunnel or Tailscale Funnel gives the other side an HTTPS base URL. When the tunnel targets port 18790 directly, that URL has no `/relay` suffix. The bearer secret is mandatory, because the URL is reachable past the tailnet.

On a single URL, leave `RELAY_FALLBACK_URL` / extra `endpoints` unset. With two relays (for example layout 2 as primary plus the VPS as backup) list both; see [docs/channels.md](docs/channels.md).

## Box install
```sh
cp examples/box.env.example ~/.mac-relay.env   # set RELAY_URL=https://<host>/relay
# copy the shared secret into the file named by RELAY_SECRET_FILE (chmod 600)
mkdir -p /workspace/relay/wake
echo 'https://<bot webhook url>' > /workspace/relay/wake/<slug>.url
echo '<bot webhook key>'          > /workspace/relay/wake/<slug>.key
set -a; . ~/.mac-relay.env; set +a
nohup bin/relay-box-agent >> /workspace/relay/agent.log 2>&1 &
bin/relay-send <slug> codex 'summarise this' --file notes.md   # stdout: task id
```
`relay-send` enqueues, prints the task id on stdout, then polls `GET /tasks/{id}` until `state` is `running` (the worker posted `/started`) or already `done`/`failed`. The default wait is 45s (`RELAY_STARTED_WAIT_S`). Success logs `accepted` on stderr and exits 0. If the wait ends first, stderr prints `已入队、尚未接单` and the process exits 2; the task id is still on stdout. An enqueue failure exits 1.

The agent wakes the sender once, on `done`/`failed`, after downloading `/workspace/relay-results/<id>/{result.json, files…}`. It does not POST the sender webhook for `status=started`. A stale started event is acked and recorded in `.wake-started` as `skipped/no-webhook`. The done/failed wake is at-most-once (`.wake`). Body shape is `{task_id, status, summary, sender, result_path}`. If a slug has no `.url`/`.key`, that wake logs one error and does not retry.

**More channels:** `RELAY_FALLBACK_URL` is a comma-separated list of backup base URLs. `relay-box-agent` long-polls results on all of them at once and wakes once per task id. `relay-send` enqueues on the first channel that answers and hands the same task id to the next channel if the Mac has not accepted it within `RELAY_HANDOFF_S` (default 20), withdrawing the first copy. Requests to `100.x`, `*.ts.net`, and localhost bypass any proxy. Long prompts: `relay-send <slug> <tool> - < prompt.txt` or `--prompt-file`. Restart the agent after changing the env. Details and failure cases: [docs/channels.md](docs/channels.md).

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
Tool templates are argv lists. They can use `{prompt} {prompt_file} {inbox} {outbox} {task_id}`. Each task runs detached, in its own session, with no timeout, and with `cwd` set to its outbox. It also gets the env vars `RELAY_INBOX`, `RELAY_OUTBOX`, and `RELAY_TASK_ID`. Every non-empty file the tool writes to the outbox is uploaded with streaming PUT, together with `stdout.txt` and `stderr.txt`, and the result POST is JSON (`files` lists those names). The summary is the last non-empty stdout line. On failure it is `exit N: <last stderr line>`.

**Restarts:** a task keeps running if the worker dies. After a restart, the worker uploads every finished task that has no `uploaded` marker. If a process vanished, for example after a reboot, the worker reports it once as `failed`.

One task's upload failure does not stop the others in that sweep. On HTTP errors the worker logs the response body. `upload_4xx_quarantine` (default 3) consecutive HTTP 4xx responses write a `quarantine` marker in the task directory and stop retries. The count is the `upload-4xx` file in that directory (it survives restarts). The task's files are left in place.

**Channels:** `endpoints` is an ordered list of `{"name", "url"}` (first = primary). The worker long-polls all of them at once; a task id claimed twice runs once. Results go back where the task came from; after `mirror_after_s` (default 60) without success and without upload progress, the worker mirrors the task to another channel and delivers there, then closes the stale row on the origin after `closeout_after_s` (default 600). Old configs with `primary_url` / `fallback_url` still work; `fail_threshold`, `cooldown_s`, `conn.json` and `reset-primary` are no longer used. Retry backoff is exponential and capped at `max_backoff_s`. `stdin_prompt_over` (`{"tool": bytes}`) passes long prompts on stdin. See [docs/channels.md](docs/channels.md).

## Tests
`python3 tests/selftest.py` runs everything over loopback, with stub tools and a stub webhook, and writes `TESTS.md`. `--quick` shortens the long task.

## License
MIT
