# Self-test results

Loopback run on the dev box, 2026-11-14 22:53 CST. `python3 tests/selftest.py`

| Test | Result | Duration | Notes |
|---|---|---|---|
| short task | PASS | 1.5s | stub hermes ok: hello len=5 sha=2cf24dba5fb0a30e |
| health reports soft_body and max_body | PASS | 0.0s | soft_body=16777216 max_body=536870912 |
| multipart over soft limit -> 413 and connection closed | PASS | 0.0s | multipart over soft limit 16777216; PUT /tasks/{id}/files/{name} then POST JSON result |
| PUT then GET sha256, JSON result files[] | PASS | 0.1s | sha256 d540f823a635… 16781312 bytes; missing name dropped |
| worker uploads outputs via PUT | PASS | 1.5s | PUT 16781312 bytes sha256 matched |
| no started webhook; acceptance before the tool finishes | PASS | 3.9s | poll saw running; one done wake |
| long task (~60s) | PASS | 61.2s | slept 60s; relay-send returned on running |
| multimodal in+out (png + mp4-ish + txt) | PASS | 0.9s | clip.mp4, image.png, notes.txt, stdout.txt |
| 5 concurrent tasks | PASS | 9.1s | all 5 done in 9.1s (serial would be 30s) |
| 2 sender slugs routed to own webhook | PASS | 1.3s | alpha->/wake/alpha, beta->/wake/beta |
| worker killed mid-task, restarted | PASS | 8.9s | detached task survived; result uploaded once |
| worker killed after finish before upload ack (re-upload) | PASS | 4.1s | no duplicate result/wake |
| server restarted mid-task | PASS | 9.9s | sqlite state kept; worker re-uploaded |
| box agent restarted | PASS | 7.9s | delivered after restart, no re-wake |
| failed task -> exactly one failed result | PASS | 3.7s | exit 3: stub error: simulated failure |
| duplicate task id is idempotent | PASS | 4.2s | second enqueue ignored |
| missing wake config -> one logged error, no retry | PASS | 3.9s | 1 error line (done only), 0 wakes |
| primary 503: relay-send, worker and box-agent use the backup; healed -> primary | PASS | 4.6s | 503 -> enqueue/claim/result via backup; healed -> next task via primary; 1 wake each |
| primary dies mid-task: result mirrored to the backup, one wake | PASS | 76.7s | primary 503 at finish -> mirror to backup; ran once, 1 wake; healed primary row closed out (ack only) |
| same task id on both channels runs once, one wake | PASS | 7.3s | claimed on both servers; ledger ran it once |
| handoff: primary queues but the worker cannot see it -> backup within RELAY_HANDOFF_S | PASS | 6.0s | accepted via backup after 2.5s; primary copy claimed; ran once |
| watchdog: a hung server fails a request in bounded time | PASS | 5.0s | deadline 3s -> 3.0s, stall 2s -> 2.0s; channel marked down |
| chunked PUT: resume after a cut chunk, offset 409, sha256 mismatch 422 | PASS | 1.2s | cut at 3MiB -> resumed to 20971643 bytes, sha256 ok; 409 on wrong offset; 422 on bad sha |
| stalled chunk sender releases the upload within RELAY_CHUNK_STALL_S; resume is not 409 busy | PASS | 3.5s | sender hung with the socket open; lock freed after 2s stall; resumed from 1 MiB, sha256 ok |
| Range download resumes; wrong bytes fail the sha256 check and never appear | PASS | 0.2s | 206 resume from 5MiB, sha256 ok; corrupted partial -> Mismatch, nothing delivered |
| large attachment: staged chunked upload box->Mac, sha256 end to end | PASS | 2.1s | 64 MiB in via staged PUT and back out; sha256 865dd4907d3a matched at Mac and box |
| nested outbox paths keep their relative path | PASS | 1.5s | out/sub/dir/deep.txt -> results/<id>/sub/dir/deep.txt |
| long prompt (300 KB) via --prompt-file and worker stdin | PASS | 6.9s | 307696 bytes; tool saw the same sha256 prefix 1fef0c1cdbf40643 |
| old (pre-v2) server: legacy multipart in, single PUT out, plain download | PASS | 5.3s | old server: multipart in, 16MiB+ PUT out, nested names flattened; >16MiB input refused (exit 1) |
| relay-send: primary down -> enqueues via RELAY_FALLBACK_URL | PASS | 2.9s | enqueued on fallback, one done wake |
| relay-send: enqueue failure exits 1 | PASS | 0.1s | exit 1, no task id |
| relay-send: accepted wait timeout exits 2 | PASS | 0.6s | exit 2, id on stdout, 已入队、尚未接单 |
| order_for_size: big attachments prefer the https fallback | PASS | 0.0s | https fallback first when big; no-https uses first fallback; small unchanged |
| big attachments enqueue on the public/fallback channel first | PASS | 1.5s | 200-byte file over RELAY_BIG_BYTES=100 enqueued and claimed on backup; 1 wake |
| big result uploads to the public channel first | PASS | 1.6s | small task claimed on primary; result mirrored to fallback immediately; 1 wake |
| stale started event is acked, not webhooked | PASS | 0.0s | acked, .wake-started=skipped/no-webhook |
| started confirm is queryable and does not block done delivery | PASS | 0.0s | started_wake=1; legacy 0 does not block done |
| unicode filename PUT/GET; traversal and empty 400; tid stays ASCII | PASS | 0.0s | PUT/GET 验收结果.md; ../evil and empty 400; tid ASCII |
| one task upload 4xx does not block others; quarantine keeps files | PASS | 3.1s | good uploaded while bad was at 1x 4xx; quarantine kept files |
| secret rotation: every line accepted, file reloaded live, clients send the first line | PASS | 10.0s | new+old both 200; old removed -> 401 within 2.5 s, no restart; bad edit ignored |
| worker embeds relay/net.py verbatim | PASS | 0.0s | net block identical |
| global: each task one final wake, no started webhook | PASS | - | 36 wakes / 36 tasks |
