#!/usr/bin/env python3
"""Loopback end-to-end self-test. Writes TESTS.md. Usage: python3 tests/selftest.py [--quick]"""
import json, os, signal, socket, subprocess, sys, tempfile, threading, time, hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUICK = "--quick" in sys.argv
LONG = 8 if QUICK else 60
T = tempfile.mkdtemp(prefix="mac-relay-test-")
def port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
PORT, DEAD, HOOK, FLAKY = port(), port(), port(), port()
URL = f"http://127.0.0.1:{PORT}"
SECRET = os.path.join(T, "secret"); open(SECRET, "w").write(os.urandom(24).hex())
WAKES = []  # (slug, body, auth)
class Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        WAKES.append((self.path.rsplit("/", 1)[-1], b, self.headers.get("Authorization")))
        self.send_response(204); self.end_headers()
    def log_message(self, *a): pass
threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", HOOK), Hook).serve_forever, daemon=True).start()
WAKE = os.path.join(T, "wake"); os.makedirs(WAKE)
for slug in ("alpha", "beta"):
    open(f"{WAKE}/{slug}.url", "w").write(f"http://127.0.0.1:{HOOK}/wake/{slug}")
    open(f"{WAKE}/{slug}.key", "w").write(f"key-{slug}")
RES = os.path.join(T, "results")
ENV = dict(os.environ, RELAY_URL=URL, RELAY_SECRET_FILE=SECRET, RELAY_DATA_DIR=os.path.join(T, "data"),
           RELAY_PORT=str(PORT), RELAY_CLAIM_TTL="5", RELAY_RESULTS_DIR=RES, RELAY_WAKE_DIR=WAKE)
stub = [sys.executable, os.path.join(ROOT, "tests/stub_tool.py")]
import urllib.request, urllib.error
BROKEN = {"on": True}  # FLAKY = primary that answers 503 (like Caddy with a dead upstream) until healed
class Flaky(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _go(self):
        n = int(self.headers.get("Content-Length") or 0); body = self.rfile.read(n) if n else None
        if BROKEN["on"]:
            self.send_response(503); self.send_header("Content-Length", "0"); self.end_headers(); return
        r = urllib.request.Request(URL + self.path, data=body, method=self.command,
                                   headers={k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection")})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(r, timeout=70) as x: st, d, ct = x.status, x.read(), x.headers.get("Content-Type")
        except urllib.error.HTTPError as e: st, d, ct = e.code, e.read(), e.headers.get("Content-Type")
        self.send_response(st); self.send_header("Content-Length", str(len(d)))
        if ct: self.send_header("Content-Type", ct)
        self.end_headers(); self.wfile.write(d)
    do_GET = do_POST = _go
    def log_message(self, *a): pass
threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", FLAKY), Flaky).serve_forever, daemon=True).start()
def wcfg(primary, name, **kw):
    p = os.path.join(T, name)
    json.dump({"primary_url": primary, "fallback_url": URL, "secret_file": SECRET, "state_dir": os.path.join(T, "mac-state"),
               "worker_name": "mac", "fail_threshold": 2, "cooldown_s": 3600, "max_backoff_s": 4,
               "tools": {t: stub + [t, "{prompt}"] for t in ("hermes", "deepseek", "codex")}, **kw}, open(p, "w"))
    return p
CFG_OK, CFG_FB = wcfg(URL, "worker.json"), wcfg(f"http://127.0.0.1:{DEAD}", "worker-fb.json")
CFG_503 = wcfg(f"http://127.0.0.1:{FLAKY}", "worker-503.json", cooldown_s=4, fallback_source_ip="127.0.0.1")
P = {}
def spawn(name, cmd):
    P[name] = subprocess.Popen(cmd, env=ENV, stdout=open(f"{T}/{name}.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)
def kill(name):
    P[name].send_signal(signal.SIGKILL); P[name].wait()
def server(): spawn("server", [sys.executable, f"{ROOT}/relay/server.py"]); time.sleep(0.6)
def worker(cfg=CFG_OK): spawn("worker", [sys.executable, f"{ROOT}/mac/relay-worker.py", cfg])
def agent(**env):
    spawn("agent", [sys.executable, f"{ROOT}/bin/relay-box-agent"])
    if env: P["agent"].kill(); P["agent"].wait(); P["agent"] = subprocess.Popen([sys.executable, f"{ROOT}/bin/relay-box-agent"],
        env=dict(ENV, **env), stdout=open(f"{T}/agent.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)
def send(slug, tool, prompt, *files, tid=None):
    cmd = [f"{ROOT}/bin/relay-send", slug, tool, prompt] + sum([["--file", f] for f in files], []) + (["--id", tid] if tid else [])
    t0 = time.time(); out = subprocess.run(cmd, env=ENV, capture_output=True, text=True, check=True).stdout.strip()
    assert time.time() - t0 < 5, "relay-send must return immediately"; return out
def wait_result(tid, timeout):
    f, end = os.path.join(RES, tid, "result.json"), time.time() + timeout
    while time.time() < end:
        if os.path.exists(f) and os.path.exists(os.path.join(RES, tid, ".wake")) and \
                open(os.path.join(RES, tid, ".wake")).read().strip() != "attempting":
            time.sleep(0.3); return json.load(open(f))
        time.sleep(0.2)
    raise AssertionError(f"no result for {tid} in {timeout}s")
def wakes_for(tid): return [w for w in WAKES if w[1]["task_id"] == tid]
def one_wake(tid, slug):
    w = wakes_for(tid); assert len(w) == 1, f"{tid}: {len(w)} wakes"
    assert w[0][0] == slug and w[0][2] == f"Bearer key-{slug}", "wrong wake route"
    assert set(w[0][1]) == {"task_id", "status", "summary", "sender", "result_path"}, w[0][1]
ROWS = []
def test(name):
    def deco(fn):
        t0 = time.time()
        try: note = fn() or ""; ok = "PASS"
        except Exception as e: ok, note = "FAIL", f"{type(e).__name__}: {e}"
        ROWS.append((name, ok, f"{time.time() - t0:.1f}s", note)); print(ok, name, f"{time.time()-t0:.1f}s", note, flush=True)
    return deco

server(); worker(); agent()

@test("short task")
def _():
    tid = send("alpha", "hermes", "hello"); r = wait_result(tid, 20); one_wake(tid, "alpha")
    assert r["status"] == "done" and "stub hermes ok" in r["summary"]; return r["summary"]

@test(f"long task (~{LONG}s)")
def _():
    tid = send("alpha", "codex", f"sleep={LONG}"); r = wait_result(tid, LONG + 40); one_wake(tid, "alpha")
    assert r["status"] == "done"; return f"slept {LONG}s, no timeout"

@test("multimodal in+out (png + mp4-ish + txt)")
def _():
    src = os.path.join(T, "input.png"); open(src, "wb").write(os.urandom(5000))
    tid = send("beta", "deepseek", "media please", src); r = wait_result(tid, 20); one_wake(tid, "beta")
    names = sorted(os.path.basename(f) for f in r["files"])
    assert {"image.png", "clip.mp4", "notes.txt", "stdout.txt"} <= set(names), names
    assert open(os.path.join(RES, tid, "image.png"), "rb").read(4) == b"\x89PNG"
    assert os.path.getsize(os.path.join(RES, tid, "clip.mp4")) > 200_000
    assert "input.png" in open(os.path.join(RES, tid, "notes.txt")).read(), "attachment not delivered to tool"
    return ", ".join(names)

@test("5 concurrent tasks")
def _():
    t0 = time.time(); ids = [send("alpha", "hermes", f"sleep=6 n={i}") for i in range(5)]
    for i in ids: wait_result(i, 40); one_wake(i, "alpha")
    el = time.time() - t0; assert el < 20, f"not concurrent ({el:.1f}s)"; return f"all 5 done in {el:.1f}s (serial would be 30s)"

@test("2 sender slugs routed to own webhook")
def _():
    a, b = send("alpha", "hermes", "for alpha"), send("beta", "codex", "for beta")
    wait_result(a, 20); wait_result(b, 20); one_wake(a, "alpha"); one_wake(b, "beta"); return "alpha->/wake/alpha, beta->/wake/beta"

@test("worker killed mid-task, restarted")
def _():
    tid = send("alpha", "hermes", "sleep=8 survive"); time.sleep(3); kill("worker"); time.sleep(2); worker()
    r = wait_result(tid, 40); one_wake(tid, "alpha"); assert r["status"] == "done"; return "detached task survived; result uploaded once"

@test("worker killed after finish before upload ack (re-upload)")
def _():
    kill("worker"); tid = send("beta", "hermes", "quick")
    # simulate: worker gone, task queued; restart worker, it runs; then kill/restart again right after -> no dup
    worker(); r = wait_result(tid, 40); kill("worker"); worker(); time.sleep(3); one_wake(tid, "beta"); return "no duplicate result/wake"

@test("server restarted mid-task")
def _():
    tid = send("alpha", "deepseek", "sleep=6 media"); time.sleep(2); kill("server"); time.sleep(6); server()
    r = wait_result(tid, 60); one_wake(tid, "alpha"); assert r["status"] == "done"; return "sqlite state kept; worker re-uploaded"

@test("box agent restarted")
def _():
    kill("agent"); tid = send("beta", "hermes", "while agent down"); time.sleep(4); agent()
    wait_result(tid, 30); kill("agent"); agent(); time.sleep(3); one_wake(tid, "beta"); return "delivered after restart, no re-wake"

@test("failed task -> exactly one failed result")
def _():
    tid = send("alpha", "codex", "please fail"); r = wait_result(tid, 20); time.sleep(3); one_wake(tid, "alpha")
    assert r["status"] == "failed" and "exit 3" in r["summary"], r["summary"]; return r["summary"]

@test("duplicate task id is idempotent")
def _():
    tid = send("alpha", "hermes", "dup", tid="dup-1"); send("alpha", "hermes", "dup", tid="dup-1")
    wait_result(tid, 20); time.sleep(3); one_wake(tid, "alpha"); return "second enqueue ignored"

@test("missing wake config -> one logged error, no retry")
def _():
    tid = send("gamma", "hermes", "nobody listens"); wait_result(tid, 20); time.sleep(3)
    assert not wakes_for(tid); n = open(f"{T}/agent.log").read().count(f"not announced"); assert n == 1, n
    return "1 error line, 0 wakes"

@test("primary down -> fallback used (sticky)")
def _():
    kill("worker"); os.remove(os.path.join(T, "mac-state/conn.json")) if os.path.exists(os.path.join(T, "mac-state/conn.json")) else None
    worker(CFG_FB); tid = send("beta", "hermes", "via fallback"); wait_result(tid, 40); one_wake(tid, "beta")
    st = json.load(open(os.path.join(T, "mac-state/conn.json"))); assert st["mode"] == "fallback", st
    tid2 = send("beta", "hermes", "still fallback"); wait_result(tid2, 20); one_wake(tid2, "beta")
    st2 = json.load(open(os.path.join(T, "mac-state/conn.json"))); assert st2 == st, "flip-flopped"
    return "switched after 2 primary failures, stayed on fallback"

@test("primary 503 -> fallback, one delivery, then sticky switch back")
def _():
    cf = os.path.join(T, "mac-state/conn.json")
    kill("worker"); os.path.exists(cf) and os.remove(cf); BROKEN["on"] = True
    worker(CFG_503); tid = send("alpha", "hermes", "primary says 503"); wait_result(tid, 40); time.sleep(2); one_wake(tid, "alpha")
    st = json.load(open(cf)); assert st["mode"] == "fallback", st
    time.sleep(5); st2 = json.load(open(cf)); assert st2 == st, "probed a still-503 primary and flipped"
    BROKEN["on"] = False; end = time.time() + 70
    while time.time() < end and json.load(open(cf))["mode"] != "primary": time.sleep(0.5)
    assert json.load(open(cf))["mode"] == "primary", "did not switch back after primary healed"
    tid2 = send("alpha", "hermes", "back on primary"); wait_result(tid2, 40); time.sleep(2); one_wake(tid2, "alpha")
    log = open(f"{T}/worker.log").read(); assert log.count("connection -> fallback") >= 1
    return "503 x2 -> fallback; stayed while 503; healed -> primary; 1 wake each"

@test("box-agent: primary 503 -> fallback, one wake, then switch back")
def _():
    alog = f"{T}/agent.log"; n0, p0 = open(alog).read().count("connection -> fallback"), open(alog).read().count("connection -> primary")
    kill("agent"); BROKEN["on"] = True
    agent(RELAY_URL=f"http://127.0.0.1:{FLAKY}", RELAY_FALLBACK_URL=URL, RELAY_FAIL_THRESHOLD="2", RELAY_COOLDOWN_S="4")
    end = time.time() + 30
    while time.time() < end and open(alog).read().count("connection -> fallback") == n0: time.sleep(0.3)
    assert open(alog).read().count("connection -> fallback") == n0 + 1, "agent did not switch to fallback"
    tid = send("alpha", "hermes", "agent on fallback"); wait_result(tid, 40); time.sleep(2); one_wake(tid, "alpha")
    time.sleep(5); assert open(alog).read().count("connection -> primary") == p0, "flipped back while primary still 503"
    BROKEN["on"] = False; end = time.time() + 70  # long-poll (50s) must return before the next probe
    while time.time() < end and open(alog).read().count("connection -> primary") == p0: time.sleep(0.5)
    assert open(alog).read().count("connection -> primary") == p0 + 1, "agent did not switch back"
    tid2 = send("beta", "hermes", "agent back on primary"); wait_result(tid2, 40); time.sleep(2); one_wake(tid2, "beta")
    assert open(alog).read().count("connection -> fallback") == n0 + 1, "flip-flopped"
    return "2x503 -> fallback; result delivered + 1 wake; healed -> primary; 1 wake"

@test("relay-send: primary down -> enqueues via RELAY_FALLBACK_URL")
def _():
    e = dict(ENV, RELAY_URL=f"http://127.0.0.1:{DEAD}", RELAY_FALLBACK_URL=URL)
    tid = subprocess.run([f"{ROOT}/bin/relay-send", "beta", "hermes", "send via fallback"], env=e, capture_output=True, text=True, check=True).stdout.strip()
    wait_result(tid, 40); time.sleep(2); one_wake(tid, "beta"); return "enqueued on fallback, 1 wake"

for n in list(P): kill(n)
total = len({w[1]["task_id"] for w in WAKES}); dup = len(WAKES) - total
ROWS.append(("global: every completed task woke exactly once", "PASS" if dup == 0 else "FAIL", "-", f"{len(WAKES)} wakes / {total} tasks, {dup} duplicates"))
with open(os.path.join(ROOT, "TESTS.md"), "w") as f:
    f.write(f"# Self-test results\n\nLoopback run on the dev box, {time.strftime('%Y-%m-%d %H:%M %Z')}. "
            f"`python3 tests/selftest.py{' --quick' if QUICK else ''}`\n\n| Test | Result | Duration | Notes |\n|---|---|---|---|\n")
    for r in ROWS: f.write("| " + " | ".join(str(x).replace("|", "/") for x in r) + " |\n")
print("logs in", T); sys.exit(any(r[1] == "FAIL" for r in ROWS))
