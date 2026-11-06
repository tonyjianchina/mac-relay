# Self-test results

Loopback run on the dev box, 2026-11-06 21:13 CST. `python3 tests/selftest.py --quick`

| Test | Result | Duration | Notes |
|---|---|---|---|
| short task | PASS | 1.6s | stub hermes ok: hello |
| started wake before done wake | PASS | 1.0s | started then done |
| long task (~8s) | PASS | 9.0s | slept 8s, no timeout |
| multimodal in+out (png + mp4-ish + txt) | PASS | 1.0s | clip.mp4, image.png, notes.txt, stdout.txt |
| 5 concurrent tasks | PASS | 8.1s | all 5 done in 8.1s (serial would be 30s) |
| 2 sender slugs routed to own webhook | PASS | 1.2s | alpha->/wake/alpha, beta->/wake/beta |
| worker killed mid-task, restarted | PASS | 8.6s | detached task survived; result uploaded once |
| worker killed after finish before upload ack (re-upload) | PASS | 4.6s | no duplicate result/wake |
| server restarted mid-task | PASS | 9.6s | sqlite state kept; worker re-uploaded |
| box agent restarted | PASS | 7.6s | delivered after restart, no re-wake |
| failed task -> exactly one failed result | PASS | 4.4s | exit 3: stub error: simulated failure |
| duplicate task id is idempotent | PASS | 3.9s | second enqueue ignored |
| missing wake config -> one logged error, no retry | PASS | 4.2s | 2 error lines (started+done), 0 wakes |
| primary down -> fallback used (sticky) | PASS | 5.6s | switched after 2 primary failures, stayed on fallback |
| primary 503 -> fallback, one delivery, then sticky switch back | PASS | 56.6s | 503 x2 -> fallback; stayed while 503; healed -> primary; started+done each |
| box-agent: primary 503 -> fallback, one wake, then switch back | PASS | 57.1s | 2x503 -> fallback; started+done wakes; healed -> primary |
| relay-send: primary down -> enqueues via RELAY_FALLBACK_URL | PASS | 3.0s | enqueued on fallback, started+done |
| global: each task started then done/failed once | PASS | - | 48 wakes / 24 tasks |
