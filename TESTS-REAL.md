# mac-relay — real end-to-end test run

Date: 2026-11-03, 22:11–22:28 (UTC+8). Deployed system: box agent → relay server (systemd `mac-relay`, behind Caddy at `<PRIMARY_URL>`) → Mac worker (launchd). Tailscale fallback `<FALLBACK_URL>`.
All prompts started with `[mac-relay test, no action needed]`. Sender `architect` (no wake file → `wake: missing`, expected) except the routing test.
Duration = task enqueue → `result.json` written on box.

| # | Test | Result | Duration | Notes |
|---|------|--------|----------|-------|
| 1a | hermes short | PASS | 16 s | `HERMES_OK` |
| 1b | deepseek short | **FAIL (Mac env)** | 10 s | Relay worked, the tool didn't: `claude-deepseek` exited 1 with "API key not found in macOS keychain, run setup-claude-deepseek". The failure came back once, correctly marked `failed`. |
| 1c | codex short | PASS | 23 s | `CODEX_OK` |
| 2 | codex long (`sleep 330`) | PASS | 368 s | No timeout. `summary.txt` came back. |
| 3 | multimodal (PNG in, PNG + .md out) | PASS | 94 s | Codex described the 64×32 red/blue input correctly. `out.png` and `report.md` came back to the box. |
| 4 | 4 concurrent codex tasks | PASS | 25/30/33/36 s; all done 36 s after the first was sent | They ran in parallel. Sending took about 1.5 s per task. |
| 5 | routing bot-a / bot-b | PASS | 13 s / 22 s | Each wake got `http 200` exactly once, and each went to its own URL (the two URL files differ). |
| 6a | Mac offline (worker booted out 75 s) | PASS | ~62 s (waited for worker) | Task was queued, picked up after bootstrap, delivered once. |
| 6b | box agent killed mid-task (75 s) | PASS | ~79 s | Result waited on the relay and was delivered once after restart (1 `delivered` line). |
| 6c | server `systemctl restart mac-relay` mid-task | PASS | ~71 s | Task finished and uploaded once. |
| 6d | worker `kill -9` mid-task | PASS | ~75 s | launchd KeepAlive restarted it (new pid). The detached task survived, and its output was uploaded once. |
| 6e | fallback (primary set to invalid host) | **FAIL (network)** | — | The worker switched to fallback after 3 failures as designed, but every fallback request failed (HTTPError via the system proxy; curl timed out). Original worker.json was restored byte-identical (same sha1) and `reset-primary` was set. The task queued during the test was delivered once on primary afterwards. |
| 7 | duplicates | PASS | — | Since 22:11: 17 tasks, 17 `delivered` lines, no duplicate ids in box-agent log. Mac log has no duplicate `started` or `uploaded`. |

## Fallback root cause (read-only investigation; nothing changed)
- The server is on Tailscale and its fallback listener is up (Caddy on `<SERVER_TS_IP>:18791`). The Mac sees the server as a peer, and `tailscale ping` gets a pong via DERP (no direct path).
- The Mac routing table has a **non-scoped `100.64/10` route via en0 → LAN gateway** (flags UGSc). It wins over Tailscale's interface-scoped `100.64/10` on utun (UCSI). So OS traffic to the server's tailnet IP goes to the LAN router and times out. Only the Mac's own tailnet IP and one box host have explicit utun host routes.
- The macOS system HTTP/HTTPS proxy is on (Shadowrocket). Python urllib in the worker uses it, so fallback requests got proxy HTTP errors instead of timeouts.
- Likely fix (not applied): exclude `100.64.0.0/10` and `*.ts.net` from Shadowrocket's proxy and TUN routing (or remove the en0 route it adds). Optionally set `NO_PROXY=100.64.0.0/10` in the worker plist, or have the worker bypass proxies for the fallback URL.

## Observations / minor issues (not fixed)
- Worker `req()` counts any HTTPError (including 502/503 from Caddy when the relay is down) as success, so a dead relay behind a live Caddy never triggers fallback. I left it alone on purpose: with fallback currently broken, failing over would make outages worse (sticky for 300 s).
- In the first 6e attempt, the test harness's `ssh` swallowed the heredoc stdin and ended the script early. That was a harness problem, not a system one; the re-run used `ssh -n` / `</dev/null`.

## Bot turns triggered
2 wakes (bot-a 1, bot-b 1), so about 2 bot turns. No other wakes were sent.

## Final state
worker.json is identical to the original. The worker is running under launchd, the box agent is running, server `mac-relay` is active, and the site returns 200.

---

# Multi-channel run (Tailscale primary, VPS backup)

Date: 2026-11-14, 15:57–17:15 (UTC+8). Box relay `http://<BOX_TS_IP>:18790` (box side `127.0.0.1:18790`) as primary; VPS relay `<VPS_URL>/relay` and `http://<VPS_TS_IP>:18791` as backups (the VPS server was still on the pre-v2 protocol: legacy mode). Mac worker polls all three. Box↔Mac traffic went through a Tailscale DERP relay (no direct path): about 130–370 KB/s Mac→box, 30–50 KB/s box→Mac, RTT 0.3–1.7 s, with short drops. Mac→VPS tailnet about 10 MB/s; VPS→box about 1.1 MB/s.
Tasks used the deterministic `selftest` tool via `prompt_routes` (no model calls) and a box-local test webhook. Each result carries a `manifest.json` with the sha256 of every inbox/outbox file as seen on the Mac; the box re-hashed every downloaded file.

| Scenario | Task | Result | Time | Evidence |
|---|---|---|---|---|
| Primary normal (1 MiB in, copy out, nested path) | rt-normal-155746 | PASS | ~60 s | sha256 equal in/out; `sub/dir/deep.txt` kept |
| 10 MiB result | rt-thru10-155904 | PASS | 29 s | sha ok, via primary |
| Primary down for the sender → backup | rt-primary-down-162933 | PASS | 209 s | enqueue via VPS; 5 MiB in + 10 MiB out; result via VPS tailnet; 1 wake |
| 300 KB prompt | rt-bigprompt-162933 | PASS | 25 s | prompt sha256 equal on the Mac |
| 4 tasks, 3 senders, one `failed` | rt-conc-{a,b,d,fail}-163318 | PASS | 26–258 s | a real DERP stall made the worker mirror 3 results to the VPS after 60 s; 1 wake each; copies acked; origin rows closed out 10 min later |
| 300 MiB result, worker restart and server restart mid-upload | rt-big300-163832 | PASS | 693 s | resumed at 40,158,592 after worker restart; box server restart → mirrored to VPS tailnet (300 MiB in 31 s), box agent fetched it in 274 s; sha256 `80946bde…a3d2fb` equal on both ends |
| Handoff (Mac cannot see the primary) | rt-handoff-165219 | PASS | 26 s | handed to backup after 22 s; primary copy withdrawn |
| Started wait shorter than acceptance | rt-started-timeout-165219 | PASS | 27 s | `relay-send` exit 2, task still ran, 1 wake |
| Box VM reboot during a 30 MiB attachment download (staged, chunked upload) | rt-attach30-165219 | PASS | 24 min (incl. ~9 min box down; DERP 30–50 KB/s) | after the stack restart the Mac resumed with `Range` (HTTP 206); sha256 `80560c03…9022d7` equal on the Mac; 1 wake |
| Worker upgrade in place (3 times) | mac-upgrade-* | PASS | ~80 s each | the running upgrade task survived the restart and was delivered by the new worker |

---

# Size routing: big attachments and results go to the public channel first

Date: 2026-11-14, 22:47–23:57 (UTC+8). Goal: the simplest fix for the slow box↔Mac DERP path (~35 KB/s on some hops) by sending large payloads over the VPS public HTTPS channel first (previously measured ~10–16 MB/s). Threshold `RELAY_BIG_BYTES` / `big_bytes`, default 8 MiB. Small tasks unchanged. VPS Caddy, Podcast Reader, and VPS `server.py` were not touched. Temp sender `relaytest` (local webhook); the 14 production wake configs were left in place and restored after the run.

Loopback `python3 tests/selftest.py` was run twice on the branch (after each of the two commits): all tests PASS, including three new ones for size routing.

Mac worker was upgraded in place (backup first, sha256 + py_compile + dry run, rollback on failure, no App install, `worker.json` unchanged). Second upgrade (`mac-bigroute-upgrade-2322`, sha `efd455bd…8096422`) is the code on this branch. It immediately resumed an in-flight 100 MiB download via the public endpoint (`attach … via vps`).

| Scenario | Task | Result | Time | Evidence |
|---|---|---|---|---|
| 100 MiB attachment box→Mac (`copyin`) | rt-bigpub-in-2305 | PASS | 32.9 min wall (see notes) | `relay-send` logged `big-attach … -> backup` and uploaded 104,857,600 bytes to the public channel in 35.6 s (~2.8 MB/s). First worker claim used the VPS tailnet URL of the same server (slow). After the second worker upgrade at 23:23:12 the download resumed on public (`via vps`). Mac `manifest.json` and box `copy-a100m.bin` sha256 `08717d18…f1e459` matched the source. 1 wake (http 200); public copy acked only. Box-agent fetched the 100 MiB result via the VPS tailnet channel (`via backup2`) in 762.5 s (~134 KB/s). |
| ≥50 MiB result Mac→box (`outmb=50`, no inbound file, claimed on primary) | rt-bigpub-out-2341 | PASS | 15.1 min wall | Accepted via primary. Worker mirrored to public and began the upload (stdout + `manifest.json` complete; `rand.bin` reached 41,943,040 bytes = 40 MiB on public, then stalled). Upload to the origin (box) finished: `rand.bin` 52,428,800 bytes, sha256 `77e39cb4…e9d841` equal in the Mac manifest and on the box. Box-agent delivered via primary in 0.1 s. 1 wake (http 200). |

## Notes vs the old ~35 KB/s path

- 100 MiB at 35 KB/s would be about 49 minutes for a single transfer. The box→VPS public upload of the same file took 35.6 s (~80×). After the worker preferred the public URL, the Mac finished the tool run by 23:25:42 (about 2.5 minutes after the resume). The long tail was the box-agent download of the result over the VPS tailnet URL (762.5 s), not the Mac attachment fetch.
- The 50 MiB result did go to public first, but that upload stalled at a 8 MiB chunk boundary (40 MiB). Existing origin fallback then delivered over the box primary. Correctness and dedup held; the public upload did not finish.
- Follow-ups (not in this PR): box-agent should prefer the public URL when downloading a large result (it currently uses whichever channel offered the result event). And Mac→VPS public uploads that stall at a chunk boundary should be diagnosed (HTTPS proxy on the Mac is a likely suspect; tailnet uploads of hundreds of MiB have been fast before).

