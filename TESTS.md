# Self-test results

Loopback run on the dev box, 2026-11-07 06:57 UTC. `python3 tests/selftest.py`

| Test | Result | Duration | Notes |
|---|---|---|---|
| short task | PASS | 1.4s | stub hermes ok: hello |
| health reports soft_body and max_body | PASS | 0.0s | soft_body=16777216 max_body=536870912 |
| multipart over soft limit -> 413 and connection closed | PASS | 0.0s | multipart over soft limit 16777216; PUT /tasks/{id}/files/{name} then POST JSON result |
| PUT then GET sha256, JSON result files[] | PASS | 0.1s | sha256 d540f823a635… 16781312 bytes; missing name dropped |
| worker uploads outputs via PUT | PASS | 1.6s | PUT 16781312 bytes sha256 matched |
| started wake before done wake | PASS | 1.0s | started then done |
| long task (~60s) | PASS | 61.0s | slept 60s, no timeout |
| multimodal in+out (png + mp4-ish + txt) | PASS | 1.0s | clip.mp4, image.png, notes.txt, stdout.txt |
| 5 concurrent tasks | PASS | 8.1s | all 5 done in 8.1s (serial would be 30s) |
| 2 sender slugs routed to own webhook | PASS | 1.1s | alpha->/wake/alpha, beta->/wake/beta |
| worker killed mid-task, restarted | PASS | 8.6s | detached task survived; result uploaded once |
| worker killed after finish before upload ack (re-upload) | PASS | 4.6s | no duplicate result/wake |
| server restarted mid-task | PASS | 9.6s | sqlite state kept; worker re-uploaded |
| box agent restarted | PASS | 7.6s | delivered after restart, no re-wake |
| failed task -> exactly one failed result | PASS | 4.4s | exit 3: stub error: simulated failure |
| duplicate task id is idempotent | PASS | 4.0s | second enqueue ignored |
| missing wake config -> one logged error, no retry | PASS | 4.0s | 2 error lines (started+done), 0 wakes |
| primary down -> fallback used (sticky) | PASS | 5.6s | switched after 2 primary failures, stayed on fallback |
| primary 503 -> fallback, one delivery, then sticky switch back | PASS | 56.6s | 503 x2 -> fallback; stayed while 503; healed -> primary; started+done each |
| box-agent: primary 503 -> fallback, one wake, then switch back | PASS | 57.2s | 2x503 -> fallback; started+done wakes; healed -> primary |
| relay-send: primary down -> enqueues via RELAY_FALLBACK_URL | PASS | 3.0s | enqueued on fallback, started+done |
| global: each task started then done/failed once | PASS | - | 50 wakes / 25 tasks |
