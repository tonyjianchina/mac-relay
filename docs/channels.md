# Channels: primary and backup relays

mac-relay can run over more than one relay server at once, for example a relay on the box
reached over Tailscale (primary) and a relay on a VPS (backup). There is no sticky failover
mode to get stuck in. Every component talks to every channel, and the task id ties the copies
together.

| Component | Config | What it does with several channels |
|---|---|---|
| `relay-send` | `RELAY_URL` (primary), `RELAY_FALLBACK_URL` (comma list, in order), `RELAY_BIG_BYTES` (default 8 MiB) | Posts the task to the first channel that accepts the enqueue. If the combined `--file` size is over `RELAY_BIG_BYTES`, the public URL in `RELAY_FALLBACK_URL` (first https fallback, or the first fallback if none is https) is tried first; the other channels follow in their original order if that one cannot take it. If the Mac has not accepted it within `RELAY_HANDOFF_S` (default 20 s), posts the same task id to the next channel and withdraws the first copy if it is still queued. Waits up to `RELAY_STARTED_WAIT_S` (default 45 s) in total, polling every channel it posted to. |
| `relay-box-agent` | same env | Long-polls `/results/next` on every channel at once. A result is delivered (files downloaded, `result.json`, one wake) once per task id. Above `RELAY_BIG_BYTES`, files come from the configured public endpoint when that endpoint has the same complete files; otherwise from the channel that offered the event. The ack goes to the event channel. Copies from other channels are acked only. |
| Mac worker | `endpoints` in `worker.json` (ordered list, first = primary); optional `public_endpoint`, `big_bytes` | Long-polls `/tasks/next` on every endpoint at once. Returns results to the channel(s) it claimed the task from, primary first. If the result files together exceed `big_bytes` (default 8 MiB, or `RELAY_BIG_BYTES`), they go to the public channel first (the first https endpoint, or `public_endpoint` by name or URL) — mirrored there if needed, without waiting for `mirror_after_s`. If the origins fail for `mirror_after_s` (default 60 s), it mirrors the task onto another channel and delivers there. |

Channel state (`channel <name> down: …` / `channel <name> recovered after N s`) is logged once
per transition. A channel counts as down after a connection error, a timeout, or HTTP 5xx.
Recovery is detected by the next successful request; nothing has to be reset by hand.

## Example: box relay over Tailscale first, VPS relay second

Box (`box.env`):

```sh
RELAY_URL=http://127.0.0.1:18790                       # relay on this box
RELAY_FALLBACK_URL=https://relay.example.com/relay,http://100.x.y.z:18791
```

Mac (`worker.json`):

```json
"endpoints": [
  {"name": "box",    "url": "http://100.a.b.c:18790"},
  {"name": "vps-ts", "url": "http://100.x.y.z:18791"},
  {"name": "vps",    "url": "https://relay.example.com/relay"}
],
"mirror_after_s": 60
```

`100.a.b.c` is the box's Tailscale address (the box relay binds `RELAY_BIND=127.0.0.1,100.a.b.c`,
never `0.0.0.0`). `100.x.y.z:18791` is the VPS Caddy listener on its Tailscale address. Requests to
`100.x`, `*.ts.net`, `127.*`, and `localhost` always bypass `HTTP(S)_PROXY` and the macOS system
proxy, on every channel. Old configs with `primary_url` / `fallback_url` still work and become a
two-entry list.

## At most once, never lost

- **Ledger on the Mac.** The task directory is the ledger. The first claim of a task id writes
  `task.json` under a lock. Any later claim of that id, from any channel, is confirmed
  (`/started`) and logged as `duplicate claim … not run again`. This assumes one worker host;
  two Macs polling the same channels would each run a copy.
- **Late enqueue copies.** A handoff copy that the server could not withdraw (already claimed)
  hits the ledger and is only confirmed.
- **Wake once.** The box agent takes a per-task lock and writes `.wake` before the webhook POST.
  A second copy of the result (mirror, closeout, redelivery) finds `.wake` and is acked only.
- **Stale rows.** After a mirror, the origin server still shows the task as running. The worker
  posts a file-less result there after `closeout_after_s` (default 600 s); the box agent acks it.
- **Lost process.** A task whose process vanished (reboot) is reported once as `failed`.
- **Pre-upgrade directories.** A task directory from an older worker that never started a process
  is not run late. Only directories with a v2 `claimed` marker resume attachment downloads.

## Large files

- **Results (Mac to box).** Chunked `PUT …?offset=&total=&sha256=` (8 MiB). The server keeps a
  `.upload` partial, answers `409 {"partial": n}` for a wrong offset, and checks the sha256 before
  the file becomes visible (`422` on mismatch, partial deleted). `GET /uploads/{id}/{name}?kind=`
  returns the resume point, so an upload resumes after a dropped connection, a worker restart,
  or a server restart. A sender that stalls with the socket open frees the upload after
  `RELAY_CHUNK_STALL_S` (default 30 s) and answers `400 {"error":"short body","partial":N}`,
  keeping those bytes. The client resumes from N in the same attempt when N is an integer
  between the offset it just sent and the file size. If N does not move, it retries twice
  and then gives up on that channel. A full 8MiB chunk is larger than a typical TCP window:
  if the peer answers with 409 or this 400 and stops reading, the client reads that response
  instead of blocking in the send until the stall timeout, then retries the chunk. A 409 that
  stays `busy` stops after a few tries and the channel is abandoned. Any other 400 (`bad name`,
  `bad offset/total`, or a body that is not that JSON) stays fatal. A resume point that grew
  since the last attempt
  restarts the mirror clock, so a slow but working channel is not abandoned.
- **Downloads (box agent, worker attachments).** Written to `<name>.partial`, resumed with
  `Range`, checked against the size and sha256 the server recorded, then renamed. A partial file
  never appears under its final name.
- **Attachments (box to Mac).** On v2 servers `relay-send` creates the task as `staged`, uploads
  each `--file` with the same chunked PUT, then commits; the task is not claimable until every
  attachment is complete and verified. Old servers get multipart (16 MiB cap). If the combined
  attachment size is over `RELAY_BIG_BYTES` (default 8 MiB), `relay-send` posts the task to the
  public fallback URL first. The worker also fetches those attachments from the public https
  endpoint first, even if it claimed the task on another URL of the same server (for example the
  VPS tailnet address), and can resume a mid-file download there.
- **Big results (Mac to box).** If the outbox (plus stdout/stderr) is over `big_bytes` (same
  default), the worker uploads to the public https endpoint first, mirroring the task there when
  it was claimed on another channel. If that upload does not finish, the files go out on another
  channel and the public row stays `running`. The worker does not POST a file-less result there.
  `GET /tasks` does not show the box-agent's delivery ack, and a timer cannot prove the real
  result was taken; that empty `done` can be the first event a restarted box-agent handles and
  would wake the sender with no files. A `running` row is not offered on `/results/next`. To
  reclaim the disk, remove that task's row and `files/<id>/` on the public relay by hand. Do not
  clear it with an empty result. The box agent, for a result over `RELAY_BIG_BYTES`,
  downloads from the configured public endpoint only when that endpoint already has every file
  complete with the same size and sha256; otherwise it uses the channel that offered the event.
  It does not try any other endpoint. The ack goes to the event channel, and a second copy
  wakes the sender once. `public_endpoint` in `worker.json` overrides the https heuristic (used
  by the loopback selftest, which has no TLS).
- **Prompts.** `relay-send … -` reads the prompt from stdin, `--prompt-file` from a file. The
  worker passes prompts longer than `stdin_prompt_over[tool]` bytes on stdin (`"-"` as the
  prompt argument) instead of argv.

## Protocol v2 and old servers

`GET /status` (authenticated) lists `features`: `status, chunked, range, staged, withdraw, mirror,
exclude, relpath`. An old server answers 404 and every client falls back to the old calls: one
streaming PUT per file (no resume), multipart attachments up to 16 MiB, flattened result paths
(`a/b.txt` becomes `a__b.txt`), and no withdraw (the ledger dedupes instead). Mixed versions work;
upgrade servers to get resume and sha256 on that channel.

## Failure cases

| Case | What happens |
|---|---|
| Primary down before enqueue | `relay-send` posts to the next channel; the worker is already polling it. |
| Primary up for the box but unreachable from the Mac | Handoff after `RELAY_HANDOFF_S`; primary copy withdrawn. |
| Primary dies while the task runs | Result goes to the backup after `mirror_after_s`; one wake; primary row closed later. |
| Primary drops mid-upload | Resume from the server's partial on retry; mirror only if no progress for `mirror_after_s`. |
| Worker restarts mid-task or mid-upload | Detached task keeps running; new worker resumes the upload. |
| Box agent restarts mid-download | `.partial` resumed with `Range`; wake only after verification. |
| Same task id on two channels | Runs once (ledger); second copy confirmed; one wake. |
| Both channels down | Results wait on the Mac with backoff; tasks wait in their queue. Nothing is dropped. |
| Hung server | Hard per-request deadline plus stall watchdog; the channel is marked down. |
