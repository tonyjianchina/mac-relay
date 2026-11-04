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
