#!/usr/bin/env python3
"""Loopback end-to-end self-test. Writes TESTS.md. Usage: python3 tests/selftest.py [--quick]"""
import json, os, signal, socket, sqlite3, ssl, subprocess, sys, tempfile, threading, time, hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUICK = "--quick" in sys.argv
LONG = 8 if QUICK else 60
T = tempfile.mkdtemp(prefix="mac-relay-test-")
def port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
PORT, DEAD, HOOK, FLAKY, PORT2, OLDP, HANG = port(), port(), port(), port(), port(), port(), port()
URL = f"http://127.0.0.1:{PORT}"
URL2 = f"http://127.0.0.1:{PORT2}"  # second relay server = the backup channel
OLDURL = f"http://127.0.0.1:{OLDP}"  # the pre-v2 server.py from git, for compatibility
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
           RELAY_PORT=str(PORT), RELAY_CLAIM_TTL="5", RELAY_RESULTS_DIR=RES, RELAY_WAKE_DIR=WAKE,
           RELAY_STUB_RUNS=os.path.join(T, "runs.log"))
ENV.pop("RELAY_FALLBACK_URL", None)
ENV["RELAY_CHUNK_STALL_S"] = "2"
stub = [sys.executable, os.path.join(ROOT, "tests/stub_tool.py")]
import urllib.request, urllib.error, urllib.parse
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
    # The worker uploads with PUT. A stand-in for Caddy has to 503 or forward that method too;
    # otherwise BaseHTTPRequestHandler answers 501 and the worker treats the channel as down.
    do_GET = do_POST = do_PUT = _go
    def log_message(self, *a): pass
threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", FLAKY), Flaky).serve_forever, daemon=True).start()
def wcfg(primary, name, **kw):
    state = kw.pop("state_dir_name", "mac-state")
    p = os.path.join(T, name)
    fb = kw.pop("fallback", URL2)
    cfg = {"primary_url": primary, "fallback_url": fb, "secret_file": SECRET, "state_dir": os.path.join(T, state),
           "worker_name": "mac", "max_backoff_s": 4, "mirror_after_s": 4, "closeout_after_s": 2, "stdin_prompt_over": {"hermes": 100000},
           "tools": {t: stub + [t, "{prompt}"] for t in ("hermes", "deepseek", "codex", "schwab")}, **kw}
    json.dump(cfg, open(p, "w"))
    return p
CFG_OK = wcfg(URL, "worker.json")
CFG_ISO = wcfg(URL, "worker-iso.json", state_dir_name="mac-state-iso", upload_4xx_quarantine=2, max_backoff_s=2, fallback=URL)
P = {}
def spawn(name, cmd, **env):
    P[name] = subprocess.Popen(cmd, env=dict(ENV, **env), stdout=open(f"{T}/{name}.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)
def kill(name):
    if name in P and P[name].poll() is None:
        P[name].send_signal(signal.SIGKILL)
    if name in P: P[name].wait()
def server(): spawn("server", [sys.executable, f"{ROOT}/relay/server.py"]); time.sleep(0.6)
def server2(): spawn("server2", [sys.executable, f"{ROOT}/relay/server.py"], RELAY_PORT=str(PORT2),
                     RELAY_DATA_DIR=os.path.join(T, "data2")); time.sleep(0.6)
def worker(cfg=CFG_OK): spawn("worker", [sys.executable, f"{ROOT}/mac/relay-worker.py", cfg])
AGENT_ENV = {"RELAY_FALLBACK_URL": URL2}
def agent(**env):
    spawn("agent", [sys.executable, f"{ROOT}/bin/relay-box-agent"], **dict(AGENT_ENV, **env))
def send(slug, tool, prompt, *files, tid=None):
    cmd = [f"{ROOT}/bin/relay-send", slug, tool, prompt] + sum([["--file", f] for f in files], []) + (["--id", tid] if tid else [])
    t0 = time.time()
    proc = subprocess.run(cmd, env=ENV, capture_output=True, text=True)
    send.last, send.err = time.time() - t0, proc.stderr
    assert proc.returncode == 0, f"exit {proc.returncode}\nstderr={proc.stderr}\nstdout={proc.stdout}"
    out = proc.stdout.strip()
    assert out and "\n" not in out, proc.stdout  # stdout is the task id only
    assert "accepted" in proc.stderr and any(
        s in proc.stderr for s in ("state=running", "state=done", "state=failed")), proc.stderr
    # Acceptance is in-process. A full tool run (sleep=LONG) must not be required.
    assert send.last < 40, f"relay-send blocked {send.last:.1f}s waiting for acceptance"
    return out
def wait_result(tid, timeout):
    f, end = os.path.join(RES, tid, "result.json"), time.time() + timeout
    while time.time() < end:
        if os.path.exists(f) and os.path.exists(os.path.join(RES, tid, ".wake")) and \
                open(os.path.join(RES, tid, ".wake")).read().strip() != "attempting":
            time.sleep(0.3); return json.load(open(f))
        time.sleep(0.2)
    raise AssertionError(f"no result for {tid} in {timeout}s")
def wakes_for(tid): return [w for w in WAKES if w[1]["task_id"] == tid]
def expect_wakes(tid, slug, final="done"):
    """Each confirmed task: exactly one done|failed wake, and no started webhook."""
    w = wakes_for(tid)
    assert len(w) == 1, f"{tid}: want 1 {final} wake, got {len(w)}: {[x[1].get('status') for x in w]}"
    body = w[0][1]
    assert body["status"] == final, body
    assert w[0][0] == slug and w[0][2] == f"Bearer key-{slug}", "wrong wake route"
    assert set(body) == {"task_id", "status", "summary", "sender", "result_path"}, body
    assert not os.path.exists(os.path.join(RES, tid, ".wake-started")), tid
def one_wake(tid, slug):  # backward name used nowhere after rewrite; keep alias
    expect_wakes(tid, slug)
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def bearer():
    return "Bearer " + open(SECRET).read().strip()
def api(method, path, body=None, ctype=None, timeout=120, headers=None):
    r = urllib.request.Request(URL + path, data=body, method=method)
    r.add_header("Authorization", bearer())
    if ctype: r.add_header("Content-Type", ctype)
    for k, v in (headers or {}).items(): r.add_header(k, v)
    try:
        with OPENER.open(r, timeout=timeout) as resp: return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e: return e.code, e.read(), e.headers
def fill(path, n):
    block = b"relay-big-block\n" * 256
    with open(path, "wb") as f:
        left = n
        while left:
            c = block if left >= len(block) else block[:left]
            f.write(c); left -= len(c)
def sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1024 * 1024), b""): h.update(c)
    return h.hexdigest()
def sha_get(path):
    r = urllib.request.Request(URL + path, method="GET"); r.add_header("Authorization", bearer())
    h, n = hashlib.sha256(), 0
    with OPENER.open(r, timeout=120) as resp:
        cl = int(resp.headers["Content-Length"])
        for c in iter(lambda: resp.read(1024 * 1024), b""):
            h.update(c); n += len(c)
    assert n == cl, (n, cl)
    return h.hexdigest(), n
ROWS = []
def test(name):
    def deco(fn):
        t0 = time.time()
        try: note = fn() or ""; ok = "PASS"
        except Exception as e: ok, note = "FAIL", f"{type(e).__name__}: {e}"
        ROWS.append((name, ok, f"{time.time() - t0:.1f}s", note)); print(ok, name, f"{time.time()-t0:.1f}s", note, flush=True)
    return deco

server(); server2(); worker(); agent()

@test("short task")
def _():
    tid = send("alpha", "hermes", "hello"); r = wait_result(tid, 20); expect_wakes(tid, "alpha")
    assert r["status"] == "done" and "stub hermes ok" in r["summary"]; return r["summary"]

@test("health reports soft_body and max_body")
def _():
    with OPENER.open(URL + "/health", timeout=5) as resp: h = json.loads(resp.read())
    assert h["ok"] is True and h["soft_body"] == 16 * 1024 * 1024 and h["max_body"] == 512 * 1024 * 1024, h
    return f"soft_body={h['soft_body']} max_body={h['max_body']}"

@test("multipart over soft limit -> 413 and connection closed")
def _():
    with OPENER.open(URL + "/health", timeout=5) as resp: soft = json.loads(resp.read())["soft_body"]
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    try:
        s.sendall((
            "POST /tasks HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            f"Authorization: {bearer()}\r\n"
            "Content-Type: multipart/form-data; boundary=deadbeef\r\n"
            f"Content-Length: {soft + 1}\r\n"
            "\r\n"
        ).encode())  # headers only: the server must reject before reading the body
        f = s.makefile("rb")
        status = f.readline().decode()
        hdrs = {}
        while True:
            line = f.readline()
            if line in (b"\r\n", b"\n", b""): break
            k, v = line.decode().split(":", 1); hdrs[k.lower()] = v.strip()
        body = f.read(int(hdrs["content-length"]))
        extra = f.read(1)
    finally:
        s.close()
    assert "413" in status, status
    assert hdrs.get("connection", "").lower() == "close", hdrs
    assert extra == b"", "connection stayed open after 413"
    err = json.loads(body)
    assert "soft limit" in err["error"] and "PUT" in err["error"], err
    return err["error"]

@test("PUT then GET sha256, JSON result files[]")
def _():
    kill("worker"); kill("agent")
    try:
        tid = "stream-put-1"
        st, raw, _ = api("POST", "/tasks", json.dumps({
            "id": tid, "sender": "alpha", "tool": "hermes", "prompt": "api stream"}).encode(), "application/json")
        assert st == 201, (st, raw)
        path = os.path.join(T, "stream.bin")
        n = 16 * 1024 * 1024 + 4096  # above the multipart soft cap; PUT must still accept it
        fill(path, n); want = sha_file(path)
        with open(path, "rb") as fh:
            st, raw, _ = api("PUT", f"/tasks/{tid}/files/blob.bin?kind=out", fh, "application/octet-stream",
                             headers={"Content-Length": str(n)})
        assert st == 200 and json.loads(raw)["bytes"] == n, (st, raw)
        got, gn = sha_get(f"/files/{tid}/out/blob.bin")
        assert got == want and gn == n, (got, want, gn)
        st, raw, _ = api("POST", f"/tasks/{tid}/result", json.dumps({
            "status": "done", "summary": "streamed put", "files": ["blob.bin", "missing.bin"]}).encode(),
                         "application/json")
        assert st == 200, (st, raw)
        task = json.loads(api("GET", f"/tasks/{tid}")[1])["task"]
        assert task["status"] == "done" and task["files"] == ["blob.bin"] and task["summary"] == "streamed put", task
        st, raw, _ = api("POST", f"/results/{tid}/ack", b"{}", "application/json")
        assert st == 200, (st, raw)
        return f"sha256 {got[:12]}… {n} bytes; missing name dropped"
    finally:
        worker(); agent()

@test("worker uploads outputs via PUT")
def _():
    tid = send("alpha", "hermes", "big please")
    r = wait_result(tid, 60); expect_wakes(tid, "alpha")
    local = os.path.join(T, "mac-state", "tasks", tid, "out", "big.bin")
    saved = os.path.join(RES, tid, "big.bin")
    assert os.path.getsize(local) > 16 * 1024 * 1024, os.path.getsize(local)
    want = sha_file(local)
    assert sha_file(saved) == want
    got, gn = sha_get(f"/files/{tid}/out/big.bin")
    assert got == want and gn == os.path.getsize(local)
    names = [os.path.basename(p) for p in r["files"]]
    assert "big.bin" in names and "stdout.txt" in names, names
    slog, wlog = open(f"{T}/server.log").read(), open(f"{T}/worker.log").read()
    needle = f"put-file {tid} out big.bin"
    assert needle in slog and needle in wlog, "worker result was not a streaming PUT"
    assert f"uploaded {tid} done" in wlog
    return f"PUT {gn} bytes sha256 matched"

@test("no started webhook; acceptance before the tool finishes")
def _():
    tid = send("alpha", "hermes", "sleep=3 order check")
    assert "state=running" in send.err, send.err
    assert send.last < 15, send.last
    r = wait_result(tid, 20); expect_wakes(tid, "alpha")
    assert r["status"] == "done" and not any(x[1]["status"] == "started" for x in wakes_for(tid))
    return "poll saw running; one done wake"

@test(f"long task (~{LONG}s)")
def _():
    t0 = time.time(); tid = send("alpha", "codex", f"sleep={LONG}")
    assert "state=running" in send.err and time.time() - t0 < 15, (send.err, time.time() - t0)
    r = wait_result(tid, LONG + 40); expect_wakes(tid, "alpha")
    assert r["status"] == "done"; return f"slept {LONG}s; relay-send returned on running"

@test("multimodal in+out (png + mp4-ish + txt)")
def _():
    src = os.path.join(T, "input.png"); open(src, "wb").write(os.urandom(5000))
    tid = send("beta", "deepseek", "media please", src); r = wait_result(tid, 20); expect_wakes(tid, "beta")
    names = sorted(os.path.basename(f) for f in r["files"])
    assert {"image.png", "clip.mp4", "notes.txt", "stdout.txt"} <= set(names), names
    assert open(os.path.join(RES, tid, "image.png"), "rb").read(4) == b"\x89PNG"
    assert os.path.getsize(os.path.join(RES, tid, "clip.mp4")) > 200_000
    assert "input.png" in open(os.path.join(RES, tid, "notes.txt")).read(), "attachment not delivered to tool"
    return ", ".join(names)

@test("5 concurrent tasks")
def _():
    t0 = time.time(); ids = [send("alpha", "hermes", f"sleep=6 n={i}") for i in range(5)]
    for i in ids: wait_result(i, 40); expect_wakes(i, "alpha")
    el = time.time() - t0; assert el < 20, f"not concurrent ({el:.1f}s)"; return f"all 5 done in {el:.1f}s (serial would be 30s)"

@test("2 sender slugs routed to own webhook")
def _():
    a, b = send("alpha", "hermes", "for alpha"), send("beta", "codex", "for beta")
    wait_result(a, 20); wait_result(b, 20); expect_wakes(a, "alpha"); expect_wakes(b, "beta"); return "alpha->/wake/alpha, beta->/wake/beta"

@test("worker killed mid-task, restarted")
def _():
    tid = send("alpha", "hermes", "sleep=8 survive"); time.sleep(3); kill("worker"); time.sleep(2); worker()
    r = wait_result(tid, 40); expect_wakes(tid, "alpha"); assert r["status"] == "done"; return "detached task survived; result uploaded once"

@test("worker killed after finish before upload ack (re-upload)")
def _():
    tid = send("beta", "hermes", "quick")
    r = wait_result(tid, 40); kill("worker"); worker(); time.sleep(3); expect_wakes(tid, "beta"); return "no duplicate result/wake"

@test("server restarted mid-task")
def _():
    tid = send("alpha", "deepseek", "sleep=6 media"); time.sleep(2); kill("server"); time.sleep(6); server()
    r = wait_result(tid, 60); expect_wakes(tid, "alpha"); assert r["status"] == "done"; return "sqlite state kept; worker re-uploaded"

@test("box agent restarted")
def _():
    kill("agent"); tid = send("beta", "hermes", "while agent down"); time.sleep(4); agent()
    wait_result(tid, 30); kill("agent"); agent(); time.sleep(3); expect_wakes(tid, "beta"); return "delivered after restart, no re-wake"

@test("failed task -> exactly one failed result")
def _():
    tid = send("alpha", "codex", "please fail"); r = wait_result(tid, 20); time.sleep(3); expect_wakes(tid, "alpha", final="failed")
    assert r["status"] == "failed" and "exit 3" in r["summary"], r["summary"]; return r["summary"]

@test("duplicate task id is idempotent")
def _():
    tid = send("alpha", "hermes", "dup", tid="dup-1"); send("alpha", "hermes", "dup", tid="dup-1")
    wait_result(tid, 20); time.sleep(3); expect_wakes(tid, "alpha"); return "second enqueue ignored"

@test("missing wake config -> one logged error, no retry")
def _():
    tid = send("gamma", "hermes", "nobody listens"); wait_result(tid, 20); time.sleep(3)
    assert not wakes_for(tid)
    assert not os.path.exists(os.path.join(RES, tid, ".wake-started"))
    n = open(f"{T}/agent.log").read().count("not announced"); assert n == 1, n
    assert "missing" in open(os.path.join(RES, tid, ".wake")).read()
    return "1 error line (done only), 0 wakes"

sys.path.insert(0, ROOT)
from relay import net as NET
FLAKY_URL = f"http://127.0.0.1:{FLAKY}"
CFG_CH = wcfg(FLAKY_URL, "worker-ch.json", fallback=URL2)  # same ledger (state dir) as CFG_OK
def api_at(base, method, path, body=None, ctype=None, timeout=120, headers=None):
    r = urllib.request.Request(base + path, data=body, method=method)
    r.add_header("Authorization", bearer())
    if ctype: r.add_header("Content-Type", ctype)
    for k, v in (headers or {}).items(): r.add_header(k, v)
    try:
        with OPENER.open(r, timeout=timeout) as resp: return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e: return e.code, e.read(), e.headers
def run_send(env, *args, stdin=None):
    return subprocess.run([f"{ROOT}/bin/relay-send", *args], env=dict(ENV, **env), capture_output=True, text=True, input=stdin)
def runs(tid):
    try: return open(os.path.join(T, "runs.log")).read().split().count(tid)
    except OSError: return 0
def wait_log(name, needle, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if needle in open(f"{T}/{name}.log").read(): return True
        time.sleep(0.3)
    raise AssertionError(f"{needle!r} not in {name}.log after {timeout}s")
CH_ENV = {"RELAY_URL": FLAKY_URL, "RELAY_FALLBACK_URL": URL2}

@test("primary 503: relay-send, worker and box-agent use the backup; healed -> primary")
def _():
    kill("worker"); kill("agent"); BROKEN["on"] = True
    try:
        worker(CFG_CH); agent(**CH_ENV)
        p = run_send(CH_ENV, "alpha", "hermes", "primary broken")
        assert p.returncode == 0 and "via backup" in p.stderr, (p.returncode, p.stderr)
        tid = p.stdout.strip(); r = wait_result(tid, 40); expect_wakes(tid, "alpha")
        wait_log("agent", f"delivered {tid} done via backup", 10)
        BROKEN["on"] = False
        wait_log("worker", "channel primary recovered", 30)
        p2 = run_send(CH_ENV, "beta", "hermes", "primary healed")
        assert p2.returncode == 0 and "via primary" in p2.stderr, (p2.returncode, p2.stderr)
        tid2 = p2.stdout.strip(); wait_result(tid2, 40); expect_wakes(tid2, "beta")
        wait_log("agent", f"delivered {tid2} done via primary", 10)
        assert f"claimed {tid2} hermes via primary" in open(f"{T}/worker.log").read()
        return "503 -> enqueue/claim/result via backup; healed -> next task via primary; 1 wake each"
    finally:
        BROKEN["on"] = False

@test("primary dies mid-task: result mirrored to the backup, one wake")
def _():
    BROKEN["on"] = False
    p = run_send(CH_ENV, "alpha", "hermes", "sleep=6 mid-task switch")
    assert p.returncode == 0 and "via primary" in p.stderr, p.stderr
    tid = p.stdout.strip(); time.sleep(1); BROKEN["on"] = True
    try:
        r = wait_result(tid, 60); expect_wakes(tid, "alpha")
        wlog = open(f"{T}/worker.log").read()
        assert f"mirror {tid} -> fallback" in wlog, "no mirror"
        wait_log("agent", f"delivered {tid} done via backup", 10)
        assert runs(tid) == 1, runs(tid)
        BROKEN["on"] = False  # primary heals: the worker closes the stale running row there
        wait_log("worker", f"closeout {tid} on primary", 90)
        wait_log("agent", f"duplicate {tid} via primary - already delivered, ack only", 30)
        expect_wakes(tid, "alpha")
        return "primary 503 at finish -> mirror to backup; ran once, 1 wake; healed primary row closed out (ack only)"
    finally:
        BROKEN["on"] = False

@test("same task id on both channels runs once, one wake")
def _():
    tid = "dual-1"
    body = json.dumps({"id": tid, "sender": "beta", "tool": "hermes", "prompt": "sleep=2 dual"}).encode()
    assert api_at(URL, "POST", "/tasks", body, "application/json")[0] == 201
    assert api_at(URL2, "POST", "/tasks", body, "application/json")[0] == 201
    wait_result(tid, 40); time.sleep(4); expect_wakes(tid, "beta")
    assert runs(tid) == 1, f"ran {runs(tid)} times"
    assert f"duplicate claim {tid}" in open(f"{T}/worker.log").read()
    return "claimed on both servers; ledger ran it once"

@test("handoff: primary queues but the worker cannot see it -> backup within RELAY_HANDOFF_S")
def _():
    BROKEN["on"] = True  # the worker reaches the primary only through FLAKY
    try:
        e = {"RELAY_URL": URL, "RELAY_FALLBACK_URL": URL2, "RELAY_HANDOFF_S": "2"}
        t0 = time.time(); p = run_send(e, "beta", "hermes", "handoff please")
        assert p.returncode == 0 and "handoff" in p.stderr and "via backup" in p.stderr, p.stderr
        tid = p.stdout.strip(); took = time.time() - t0
        # Queued -> withdrawn. If a long-poll already in flight through the proxy took the claim
        # (its confirm then 503s), the copy is refused withdrawal and the ledger dedupes instead.
        pst = json.loads(api_at(URL, "GET", f"/tasks/{tid}")[1])["task"]["state"]
        assert ("-> withdrawn" in p.stderr and pst == "withdrawn") or pst in ("claimed", "running", "done", "queued"), (pst, p.stderr)
        wait_result(tid, 40); time.sleep(2); expect_wakes(tid, "beta"); assert runs(tid) == 1
        return f"accepted via backup after {took:.1f}s; primary copy {pst}; ran once"
    finally:
        BROKEN["on"] = False

@test("watchdog: a hung server fails a request in bounded time")
def _():
    hs = socket.socket(); hs.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); hs.bind(("127.0.0.1", HANG)); hs.listen(4)
    held = []
    def hang():
        while True:
            c, _ = hs.accept(); held.append(c)  # read nothing, answer nothing
    threading.Thread(target=hang, daemon=True).start()
    ep = NET.Endpoint(f"http://127.0.0.1:{HANG}", open(SECRET).read().strip(), "hang")
    out = []
    for kw in ({"deadline": 3}, {"deadline": 300, "stall": 2}):
        t0 = time.time()
        try:
            ep.call("GET", "/tasks/next?wait=50", **kw); raise AssertionError("no timeout")
        except TimeoutError:
            out.append(time.time() - t0)
    assert out[0] < 6 and out[1] < 5, out
    assert not ep.healthy()
    return f"deadline 3s -> {out[0]:.1f}s, stall 2s -> {out[1]:.1f}s; channel marked down"

@test("chunked PUT: resume after a cut chunk, offset 409, sha256 mismatch 422")
def _():
    tid = "chunk-1"  # mirror row: never handed to the worker that is polling this server
    assert api("POST", "/tasks", json.dumps({"id": tid, "sender": "alpha", "tool": "hermes", "prompt": "chunk",
                                             "mirror": True}).encode(), "application/json")[0] == 201
    path = os.path.join(T, "chunk.bin"); n = 20 * 1024 * 1024 + 123
    with open(path, "wb") as f: f.write(os.urandom(n))
    want = sha_file(path)
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    s.sendall((f"PUT /tasks/{tid}/files/chunk.bin?kind=out&offset=0&total={n} HTTP/1.1\r\nHost: x\r\n"
               f"Authorization: {bearer()}\r\nContent-Type: application/octet-stream\r\n"
               f"Content-Length: {8 * 1024 * 1024}\r\n\r\n").encode())
    s.sendall(open(path, "rb").read(3 * 1024 * 1024)); time.sleep(0.5); s.close(); time.sleep(0.5)
    st = json.loads(api("GET", f"/uploads/{tid}/chunk.bin?kind=out")[1])
    assert st["partial"] == 3 * 1024 * 1024 and not st["complete"], st
    st, raw, _ = api("PUT", f"/tasks/{tid}/files/chunk.bin?kind=out&offset=0&total={n}", b"x" * 10, "application/octet-stream")
    assert st == 409 and json.loads(raw)["partial"] == 3 * 1024 * 1024, (st, raw)
    st, raw, _ = api("GET", f"/files/{tid}/out/chunk.bin")
    assert st == 404, "partial upload visible as a file"
    logs = []
    ep = NET.Endpoint(URL, open(SECRET).read().strip(), "a")
    m = NET.upload_file(ep, tid, "out", path, "chunk.bin", log=lambda *a: logs.append(a))
    assert m["sha256"] == want and any(a[0] == "resume" for a in logs), logs
    got, gn = sha_get(f"/files/{tid}/out/chunk.bin"); assert got == want and gn == n
    st, raw, _ = api("PUT", f"/tasks/{tid}/files/bad.bin?kind=out&offset=0&total=4&sha256={'0' * 64}", b"abcd",
                     "application/octet-stream")
    assert st == 422, (st, raw)
    st = json.loads(api("GET", f"/uploads/{tid}/bad.bin?kind=out")[1]); assert st["partial"] == 0 and not st["complete"], st
    assert api("GET", f"/files/{tid}/out/bad.bin")[0] == 404
    api("POST", f"/tasks/{tid}/result", json.dumps({"status": "done", "summary": "c", "files": ["chunk.bin"]}).encode(), "application/json")
    meta = json.loads(api("GET", f"/tasks/{tid}")[1])["task"]["files_meta"]
    assert meta == [{"name": "chunk.bin", "size": n, "sha256": want}], meta
    api("POST", f"/results/{tid}/ack", b"{}", "application/json")
    return f"cut at 3MiB -> resumed to {n} bytes, sha256 ok; 409 on wrong offset; 422 on bad sha"

@test("stalled chunk sender releases the upload within RELAY_CHUNK_STALL_S; resume is not 409 busy")
def _():
    tid = "chunk-2"
    assert api("POST", "/tasks", json.dumps({"id": tid, "sender": "alpha", "tool": "hermes", "prompt": "stall",
                                             "mirror": True}).encode(), "application/json")[0] == 201
    path = os.path.join(T, "stall.bin"); n = 4 * 1024 * 1024
    with open(path, "wb") as f: f.write(os.urandom(n))
    s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
    s.sendall((f"PUT /tasks/{tid}/files/stall.bin?kind=out&offset=0&total={n} HTTP/1.1\r\nHost: x\r\n"
               f"Authorization: {bearer()}\r\nContent-Type: application/octet-stream\r\n"
               f"Content-Length: {n}\r\n\r\n").encode())
    s.sendall(open(path, "rb").read(1024 * 1024))  # then hang with the socket open
    try:
        time.sleep(3.5)
        st = json.loads(api("GET", f"/uploads/{tid}/stall.bin?kind=out")[1])
        assert st["partial"] == 1024 * 1024, st
        logs = []
        ep = NET.Endpoint(URL, open(SECRET).read().strip(), "a")
        m = NET.upload_file(ep, tid, "out", path, "stall.bin", log=lambda *a: logs.append(a))
        assert m["sha256"] == sha_file(path), m
    finally:
        s.close()
    api("POST", f"/results/{tid}/ack", b"{}", "application/json")
    return "sender hung with the socket open; lock freed after 2s stall; resumed from 1 MiB, sha256 ok"

def _stall_after(nbytes):
    """Sleep once, on the next read after nbytes have already been handed to the socket."""
    orig, st = NET._Body.read, {"n": 0, "did": False}
    def slow(self, k=65536):
        if not st["did"] and st["n"] >= nbytes:
            st["did"] = True
            time.sleep(3.5)  # server RELAY_CHUNK_STALL_S is 2
        b = orig(self, k)
        st["n"] += len(b)
        return b
    NET._Body.read = slow
    return orig

@test("400 short body mid-chunk resumes from the kept partial (stall_s=2)")
def _():
    """One client chunk, pause after the server has committed >=1 MiB. Then five small
    chunks completed and the next one paused: resume stays at or past that boundary."""
    ep = NET.Endpoint(URL, open(SECRET).read().strip(), "short")
    def once(tid, name, n, chunk, pause_after, floor):
        assert api("POST", "/tasks", json.dumps({"id": tid, "sender": "alpha", "tool": "hermes",
                   "prompt": "short", "mirror": True}).encode(), "application/json")[0] == 201
        path = os.path.join(T, name); fill(path, n); want = sha_file(path)
        logs, orig = [], _stall_after(pause_after)
        try:
            m = NET.upload_file(ep, tid, "out", path, name, chunk=chunk, log=lambda *a: logs.append(a))
        finally:
            NET._Body.read = orig
        assert m["sha256"] == want, m
        offs = []
        for a in logs:
            if a and a[0] == "resume":
                offs.append(int(str(a[3]).split("/")[0]))
        if floor is not None:
            assert offs and min(offs) >= floor and max(offs) < n, (offs, floor, n)
        got, gn = sha_get(f"/files/{tid}/out/{name}")
        assert got == want and gn == n, (gn, n)
        api("POST", f"/results/{tid}/ack", b"{}", "application/json")
        return offs
    mid = once("short-mid-1", "mid.bin", 3 * 1024 * 1024, NET.CHUNK_BYTES, 1024 * 1024 + 65536, 1024 * 1024)
    chunk = 256 * 1024
    once("short-bound-1", "bound.bin", chunk * 6, chunk, chunk * 5 + 64 * 1024, None)
    lines = open(f"{T}/server.log").read().splitlines()
    assert any("short-mid-1" in ln and " 400 " in ln for ln in lines), "mid-chunk stall was not a 400"
    at = chunk * 5
    assert any(f"short-bound-1/files/bound.bin" in ln and f"offset={at}" in ln and " 400 " in ln for ln in lines)
    assert any(f"short-bound-1/files/bound.bin" in ln and f"offset={at}" in ln and " 200 " in ln for ln in lines)
    return f"mid-chunk kept bytes, resumed from {mid[0]}; sixth chunk 400 at {at} then retried there"

def _tls_bridge(http_port, tls_port, cert, key):
    """Terminate a disposable localhost cert and splice the plaintext onto the relay."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    ls = socket.socket(); ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind(("127.0.0.1", tls_port)); ls.listen(32)
    def pump(src, dst):
        try:
            while True:
                b = src.recv(65536)
                if not b:
                    break
                dst.sendall(b)
        except OSError:
            pass
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass
    def one(raw):
        try:
            tls = ctx.wrap_socket(raw, server_side=True)
            up = socket.create_connection(("127.0.0.1", http_port))
        except OSError:
            try:
                raw.close()
            except OSError:
                pass
            return
        a = threading.Thread(target=pump, args=(tls, up), daemon=True)
        b = threading.Thread(target=pump, args=(up, tls), daemon=True)
        a.start(); b.start(); a.join(); b.join()
        for s in (tls, up):
            try:
                s.close()
            except OSError:
                pass
    def loop():
        while True:
            c, _ = ls.accept()
            threading.Thread(target=one, args=(c,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()

@test("TLS interrupted chunk resumes from the kept partial")
def _():
    cert, key = os.path.join(T, "localhost-test.crt"), os.path.join(T, "localhost-test.key")
    made = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "1", "-nodes",
                           "-keyout", key, "-out", cert, "-subj", "/CN=localhost",
                           "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost"], capture_output=True)
    assert made.returncode == 0, made.stderr.decode()[:300]
    tp = port(); _tls_bridge(PORT, tp, cert, key)
    tid, name, n = "tls-short-1", "tls.bin", 3 * 1024 * 1024
    assert api("POST", "/tasks", json.dumps({"id": tid, "sender": "alpha", "tool": "hermes", "prompt": "tls",
                                             "mirror": True}).encode(), "application/json")[0] == 201
    path = os.path.join(T, name); fill(path, n); want = sha_file(path)
    trust = ssl.create_default_context(); trust.load_verify_locations(cert)
    old_ctx, logs, orig = ssl._create_default_https_context, [], _stall_after(1024 * 1024 + 65536)
    ssl._create_default_https_context = lambda: trust
    try:
        ep = NET.Endpoint(f"https://127.0.0.1:{tp}", open(SECRET).read().strip(), "tls")
        m = NET.upload_file(ep, tid, "out", path, name, log=lambda *a: logs.append(a))
    finally:
        ssl._create_default_https_context = old_ctx
        NET._Body.read = orig
    assert m["sha256"] == want
    offs = [int(str(a[3]).split("/")[0]) for a in logs if a and a[0] == "resume"]
    assert offs and min(offs) >= 1024 * 1024 and max(offs) < n, offs
    got, gn = sha_get(f"/files/{tid}/out/{name}")
    assert got == want and gn == n
    lines = open(f"{T}/server.log").read().splitlines()
    assert any("tls-short-1" in ln and " 400 " in ln for ln in lines)
    api("POST", f"/results/{tid}/ack", b"{}", "application/json")
    return f"localhost TLS stall resumed from {offs[0]}"

def _localhost_cert():
    cert, key = os.path.join(T, "chunk-tls.crt"), os.path.join(T, "chunk-tls.key")
    if not os.path.exists(cert):
        subprocess.check_call(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "1", "-nodes",
                               "-keyout", key, "-out", cert, "-subj", "/CN=localhost",
                               "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cert, key

def _stream_proxy(upstream_port, tls_ctx=None):
    """One accept loop. Optionally TLS. Streams each request to upstream and the response back.
    Upstream connections are reused (keep-alive), the way a reverse proxy does."""
    ls = socket.socket(); ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind(("127.0.0.1", 0)); ls.listen(32)
    port = ls.getsockname()[1]
    up = {"s": None}
    def headers(f):
        line = f.readline()
        if not line:
            return None, {}
        hdrs = {}
        while True:
            h = f.readline()
            if h in (b"\r\n", b"\n", b""):
                break
            k, v = h.decode("iso-8859-1").split(":", 1)
            hdrs[k.lower()] = v.strip()
        return line, hdrs
    def upstream():
        if up["s"] is None:
            up["s"] = socket.create_connection(("127.0.0.1", upstream_port))
        return up["s"]
    def handle(raw):
        c = tls_ctx.wrap_socket(raw, server_side=True) if tls_ctx else raw
        f = c.makefile("rb")
        try:
            while True:
                line, hdrs = headers(f)
                if not line:
                    break
                n = int(hdrs.get("content-length", "0") or 0)
                u = upstream()
                u.sendall(line)
                for k, v in hdrs.items():
                    if k != "connection":
                        u.sendall(f"{k}: {v}\r\n".encode())
                u.sendall(b"Connection: keep-alive\r\n\r\n")
                left = n
                while left:
                    b = f.read(min(65536, left))
                    if not b:
                        break
                    u.sendall(b); left -= len(b)
                uf = u.makefile("rb")
                sl, sh = headers(uf)
                if not sl:
                    break
                un = int(sh.get("content-length", "0") or 0)
                body = uf.read(un) if un else b""
                c.sendall(sl)
                for k, v in sh.items():
                    if k != "connection":
                        c.sendall(f"{k}: {v}\r\n".encode())
                c.sendall(b"Connection: close\r\n\r\n" + body)
                if (hdrs.get("connection") or "").lower() == "close":
                    break
        except OSError:
            up["s"] = None
        finally:
            try:
                c.close()
            except OSError:
                pass
    def loop():
        while True:
            c, _ = ls.accept()
            threading.Thread(target=handle, args=(c,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    return port

@test("two full 8MiB chunks complete over TLS and a keep-alive proxy")
def _():
    cert, key = _localhost_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(cert, key)
    hp = _stream_proxy(PORT)
    tp = _stream_proxy(PORT, ctx)
    trust = ssl.create_default_context(); trust.load_verify_locations(cert)
    old = ssl._create_default_https_context
    ssl._create_default_https_context = lambda: trust
    try:
        def run(label, url, nbytes):
            tid = "chunks-" + label
            assert api("POST", "/tasks", json.dumps({"id": tid, "sender": "alpha", "tool": "hermes",
                       "prompt": "chunks", "mirror": True}).encode(), "application/json")[0] == 201
            path = os.path.join(T, tid + ".bin"); fill(path, nbytes)
            ep = NET.Endpoint(url, open(SECRET).read().strip(), label)
            m = NET.upload_file(ep, tid, "out", path, "f.bin", stall=30)
            assert m["sha256"] == sha_file(path) and m["size"] == nbytes
            api("POST", f"/results/{tid}/ack", b"{}", "application/json")
        run("p16", f"http://127.0.0.1:{hp}", 16 * 1024 * 1024)   # second chunk is a full 8MiB and the tail
        run("p17", f"http://127.0.0.1:{hp}", 17 * 1024 * 1024)   # second chunk is a full 8MiB and not the tail
        run("t17", f"https://127.0.0.1:{tp}", 17 * 1024 * 1024)
        lines = open(f"{T}/server.log").read().splitlines()
        assert any("chunks-p17" in ln and "offset=8388608" in ln and " 200 " in ln for ln in lines)
        assert any("chunks-t17" in ln and "offset=8388608" in ln and " 200 " in ln for ln in lines)
        return "16MiB and 17MiB via keep-alive proxy; 17MiB via TLS; second full chunk stored"
    finally:
        ssl._create_default_https_context = old

@test("full chunk answered early with 409 is resumed; persistent busy stops")
def _():
    cert, key = _localhost_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(cert, key)
    state = {"puts": 0}
    def reply(c, code, obj, close=True):
        raw = json.dumps(obj).encode()
        c.sendall(f"HTTP/1.1 {code} X\r\nContent-Type: application/json\r\nContent-Length: {len(raw)}\r\n"
                  f"Connection: {'close' if close else 'keep-alive'}\r\n\r\n".encode() + raw)
        if close:
            c.close()
    def one(raw):
        try:
            c = ctx.wrap_socket(raw, server_side=True)
        except OSError:
            raw.close(); return
        f = c.makefile("rb")
        try:
            line = f.readline()
            if not line:
                return
            hdrs = {}
            while True:
                h = f.readline()
                if h in (b"\r\n", b"\n", b""):
                    break
                k, v = h.decode().split(":", 1); hdrs[k.lower()] = v.strip()
            cl = int(hdrs.get("content-length") or 0)
            path = line.decode().split(" ", 2)[1]
            if path.startswith("/status"):
                return reply(c, 200, {"features": ["chunked", "status"]})
            if path.startswith("/uploads/"):
                done = state["puts"] >= 3
                return reply(c, 200, {"partial": 0 if state["puts"] == 0 else 8 * 1024 * 1024,
                                      "complete": done, "size": 16 * 1024 * 1024 if done else None,
                                      "sha256": None})
            if "offset=0" in path:
                got = 0
                while got < cl:
                    b = f.read(min(65536, cl - got))
                    if not b:
                        break
                    got += len(b)
                state["puts"] += 1
                return reply(c, 200, {"partial": got, "complete": False})
            state["puts"] += 1
            if state["puts"] == 2:
                # Answer and stop reading. An 8MiB blocking send must not wait out the stall.
                reply(c, 409, {"busy": True, "partial": 8 * 1024 * 1024}, close=False)
                time.sleep(30)
                return
            got = 0
            while got < cl:
                b = f.read(min(65536, cl - got))
                if not b:
                    break
                got += len(b)
            reply(c, 200, {"partial": 16 * 1024 * 1024, "complete": True})
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass
    ls = socket.socket(); ls.bind(("127.0.0.1", 0)); ls.listen(16); sp = ls.getsockname()[1]
    def loop():
        while True:
            c, _ = ls.accept()
            threading.Thread(target=one, args=(c,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    # always-busy listener
    busy_n = {"n": 0}
    def busy_one(raw):
        try:
            c = ctx.wrap_socket(raw, server_side=True)
        except OSError:
            raw.close(); return
        f = c.makefile("rb")
        try:
            line = f.readline()
            if not line:
                return
            hdrs = {}
            while True:
                h = f.readline()
                if h in (b"\r\n", b"\n", b""):
                    break
                k, v = h.decode().split(":", 1); hdrs[k.lower()] = v.strip()
            path = line.decode().split(" ", 2)[1]
            cl = int(hdrs.get("content-length") or 0)
            while cl:
                b = f.read(min(65536, cl))
                if not b:
                    break
                cl -= len(b)
            if path.startswith("/status"):
                return reply(c, 200, {"features": ["chunked", "status"]})
            if path.startswith("/uploads/"):
                return reply(c, 200, {"partial": 0, "complete": False})
            busy_n["n"] += 1
            reply(c, 409, {"busy": True, "partial": 0})
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass
    bl = socket.socket(); bl.bind(("127.0.0.1", 0)); bl.listen(16); bp = bl.getsockname()[1]
    def bloop():
        while True:
            c, _ = bl.accept()
            threading.Thread(target=busy_one, args=(c,), daemon=True).start()
    threading.Thread(target=bloop, daemon=True).start()
    trust = ssl.create_default_context(); trust.load_verify_locations(cert)
    old = ssl._create_default_https_context
    ssl._create_default_https_context = lambda: trust
    try:
        path = os.path.join(T, "early409.bin"); fill(path, 16 * 1024 * 1024)
        ep = NET.Endpoint(f"https://127.0.0.1:{sp}", "k" * 16, "early")
        t0 = time.time()
        m = NET.upload_file(ep, "early", "out", path, "f.bin", stall=8, log=lambda *a: None)
        dt = time.time() - t0
        assert m["size"] == 16 * 1024 * 1024 and dt < 8 and state["puts"] >= 3, (m, round(dt, 2), state)
        path2 = os.path.join(T, "busy.bin"); open(path2, "wb").write(b"b" * 32)
        epb = NET.Endpoint(f"https://127.0.0.1:{bp}", "k" * 16, "busy")
        t0 = time.time()
        try:
            NET.upload_file(epb, "busy", "out", path2, "f.bin", stall=8)
            raise AssertionError("persistent busy completed")
        except ConnectionError as e:
            assert "busy" in str(e)
        assert time.time() - t0 < 8, time.time() - t0
        assert busy_n["n"] == 1 + NET._BUSY_TRIES, busy_n
        return f"409 while not reading the 8MiB body resumed in {dt:.2f}s; busy stopped after {busy_n['n']} tries"
    finally:
        ssl._create_default_https_context = old

def scripted_upload(responses):
    """Tiny relay: scripted PUT status per file name. GET /uploads is an empty partial."""
    hits, p = {}, port()
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def _send(self, code, obj):
            raw = b"" if obj is None else (obj if isinstance(obj, (bytes, bytearray)) else json.dumps(obj).encode())
            self.send_response(code)
            self.send_header("Content-Length", str(len(raw)))
            if obj is not None and not isinstance(obj, (bytes, bytearray)):
                self.send_header("Content-Type", "application/json")
            elif isinstance(obj, (bytes, bytearray)) and obj:
                self.send_header("Content-Type", "text/plain")
            self.send_header("Connection", "close")
            self.end_headers()
            if raw:
                self.wfile.write(raw)
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/status":
                return self._send(200, {"ok": True, "version": 2, "features": ["status", "chunked", "relpath"]})
            if path.startswith("/uploads/"):
                return self._send(200, {"partial": 0, "complete": False, "size": None, "sha256": None})
            self._send(404, {"error": "no route"})
        def do_PUT(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            name = urllib.parse.unquote(self.path.split("?", 1)[0].rstrip("/").split("/")[-1])
            hits[name] = hits.get(name, 0) + 1
            code, obj = responses[name][min(hits[name] - 1, len(responses[name]) - 1)]
            self._send(code, obj)
        def log_message(self, *a):
            pass
    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", p), H).serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{p}", hits

@test("malformed 400 is fatal; short body with no progress stops; a higher partial resumes")
def _():
    responses = {
        "bad.bin": [(400, {"error": "bad name"})],
        "off.bin": [(400, {"error": "bad offset/total"})],
        "plain.bin": [(400, {"error": "short body"})],
        "neg.bin": [(400, {"error": "short body", "partial": -1})],
        "bool.bin": [(400, {"error": "short body", "partial": True})],
        "str.bin": [(400, {"error": "short body", "partial": "6"})],
        "huge.bin": [(400, {"error": "short body", "partial": 10 ** 9})],
        "junk.bin": [(400, b"not-json")],
        "zero.bin": [(400, {"error": "short body", "partial": 0})],
        "grow.bin": [(400, {"error": "short body", "partial": 10}),
                     (200, {"partial": 32, "complete": True})],
    }
    url, hits = scripted_upload(responses)
    ep = NET.Endpoint(url, "k" * 16, "script")
    path = os.path.join(T, "script.bin"); open(path, "wb").write(b"abcdefghij" * 3 + b"12")  # 32 bytes
    fatal = []
    for name in ("bad.bin", "off.bin", "plain.bin", "neg.bin", "bool.bin", "str.bin", "huge.bin", "junk.bin"):
        try:
            NET.upload_file(ep, "script-1", "out", path, name)
            raise AssertionError(name + " was accepted")
        except urllib.error.HTTPError as e:
            fatal.append((name, e.code, hits[name]))
            assert e.code == 400 and hits[name] == 1, (name, e.code, hits[name], getattr(e, "relay_body", "")[:80])
        except NET.Mismatch:
            raise
    try:
        NET.upload_file(ep, "script-1", "out", path, "zero.bin")
        raise AssertionError("no-progress short body ran to completion")
    except urllib.error.HTTPError as e:
        assert e.code == 400 and hits["zero.bin"] == 1 + NET._SHORT_STALLS, (hits["zero.bin"], e.relay_body)
    logs = []
    m = NET.upload_file(ep, "script-1", "out", path, "grow.bin", log=lambda *a: logs.append(a))
    assert m["size"] == 32 and hits["grow.bin"] == 2, hits["grow.bin"]
    assert any(a[0] == "resume" and str(a[3]).startswith("10/") for a in logs), logs
    return f"{len(fatal)} malformed 400s raised on the first PUT; zero.bin stopped at {hits['zero.bin']}; grow resumed at 10"

@test("Range download resumes; wrong bytes fail the sha256 check and never appear")
def _():
    tid, n = "chunk-1", 20 * 1024 * 1024 + 123
    want = sha_file(os.path.join(T, "chunk.bin"))
    ep = NET.Endpoint(URL, open(SECRET).read().strip(), "a")
    d = os.path.join(T, "dl"); os.makedirs(d, exist_ok=True)
    dest = os.path.join(d, "ok.bin")
    with open(dest + ".partial", "wb") as f: f.write(open(os.path.join(T, "chunk.bin"), "rb").read(5 * 1024 * 1024))
    NET.download_file(ep, tid, "out", "chunk.bin", dest, n, want)
    assert sha_file(dest) == want and not os.path.exists(dest + ".partial")
    assert '"GET /files/chunk-1/out/chunk.bin HTTP/1.1" 206' in open(f"{T}/server.log").read()
    bad = os.path.join(d, "bad.bin")
    with open(bad + ".partial", "wb") as f: f.write(os.urandom(5 * 1024 * 1024))
    try:
        NET.download_file(ep, tid, "out", "chunk.bin", bad, n, want); raise AssertionError("bad bytes accepted")
    except NET.Mismatch:
        pass
    assert not os.path.exists(bad) and not os.path.exists(bad + ".partial")
    return "206 resume from 5MiB, sha256 ok; corrupted partial -> Mismatch, nothing delivered"

@test("large attachment: staged chunked upload box->Mac, sha256 end to end")
def _():
    kill("worker"); kill("agent"); worker(); agent()
    mb = 24 if QUICK else 64
    src = os.path.join(T, "att.bin")
    with open(src, "wb") as f:
        for _i in range(mb): f.write(os.urandom(1024 * 1024))
    want = sha_file(src)
    tid = send("alpha", "hermes", "copyin please", src)
    assert "uploaded att.bin" in send.err, send.err
    r = wait_result(tid, 120); expect_wakes(tid, "alpha")
    assert sha_file(os.path.join(T, "mac-state", "tasks", tid, "in", "att.bin")) == want
    assert sha_file(os.path.join(RES, tid, "copy-att.bin")) == want
    assert "committed " + tid in open(f"{T}/server.log").read()
    return f"{mb} MiB in via staged PUT and back out; sha256 {want[:12]} matched at Mac and box"

@test("nested outbox paths keep their relative path")
def _():
    tid = send("beta", "hermes", "nested please"); r = wait_result(tid, 30); expect_wakes(tid, "beta")
    p = os.path.join(RES, tid, "sub", "dir", "deep.txt")
    assert open(p).read() == "deep\n" and p in r["files"], r["files"]
    return "out/sub/dir/deep.txt -> results/<id>/sub/dir/deep.txt"

@test("long prompt (300 KB) via --prompt-file and worker stdin")
def _():
    big = ("long-prompt\u00e9 " * 50000)[:100000] + "x" * 200000 + " end"
    pf = os.path.join(T, "prompt.txt"); open(pf, "w").write(big)
    want = hashlib.sha256(big.encode()).hexdigest()[:16]
    p = run_send({}, "alpha", "hermes", "-", "--prompt-file", pf)
    assert p.returncode == 0, p.stderr
    tid = p.stdout.strip(); r = wait_result(tid, 30); expect_wakes(tid, "alpha")
    assert f"len={len(big.encode())} sha={want}" in r["summary"], r["summary"]
    return f"{len(big.encode())} bytes; tool saw the same sha256 prefix {want}"

@test("old (pre-v2) server: legacy multipart in, single PUT out, plain download")
def _():
    old = os.path.join(T, "old_server.py")
    open(old, "w").write(subprocess.run(["git", "-C", ROOT, "show", "4782f20:relay/server.py"], capture_output=True,
                                        text=True, check=True).stdout)
    spawn("old", [sys.executable, old], RELAY_PORT=str(OLDP), RELAY_DATA_DIR=os.path.join(T, "data-old")); time.sleep(0.6)
    cfg = wcfg(OLDURL, "worker-old.json", state_dir_name="mac-state-old", fallback=None)
    spawn("worker-old", [sys.executable, f"{ROOT}/mac/relay-worker.py", cfg])
    spawn("agent-old", [sys.executable, f"{ROOT}/bin/relay-box-agent"], RELAY_URL=OLDURL)
    try:
        src = os.path.join(T, "small.png"); open(src, "wb").write(os.urandom(4000))
        p = run_send({"RELAY_URL": OLDURL}, "alpha", "hermes", "big copyin nested", "--file", src)
        assert p.returncode == 0, p.stderr
        tid = p.stdout.strip(); r = wait_result(tid, 60); time.sleep(2)
        w = wakes_for(tid); assert [x[1]["status"] for x in w] == ["done"], w  # old server's started event: acked, no wake
        local = os.path.join(T, "mac-state-old", "tasks", tid, "out", "big.bin")
        assert sha_file(os.path.join(RES, tid, "big.bin")) == sha_file(local)
        assert os.path.exists(os.path.join(RES, tid, "sub__dir__deep.txt")), os.listdir(os.path.join(RES, tid))
        assert open(os.path.join(RES, tid, "copy-small.png"), "rb").read() == open(src, "rb").read()
        huge = os.path.join(T, "huge.bin")
        with open(huge, "wb") as f: f.truncate(17 * 1024 * 1024)
        p = run_send({"RELAY_URL": OLDURL}, "alpha", "hermes", "too big", "--file", huge)
        assert p.returncode == 1 and "16MiB multipart cap" in p.stderr, p.stderr
        return "old server: multipart in, 16MiB+ PUT out, nested names flattened; >16MiB input refused (exit 1)"
    finally:
        for n in ("worker-old", "agent-old", "old"): kill(n)

@test("relay-send: primary down -> enqueues via RELAY_FALLBACK_URL")
def _():
    e = dict(ENV, RELAY_URL=f"http://127.0.0.1:{DEAD}", RELAY_FALLBACK_URL=URL)
    p = subprocess.run([f"{ROOT}/bin/relay-send", "beta", "hermes", "send via fallback"], env=e, capture_output=True, text=True)
    assert p.returncode == 0 and "accepted" in p.stderr, (p.returncode, p.stderr)
    tid = p.stdout.strip()
    wait_result(tid, 40); time.sleep(2); expect_wakes(tid, "beta"); return "enqueued on fallback, one done wake"

@test("relay-send: enqueue failure exits 1")
def _():
    e = dict(ENV, RELAY_URL=f"http://127.0.0.1:{DEAD}")
    e.pop("RELAY_FALLBACK_URL", None)
    p = subprocess.run([f"{ROOT}/bin/relay-send", "alpha", "hermes", "no server"], env=e, capture_output=True, text=True)
    assert p.returncode == 1, (p.returncode, p.stderr, p.stdout)
    assert p.stdout.strip() == ""
    assert "enqueue failed" in p.stderr
    return "exit 1, no task id"

@test("relay-send: accepted wait timeout exits 2")
def _():
    kill("worker"); kill("agent")
    try:
        tid = "not-accepted-1"
        e = dict(ENV, RELAY_STARTED_WAIT_S="0.5")
        t0 = time.time()
        p = subprocess.run([f"{ROOT}/bin/relay-send", "alpha", "hermes", "queued only", "--id", tid],
                           env=e, capture_output=True, text=True)
        assert time.time() - t0 < 5, time.time() - t0
        assert p.returncode == 2, (p.returncode, p.stderr, p.stdout)
        assert p.stdout.strip() == tid and "\n" not in p.stdout.strip()
        assert "已入队、尚未接单" in p.stderr and "accepted" not in p.stderr, p.stderr
        task = json.loads(api("GET", f"/tasks/{tid}")[1])["task"]
        assert task["state"] in ("queued", "claimed"), task
        st, raw, _ = api("POST", f"/tasks/{tid}/result", json.dumps({
            "status": "failed", "summary": "test closed"}).encode(), "application/json")
        assert st == 200, (st, raw)
        assert api("POST", f"/results/{tid}/ack", b"{}", "application/json")[0] == 200
        assert not wakes_for(tid)
        return "exit 2, id on stdout, 已入队、尚未接单"
    finally:
        worker(); agent()

def box_agent_mod():
    if not hasattr(box_agent_mod, "m"):
        os.environ["RELAY_URL"] = URL
        os.environ["RELAY_SECRET_FILE"] = SECRET
        os.environ["RELAY_RESULTS_DIR"] = RES
        os.environ["RELAY_WAKE_DIR"] = WAKE
        import importlib.machinery, importlib.util
        name, path = "relay_box_agent_under_test", os.path.join(ROOT, "bin", "relay-box-agent")
        loader = importlib.machinery.SourceFileLoader(name, path)
        spec = importlib.util.spec_from_loader(name, loader)
        mod = importlib.util.module_from_spec(spec); loader.exec_module(mod)
        box_agent_mod.m = mod
    return box_agent_mod.m

@test("order_for_size: big attachments prefer the https fallback")
def _():
    import relay.client as rc
    sec = "x"
    a = rc.Endpoint("http://127.0.0.1:1", sec, "primary")
    b = rc.Endpoint("https://relay.example.com/relay", sec, "backup")
    c = rc.Endpoint("http://127.0.0.1:2", sec, "backup2")
    prev = os.environ.get("RELAY_BIG_BYTES")
    try:
        os.environ.pop("RELAY_BIG_BYTES", None)
        assert [e.name for e in rc.order_for_size([a, b, c], 100)] == ["primary", "backup", "backup2"]
        assert [e.name for e in rc.order_for_size([a, b, c], 8 * 1024 * 1024 + 1)] == ["backup", "primary", "backup2"]
        assert [e.name for e in rc.order_for_size([a, c], 9 * 1024 * 1024)] == ["backup2", "primary"]
        os.environ["RELAY_BIG_BYTES"] = "50"
        assert [e.name for e in rc.order_for_size([a, b, c], 51)] == ["backup", "primary", "backup2"]
        assert rc.big_bytes() == 50
    finally:
        if prev is None:
            os.environ.pop("RELAY_BIG_BYTES", None)
        else:
            os.environ["RELAY_BIG_BYTES"] = prev
    return "https fallback first when big; no-https uses first fallback; small unchanged"

@test("big attachments enqueue on the public/fallback channel first")
def _():
    src = os.path.join(T, "bigroute.bin")
    open(src, "wb").write(os.urandom(200))
    e = {"RELAY_URL": URL, "RELAY_FALLBACK_URL": URL2, "RELAY_BIG_BYTES": "100"}
    p = run_send(e, "alpha", "hermes", "copyin please", "--file", src)
    assert p.returncode == 0, (p.returncode, p.stderr, p.stdout)
    assert "big-attach" in p.stderr and "-> backup" in p.stderr, p.stderr
    assert "via backup" in p.stderr, p.stderr
    tid = p.stdout.strip()
    r = wait_result(tid, 40); expect_wakes(tid, "alpha")
    wlog = open(f"{T}/worker.log").read()
    assert f"claimed {tid} hermes via fallback" in wlog, wlog[-2000:]
    assert sha_file(os.path.join(RES, tid, "copy-bigroute.bin")) == sha_file(src)
    return "200-byte file over RELAY_BIG_BYTES=100 enqueued and claimed on backup; 1 wake"

@test("big result uploads to the public channel first")
def _():
    kill("worker"); kill("agent")
    cfg = wcfg(URL, "worker-big.json", state_dir_name="mac-state-big",
               public_endpoint="fallback", big_bytes=100, fallback=URL2)
    try:
        worker(cfg); agent()
        p = run_send({"RELAY_URL": URL, "RELAY_FALLBACK_URL": URL2}, "alpha", "hermes", "media please")
        assert p.returncode == 0 and "via primary" in p.stderr, p.stderr
        tid = p.stdout.strip()
        wait_log("worker", f"big-result {tid}", 30)
        wait_log("worker", f"uploaded {tid} done", 40)
        r = wait_result(tid, 40); expect_wakes(tid, "alpha")
        wait_log("agent", f"delivered {tid} done via backup", 20)
        wlog = open(f"{T}/worker.log").read()
        assert f"big-result {tid}" in wlog and "mirror -> fallback" in wlog
        assert f"uploaded {tid} done" in wlog and "via fallback" in wlog
        assert os.path.getsize(os.path.join(RES, tid, "clip.mp4")) > 200_000
        return "small task claimed on primary; result mirrored to fallback immediately; 1 wake"
    finally:
        kill("worker"); worker(); agent()

def _forward(target, rec):
    """Loopback alias in front of one relay. Records method, path, and Range."""
    p = port()
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def _go(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else None
            rec.append((self.command, self.path, self.headers.get("Range")))
            hdrs = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection", "content-length")}
            r = urllib.request.Request(target + self.path, data=body, method=self.command, headers=hdrs)
            try:
                with OPENER.open(r, timeout=70) as x:
                    st, d, ct = x.status, x.read(), x.headers.get("Content-Type")
            except urllib.error.HTTPError as e:
                st, d, ct = e.code, e.read(), e.headers.get("Content-Type")
            self.send_response(st)
            self.send_header("Content-Length", str(len(d)))
            if ct:
                self.send_header("Content-Type", ct)
            self.end_headers()
            self.wfile.write(d)
        do_GET = do_POST = do_PUT = _go
        def log_message(self, *a):
            pass
    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", p), H).serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{p}"

def sha_file_bytes(b):
    return hashlib.sha256(b).hexdigest()

def _hit_server(base, tid, name, body):
    assert api_at(base, "POST", "/tasks", json.dumps({
        "id": tid, "sender": "alpha", "tool": "hermes", "prompt": "dl", "mirror": True}).encode(),
        "application/json")[0] == 201, tid
    st, raw, _ = api_at(base, "PUT", f"/tasks/{tid}/files/{name}?kind=out", body, "application/octet-stream")
    assert st == 200, (tid, st, raw[:120])

@test("large result prefers the public alias when it has the same files; ack stays on the event")
def _():
    vp = port()
    spawn("vpsdl", [sys.executable, f"{ROOT}/relay/server.py"], RELAY_PORT=str(vp),
          RELAY_DATA_DIR=os.path.join(T, "data-vpsdl"))
    time.sleep(0.5)
    base = f"http://127.0.0.1:{vp}"
    pub_hits, ts_hits, quiet_hits, lie_hits = [], [], [], []
    pub = _forward(base, pub_hits)
    ts = _forward(base, ts_hits)
    qp = port()
    class Quiet(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def _no(self):
            quiet_hits.append((self.command, self.path.split("?", 1)[0]))
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            self.send_response(404); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")
        do_GET = do_POST = do_PUT = _no
        def log_message(self, *a):
            pass
    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", qp), Quiet).serve_forever, daemon=True).start()
    lp = port()
    class Lie(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def do_GET(self):
            lie_hits.append(self.path.split("?", 1)[0])
            path = lie_hits[-1]
            if path.startswith("/uploads/"):
                obj = {"partial": 0, "complete": True, "size": 1, "sha256": "ab" * 32}
            else:
                obj = {"error": "not the file"}
            raw = json.dumps(obj).encode()
            self.send_response(200 if path.startswith("/uploads/") else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def log_message(self, *a):
            pass
    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", lp), Lie).serve_forever, daemon=True).start()
    blob = b"R" * (256 * 1024)
    _hit_server(base, "bigdl-alias-1", "blob.bin", blob)
    small = b"xyz"
    _hit_server(base, "smalldl-alias-1", "s.bin", small)
    other = b"Q" * (64 * 1024)
    _hit_server(base, "bigdl-other-1", "o.bin", other)
    sec = open(SECRET).read().strip()
    mod = box_agent_mod()
    old_eps, prev = list(mod.EPS), os.environ.get("RELAY_BIG_BYTES")
    os.environ["RELAY_BIG_BYTES"] = "1000"
    try:
        mod.EPS = [NET.Endpoint(f"http://127.0.0.1:{qp}", sec, "primary"),
                   NET.Endpoint(pub, sec, "backup"),
                   NET.Endpoint(ts, sec, "backup2")]
        sha = sha_file_bytes(blob)
        body = {"id": "bigdl-alias-1", "sender": "alpha", "tool": "hermes", "status": "done",
                "summary": "alias", "files": ["blob.bin"],
                "files_meta": [{"name": "blob.bin", "size": len(blob), "sha256": sha}]}
        os.makedirs(os.path.join(RES, "bigdl-alias-1"), exist_ok=True)
        with open(os.path.join(RES, "bigdl-alias-1", "blob.bin.partial"), "wb") as f:
            f.write(blob[:4096])
        mod.handle(sec, body, mod.EPS[2])  # event is the tailnet alias
        got = os.path.join(RES, "bigdl-alias-1", "blob.bin")
        assert sha_file(got) == sha and not os.path.exists(got + ".partial")
        assert any(m == "GET" and "/files/" in path and rng == "bytes=4096-" for m, path, rng in pub_hits), pub_hits
        assert not any("/files/" in path for _, path, _ in ts_hits), ts_hits
        assert any(m == "POST" and path.startswith("/results/bigdl-alias-1/ack") for m, path, _ in ts_hits), ts_hits
        assert not any(m == "POST" and "/ack" in path for m, path, _ in pub_hits), pub_hits
        assert quiet_hits == [], quiet_hits
        expect_wakes("bigdl-alias-1", "alpha")
        nget = sum(1 for m, path, _ in pub_hits if m == "GET" and "/files/" in path)
        mod.handle(sec, body, mod.EPS[1])  # duplicate offered on the public alias
        assert sum(1 for m, path, _ in pub_hits if m == "GET" and "/files/" in path) == nget
        assert any(m == "POST" and "/results/bigdl-alias-1/ack" in path for m, path, _ in pub_hits)
        expect_wakes("bigdl-alias-1", "alpha")
        ts_before = len(ts_hits)
        pub_before = len(pub_hits)
        sbody = {"id": "smalldl-alias-1", "sender": "alpha", "tool": "hermes", "status": "done",
                 "summary": "small", "files": ["s.bin"],
                 "files_meta": [{"name": "s.bin", "size": len(small), "sha256": sha_file_bytes(small)}]}
        mod.handle(sec, sbody, mod.EPS[2])
        assert sha_file(os.path.join(RES, "smalldl-alias-1", "s.bin")) == sha_file_bytes(small)
        new_ts = ts_hits[ts_before:]
        new_pub = pub_hits[pub_before:]
        assert any(m == "GET" and "/files/" in path for m, path, _ in new_ts), new_ts
        assert not any("/files/" in path or "/uploads/" in path for _, path, _ in new_pub), new_pub
        expect_wakes("smalldl-alias-1", "alpha")
        # A different server advertising the same name with a different sha is not a source.
        mod.EPS = [NET.Endpoint(base, sec, "primary"), NET.Endpoint(f"http://127.0.0.1:{lp}", sec, "backup")]
        obody = {"id": "bigdl-other-1", "sender": "alpha", "tool": "hermes", "status": "done",
                 "summary": "other", "files": ["o.bin"],
                 "files_meta": [{"name": "o.bin", "size": len(other), "sha256": sha_file_bytes(other)}]}
        mod.handle(sec, obody, mod.EPS[0])
        assert sha_file(os.path.join(RES, "bigdl-other-1", "o.bin")) == sha_file_bytes(other)
        assert any(p.startswith("/uploads/") for p in lie_hits), lie_hits
        assert not any(p.startswith("/files/") for p in lie_hits), lie_hits
        expect_wakes("bigdl-other-1", "alpha")
        # https is the public endpoint, but it is down: do not assume it shares the files.
        dead = port()  # nothing listens; https so it is the public endpoint, and it has no files
        mod.EPS = [NET.Endpoint(f"http://127.0.0.1:{qp}", sec, "primary"),
                   NET.Endpoint(f"https://127.0.0.1:{dead}/relay", sec, "backup"),
                   NET.Endpoint(base, sec, "backup2")]
        quiet_hits.clear()
        hid = "bigdl-https-1"
        _hit_server(base, hid, "h.bin", other)
        hbody = {"id": hid, "sender": "alpha", "tool": "hermes", "status": "done", "summary": "https",
                 "files": ["h.bin"], "files_meta": [{"name": "h.bin", "size": len(other), "sha256": sha_file_bytes(other)}]}
        mod.handle(sec, hbody, mod.EPS[2])
        assert sha_file(os.path.join(RES, hid, "h.bin")) == sha_file_bytes(other)
        assert quiet_hits == [], quiet_hits
        expect_wakes(hid, "alpha")
        return "public alias served the Range download; tailnet acked; duplicate did not re-fetch; small stayed on the event; wrong sha and dead https were not used"
    finally:
        mod.EPS = old_eps
        if prev is None:
            os.environ.pop("RELAY_BIG_BYTES", None)
        else:
            os.environ["RELAY_BIG_BYTES"] = prev
        kill("vpsdl")

@test("late box-agent gets the files once; abandoned public row is not an empty done")
def _():
    kill("worker"); kill("agent")
    sp = port()
    puts, results = {}, []
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def _send(self, code, obj=None):
            raw = b"" if obj is None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(raw)))
            if obj is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            if raw:
                self.wfile.write(raw)
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/health", "/status"):
                return self._send(200, {"ok": True, "version": 2, "features": ["status", "chunked", "relpath", "mirror"]})
            if path.startswith("/uploads/"):
                return self._send(200, {"partial": 0, "complete": False, "size": None, "sha256": None})
            if path == "/tasks/next":
                time.sleep(0.5); return self._send(204)
            self._send(404, {"error": "no route"})
        def do_PUT(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            name = urllib.parse.unquote(self.path.split("?", 1)[0].rstrip("/").split("/")[-1])
            puts[name] = puts.get(name, 0) + 1
            self._send(400, {"error": "short body", "partial": 0})
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            path = self.path.split("?", 1)[0]
            if path == "/tasks":
                return self._send(201, {"id": "x", "state": "running"})
            if path.endswith("/result"):
                results.append(json.loads(raw or b"{}")); return self._send(200, {"id": "x"})
            if path.endswith("/started"):
                return self._send(200, {"id": "x"})
            self._send(404, {"error": "no route"})
        def log_message(self, *a):
            pass
    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", sp), H).serve_forever, daemon=True).start()
    cfg = wcfg(URL, "worker-close.json", state_dir_name="mac-state-close",
               public_endpoint="fallback", big_bytes=100, fallback=f"http://127.0.0.1:{sp}",
               closeout_after_s=1)
    try:
        worker(cfg)  # box-agent stays down past closeout_after_s
        p = run_send({"RELAY_URL": URL}, "alpha", "hermes", "media please")
        assert p.returncode == 0 and "via primary" in p.stderr, p.stderr
        tid = p.stdout.strip()
        wait_log("worker", f"uploaded {tid} done", 40)
        time.sleep(3)  # longer than closeout_after_s=1; a timer must not invent an empty done
        assert results == [], results
        wlog = open(f"{T}/worker.log").read()
        assert any(f"big-result {tid}" in ln and "public failed:" in ln and "HTTPError" in ln and " 400 " in ln
                   for ln in wlog.splitlines()), wlog[-1500:]
        assert f"uploaded {tid} done" in wlog and "via primary" in wlog
        assert f"public partial left {tid} on fallback" in wlog
        assert f"closeout {tid}" not in wlog
        assert f"upload error {tid}" not in wlog
        root = os.path.join(T, "mac-state-close", "tasks", tid)
        assert not os.path.exists(os.path.join(root, "quarantine"))
        assert not os.path.exists(os.path.join(root, "upload-4xx"))
        assert not os.path.exists(os.path.join(root, "closeout.json"))
        assert puts.get("stdout.txt") == 1 + NET._SHORT_STALLS, puts
        assert max(puts.values()) <= 1 + NET._SHORT_STALLS, puts
        agent()
        r = wait_result(tid, 40); expect_wakes(tid, "alpha")
        assert r["files"] and os.path.getsize(os.path.join(RES, tid, "clip.mp4")) > 200_000
        assert "this copy has none" not in (r.get("summary") or "")
        time.sleep(2)
        assert results == [], results
        expect_wakes(tid, "alpha")
        task = json.loads(api("GET", f"/tasks/{tid}")[1])["task"]
        assert task["state"] == "done" and task["files"], task
        return f"agent started after closeout; public got no result POST; files delivered once ({puts.get('stdout.txt')} short bodies)"
    finally:
        if P.get("worker") and P["worker"].poll() is None:
            kill("worker")
        kill("agent")
        worker(); agent()

@test("stale started event is acked, not webhooked")
def _():
    mod = box_agent_mod()
    tid = "stale-started-1"
    n0 = len(WAKES)
    body = {"id": tid, "sender": "alpha", "status": "started", "summary": "Mac 已接单（hermes）",
            "tool": "hermes", "files": []}
    sec = open(SECRET).read().strip()
    mod.handle(sec, body); mod.handle(sec, body)  # second delivery still does not POST
    assert len(WAKES) == n0
    marker = os.path.join(RES, tid, ".wake-started")
    assert open(marker).read().strip() == "skipped/no-webhook", open(marker).read()
    return "acked, .wake-started=skipped/no-webhook"

@test("started confirm is queryable and does not block done delivery")
def _():
    kill("worker"); kill("agent")
    try:
        # Drop anything already finished so this assertion sees only the rows it creates.
        for _i in range(20):
            st, raw, _ = api("GET", "/results/next?wait=0")
            if st == 204:
                break
            assert st == 200, (st, raw)
            got = json.loads(raw)["task"]
            assert got["status"] in ("done", "failed"), got
            assert api("POST", f"/results/{got['id']}/ack", b"{}", "application/json")[0] == 200
        else:
            raise AssertionError("results queue did not drain")
        tid = "accept-mark-1"
        st, raw, _ = api("POST", "/tasks", json.dumps({
            "id": tid, "sender": "alpha", "tool": "hermes", "prompt": "mark"}).encode(), "application/json")
        assert st == 201, (st, raw)
        # /started only moves claimed|running. Claim the row, then confirm through the API.
        db = sqlite3.connect(os.path.join(T, "data", "relay.db"), timeout=5)
        db.execute("update tasks set state='claimed', started=? where id=?", (time.time(), tid))
        db.commit()
        st, raw, _ = api("POST", f"/tasks/{tid}/started", b"{}", "application/json")
        assert st == 200, (st, raw)
        task = json.loads(api("GET", f"/tasks/{tid}")[1])["task"]
        assert task["state"] == "running", task
        sw = db.execute("select started_wake, state from tasks where id=?", (tid,)).fetchone()
        assert sw == (1, "running"), sw
        st, raw, _ = api("GET", "/results/next?wait=0")
        assert st == 204, (st, raw)
        # A legacy pending started wake must not be preferred over an undelivered result.
        db.execute("update tasks set started_wake=0 where id=?", (tid,))
        db.commit()
        other = "accept-done-1"
        st, raw, _ = api("POST", "/tasks", json.dumps({
            "id": other, "sender": "beta", "tool": "hermes", "prompt": "done"}).encode(), "application/json")
        assert st == 201, (st, raw)
        st, raw, _ = api("POST", f"/tasks/{other}/result", json.dumps({
            "status": "done", "summary": "finished"}).encode(), "application/json")
        assert st == 200, (st, raw)
        st, raw, _ = api("GET", "/results/next?wait=1")
        assert st == 200, (st, raw)
        got = json.loads(raw)["task"]
        assert got["id"] == other and got["status"] == "done", got
        assert got["status"] != "started"
        assert api("POST", f"/results/{other}/ack", b"{}", "application/json")[0] == 200
        st, raw, _ = api("GET", "/results/next?wait=0")
        assert st == 204, (st, raw)  # legacy started_wake=0 is not delivered
        st, raw, _ = api("POST", f"/tasks/{other}/started", b"{}", "application/json")
        assert st == 200, (st, raw)
        done = json.loads(api("GET", f"/tasks/{other}")[1])["task"]
        assert done["state"] == "done", done  # confirm must not rewind a finished task
        db.close()
        return "started_wake=1; legacy 0 does not block done"
    finally:
        worker(); agent()

def load_server():
    import importlib.util
    spec = importlib.util.spec_from_file_location("relay_server_under_test", os.path.join(ROOT, "relay", "server.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

def raw_http(method, path, body=b""):
    """Send one HTTP/1.1 request with the path bytes exactly as given (no client normalization)."""
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    try:
        head = f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: {bearer()}\r\n"
        if method != "GET":
            head += f"Content-Type: application/octet-stream\r\nContent-Length: {len(body)}\r\n"
        s.sendall(head.encode() + b"\r\n" + (body if method != "GET" else b""))
        f = s.makefile("rb")
        status = f.readline().decode()
        hdrs = {}
        while True:
            line = f.readline()
            if line in (b"\r\n", b"\n", b""): break
            k, v = line.decode().split(":", 1); hdrs[k.lower()] = v.strip()
        return status, f.read(int(hdrs.get("content-length", "0")))
    finally:
        s.close()

@test("unicode filename PUT/GET; traversal and empty 400; tid stays ASCII")
def _():
    kill("worker"); kill("agent")
    try:
        srv = load_server()
        assert srv.safe_name("验收结果.md") == "验收结果.md"
        assert srv.safe_name("a..b.md") == "a..b.md"
        assert srv.safe_name("..") is None and srv.safe_name("../evil") is None
        assert srv.safe_name("..\\evil") is None and srv.safe_name("") is None
        assert srv.safe_name("a/b") is None and srv.safe_name("a\\b") is None
        assert srv.safe_name("a\x00b") is None and srv.safe_name("n" * 129) is None
        assert srv.safe_name("验" * 20) == "验" * 20  # 60 bytes, under the cap
        assert srv.safe_name("验" * 100) is None  # 300 bytes, would not fit on disk
        assert srv.SAFE.match("task-1") and not srv.SAFE.match("验收") and not srv.SAFE.match("n" * 129)
        tid = "cjk-name-1"
        st, raw, _ = api("POST", "/tasks", json.dumps({
            "id": tid, "sender": "alpha", "tool": "hermes", "prompt": "unicode name"}).encode(), "application/json")
        assert st == 201, (st, raw)
        name, body = "验收结果.md", "验收通过\n".encode()
        qn = urllib.parse.quote(name, safe="")
        st, raw, _ = api("PUT", f"/tasks/{tid}/files/{qn}?kind=out", body, "application/octet-stream")
        assert st == 200, (st, raw)
        got = json.loads(raw)
        assert got["name"] == name and got["bytes"] == len(body), got
        st, raw, _ = api("GET", f"/files/{tid}/out/{qn}")
        assert st == 200 and raw == body, (st, raw)
        disk = os.path.join(T, "data", "files", tid, "out", name)
        assert open(disk, "rb").read() == body, "stored under a different name"
        st, raw, _ = api("PUT", f"/tasks/{tid}/files/{urllib.parse.quote('a..b.md', safe='')}?kind=out",
                         b"dots", "application/octet-stream")
        assert st == 200, (st, raw)
        st, raw, _ = api("POST", f"/tasks/{tid}/result", json.dumps({
            "status": "done", "summary": "cjk file", "files": [name, "a..b.md", "missing.bin"]}).encode(),
                         "application/json")
        assert st == 200, (st, raw)
        task = json.loads(api("GET", f"/tasks/{tid}")[1])["task"]
        assert task["files"] == [name, "a..b.md"], task
        st, raw, _ = api("POST", f"/results/{tid}/ack", b"{}", "application/json")
        assert st == 200, (st, raw)
        # Attachment names use the same safe_name path.
        at = "cjk-att-1"
        boundary, payload = "bndcjk", "附件".encode()
        mp = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"id\"\r\n\r\n{at}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"sender\"\r\n\r\nalpha\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"tool\"\r\n\r\nhermes\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\nattach\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
        st, raw, _ = api("POST", "/tasks", mp, f"multipart/form-data; boundary={boundary}")
        assert st == 201, (st, raw)
        st, raw, _ = api("GET", f"/files/{at}/in/{qn}")
        assert st == 200 and raw == payload, (st, raw)
        st, raw, _ = api("POST", f"/tasks/{at}/result", json.dumps({
            "status": "done", "summary": "att"}).encode(), "application/json")
        assert st == 200, (st, raw)
        assert api("POST", f"/results/{at}/ack", b"{}", "application/json")[0] == 200
        st, raw, _ = api("POST", "/tasks", json.dumps({
            "id": "任务", "sender": "alpha", "tool": "hermes", "prompt": "no"}).encode(), "application/json")
        assert st == 400, (st, raw)
        st, raw, _ = api("PUT", "/tasks/" + urllib.parse.quote("任务", safe="") + "/files/a.txt?kind=out",
                         b"hi", "application/octet-stream")
        assert st == 400 and b"bad name" in raw, (st, raw)
        bad_paths = [
            f"/tasks/{tid}/files/..%2Fevil?kind=out",
            f"/tasks/{tid}/files/../evil?kind=out",
            f"/tasks/{tid}/files/?kind=out",
            f"/tasks/{tid}/files/..?kind=out",
            f"/tasks/{tid}/files/%2e%2e?kind=out",
            f"/tasks/{tid}/files/a%00b?kind=out",
            f"/tasks/{tid}/files/..%5Cevil?kind=out",
            f"/tasks/{tid}/files/{'n' * 129}?kind=out",
            f"/files/{tid}/out/..%2Fevil",
            f"/files/{tid}/out/../evil",
            f"/files/{tid}/out/",
            f"/files/{tid}/out/..",
        ]
        for path in bad_paths:
            status, resp = raw_http("PUT" if path.startswith("/tasks/") else "GET", path, b"x")
            assert "400" in status and b"bad name" in resp, (path, status, resp)
        return "PUT/GET 验收结果.md; ../evil and empty 400; tid ASCII"
    finally:
        worker(); agent()

@test("one task upload 4xx does not block others; quarantine keeps files")
def _():
    kill("worker"); kill("agent")
    try:
        bad, good = "iso-bad", "iso-good"
        assert bad < good  # sweep is sorted; a head-of-line return would skip good
        for tid in (bad, good):
            st, raw, _ = api("POST", "/tasks", json.dumps({
                "id": tid, "sender": "alpha", "tool": "hermes", "prompt": "iso"}).encode(), "application/json")
            assert st == 201, (st, raw)
            st, raw, _ = api("POST", f"/tasks/{tid}/result", json.dumps({
                "status": "done", "summary": "seed"}).encode(), "application/json")
            assert st == 200, (st, raw)
            assert api("POST", f"/results/{tid}/ack", b"{}", "application/json")[0] == 200
        root = os.path.join(T, "mac-state-iso")
        def seed(tid, out_name, out_body):
            d = os.path.join(root, "tasks", tid); out = os.path.join(d, "out"); os.makedirs(out)
            open(os.path.join(d, "stdout.txt"), "w").write(f"line {tid}\n")
            open(os.path.join(d, "exitcode"), "w").write("0")
            open(os.path.join(d, "pid"), "w").write("999999")
            open(os.path.join(d, "confirmed"), "w").write("1")
            open(os.path.join(out, out_name), "wb").write(out_body)
            return d
        long_name = "n" * 129
        cjk = "验收结果.md"
        cjk_body = "验收通过\n".encode()
        bad_dir = seed(bad, long_name, b"nope")
        good_dir = seed(good, cjk, cjk_body)
        # A log-line counter would already be over the threshold before any real 4xx.
        with open(f"{T}/worker.log", "a") as logf:
            for _i in range(9):
                logf.write(f"upload error {bad} 400 {{\"error\": \"bad name\"}}\n")
        worker(CFG_ISO)
        saw_isolated = False
        end = time.time() + 20
        cpath, qpath = os.path.join(bad_dir, "upload-4xx"), os.path.join(bad_dir, "quarantine")
        gpath = os.path.join(good_dir, "uploaded")
        while time.time() < end:
            # Counter stays at 1 for the backoff gap. Good must already be uploaded then,
            # which is only true if the sweep continued past the bad task.
            if os.path.isfile(cpath) and not os.path.isfile(qpath):
                n = int(open(cpath).read().strip() or "0")
                if n == 1 and os.path.isfile(gpath):
                    saw_isolated = True
            if os.path.isfile(qpath) and os.path.isfile(gpath):
                break
            time.sleep(0.1)
        else:
            raise AssertionError(
                f"timed out; counter={os.path.isfile(cpath)} quarantine={os.path.isfile(qpath)} uploaded={os.path.isfile(gpath)}")
        assert saw_isolated, "good task was not uploaded during the first 4xx"
        assert int(open(cpath).read().strip()) == 2
        assert open(qpath).read().strip() == "2"
        assert not os.path.exists(os.path.join(bad_dir, "uploaded"))
        assert open(os.path.join(bad_dir, "stdout.txt")).read().startswith("line ")
        assert open(os.path.join(bad_dir, "out", long_name), "rb").read() == b"nope"
        st, raw, _ = api("GET", f"/files/{good}/out/{urllib.parse.quote(cjk, safe='')}")
        assert st == 200 and raw == cjk_body, (st, raw)
        wlog = open(f"{T}/worker.log", encoding="utf-8").read()
        stamped = [ln for ln in wlog.splitlines() if f" upload error {bad} 400 " in ln and "T" in ln[:20]]
        assert len(stamped) == 2, stamped
        assert all('{"error": "bad name"}' in ln for ln in stamped), stamped
        assert f"quarantine {bad} consecutive-4xx 2" in wlog
        assert f"uploaded {good} done" in wlog and f"put-file {good} out {cjk}" in wlog
        return "good uploaded while bad was at 1x 4xx; quarantine kept files"
    finally:
        if P["worker"].poll() is None:
            kill("worker")
        worker(); agent()

@test("secret rotation: every line accepted, file reloaded live, clients send the first line")
def _():
    orig = open(SECRET).read()
    old, new = orig.strip(), "rotated-" + os.urandom(12).hex()
    def code(tok):
        try:
            return api("GET", "/status", headers={"Authorization": "Bearer " + tok})[0]
        except urllib.error.HTTPError as e:
            return e.code
    try:
        with open(SECRET, "w") as f: f.write(new + "\n" + old + "\n")
        time.sleep(2.5)
        assert code(new) == 200 and code(old) == 200, (code(new), code(old))
        sys.path.insert(0, ROOT); import relay.client as rc
        assert rc.secret(SECRET) == new
        with open(SECRET, "w") as f: f.write(new + "\n")
        time.sleep(2.5)
        assert code(new) == 200 and code(old) == 401, (code(new), code(old))
        with open(SECRET, "w") as f: f.write("short\n")  # broken edit keeps the last good keys
        time.sleep(2.5)
        assert code(new) == 200, code(new)
        assert "secret file not reloaded" in open(f"{T}/server.log").read()
    finally:
        with open(SECRET, "w") as f: f.write(orig)
        time.sleep(2.5)
    assert code(old) == 200
    return "new+old both 200; old removed -> 401 within 2.5 s, no restart; bad edit ignored"

@test("worker embeds relay/net.py verbatim")
def _():
    net = open(os.path.join(ROOT, "relay", "net.py")).read().strip()
    assert net in open(os.path.join(ROOT, "mac", "relay-worker.py")).read(), "mac/relay-worker.py net block drifted"
    return "net block identical"

for n in list(P): kill(n)
# Each confirmed task contributes exactly one final wake (done|failed). No started webhooks.
# gamma (no wake config) contributes 0. Count by task_id.
by = {}
for slug, body, _auth in WAKES:
    by.setdefault(body["task_id"], []).append(body["status"])
bad = {tid: st for tid, st in by.items() if st not in (["done"], ["failed"])}
ROWS.append(("global: each task one final wake, no started webhook", "PASS" if not bad else "FAIL", "-",
             f"{len(WAKES)} wakes / {len(by)} tasks" + (f"; bad={bad}" if bad else "")))
with open(os.path.join(ROOT, "TESTS.md"), "w") as f:
    f.write(f"# Self-test results\n\nLoopback run on the dev box, {time.strftime('%Y-%m-%d %H:%M %Z')}. "
            f"`python3 tests/selftest.py{' --quick' if QUICK else ''}`\n\n| Test | Result | Duration | Notes |\n|---|---|---|---|\n")
    for r in ROWS: f.write("| " + " | ".join(str(x).replace("|", "/") for x in r) + " |\n")
print("logs in", T); sys.exit(any(r[1] == "FAIL" for r in ROWS))
