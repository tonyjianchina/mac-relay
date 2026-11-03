# Self-test results

Loopback run on the dev box, 2026-11-03 21:44 CST. `python3 tests/selftest.py`

| Test | Result | Duration | Notes |
|---|---|---|---|
| short task | PASS | 1.4s | stub hermes ok: hello |
| long task (~60s) | PASS | 61.2s | slept 60s, no timeout |
| multimodal in+out (png + mp4-ish + txt) | PASS | 1.0s | clip.mp4, image.png, notes.txt, stdout.txt |
| 5 concurrent tasks | PASS | 8.2s | all 5 done in 8.2s (serial would be 30s) |
| 2 sender slugs routed to own webhook | PASS | 1.1s | alpha->/wake/alpha, beta->/wake/beta |
| worker killed mid-task, restarted | PASS | 8.6s | detached task survived; result uploaded once |
| worker killed after finish before upload ack (re-upload) | PASS | 4.6s | no duplicate result/wake |
| server restarted mid-task | PASS | 9.6s | sqlite state kept; worker re-uploaded |
| box agent restarted | PASS | 7.6s | delivered after restart, no re-wake |
| failed task -> exactly one failed result | PASS | 4.4s | exit 3: stub error: simulated failure |
| duplicate task id is idempotent | PASS | 4.0s | second enqueue ignored |
| missing wake config -> one logged error, no retry | PASS | 4.0s | 1 error line, 0 wakes |
| primary down -> fallback used (sticky) | PASS | 5.4s | switched after 2 primary failures, stayed on fallback |
| global: every completed task woke exactly once | PASS | - | 18 wakes / 18 tasks, 0 duplicates |
