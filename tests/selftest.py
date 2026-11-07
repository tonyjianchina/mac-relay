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
    json.dump({"primary_url": primary, "fallback_url": URL, "secret_file": SECRET, "state_dir": os.path.join(T, state),
               "worker_name": "mac", "fail_threshold": 2, "cooldown_s": 3600, "max_backoff_s": 4,
               "tools": {t: stub + [t, "{prompt}"] for t in ("hermes", "deepseek", "codex", "schwab")}, **kw}, open(p, "w"))
    return p
CFG_OK, CFG_FB = wcfg(URL, "worker.json"), wcfg(f"http://127.0.0.1:{DEAD}", "worker-fb.json")
CFG_503 = wcfg(f"http://127.0.0.1:{FLAKY}", "worker-503.json", cooldown_s=4, fallback_source_ip="127.0.0.1")
CFG_ISO = wcfg(URL, "worker-iso.json", state_dir_name="mac-state-iso", upload_4xx_quarantine=2, max_backoff_s=2)
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
def expect_wakes(tid, slug, final="done"):
    """Each confirmed task: exactly one started wake, then exactly one done|failed wake."""
    w = wakes_for(tid)
    assert len(w) == 2, f"{tid}: want 2 wakes (started+{final}), got {len(w)}: {[x[1].get('status') for x in w]}"
    assert [x[1]["status"] for x in w] == ["started", final], [x[1]["status"] for x in w]
    for x in w:
        assert x[0] == slug and x[2] == f"Bearer key-{slug}", "wrong wake route"
        assert set(x[1]) == {"task_id", "status", "summary", "sender", "result_path"}, x[1]
    assert "Mac 已接单" in w[0][1]["summary"], w[0][1]["summary"]
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

server(); worker(); agent()

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

@test("started wake before done wake")
def _():
    tid = send("alpha", "hermes", "order check"); r = wait_result(tid, 20); expect_wakes(tid, "alpha")
    w = wakes_for(tid)
    assert w[0][1]["status"] == "started" and w[1][1]["status"] == "done"
    assert "hermes" in w[0][1]["summary"]; return "started then done"

@test(f"long task (~{LONG}s)")
def _():
    tid = send("alpha", "codex", f"sleep={LONG}"); r = wait_result(tid, LONG + 40); expect_wakes(tid, "alpha")
    assert r["status"] == "done"; return f"slept {LONG}s, no timeout"

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
    kill("worker"); tid = send("beta", "hermes", "quick")
    # simulate: worker gone, task queued; restart worker, it runs; then kill/restart again right after -> no dup
    worker(); r = wait_result(tid, 40); kill("worker"); worker(); time.sleep(3); expect_wakes(tid, "beta"); return "no duplicate result/wake"

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
    assert not wakes_for(tid); n = open(f"{T}/agent.log").read().count(f"not announced"); assert n == 2, n
    return "2 error lines (started+done), 0 wakes"

@test("primary down -> fallback used (sticky)")
def _():
    kill("worker"); os.remove(os.path.join(T, "mac-state/conn.json")) if os.path.exists(os.path.join(T, "mac-state/conn.json")) else None
    worker(CFG_FB); tid = send("beta", "hermes", "via fallback"); wait_result(tid, 40); expect_wakes(tid, "beta")
    st = json.load(open(os.path.join(T, "mac-state/conn.json"))); assert st["mode"] == "fallback", st
    tid2 = send("beta", "hermes", "still fallback"); wait_result(tid2, 20); expect_wakes(tid2, "beta")
    st2 = json.load(open(os.path.join(T, "mac-state/conn.json"))); assert st2 == st, "flip-flopped"
    return "switched after 2 primary failures, stayed on fallback"

@test("primary 503 -> fallback, one delivery, then sticky switch back")
def _():
    cf = os.path.join(T, "mac-state/conn.json")
    kill("worker"); os.path.exists(cf) and os.remove(cf); BROKEN["on"] = True
    worker(CFG_503); tid = send("alpha", "hermes", "primary says 503"); wait_result(tid, 40); time.sleep(2); expect_wakes(tid, "alpha")
    st = json.load(open(cf)); assert st["mode"] == "fallback", st
    time.sleep(5); st2 = json.load(open(cf)); assert st2 == st, "probed a still-503 primary and flipped"
    BROKEN["on"] = False; end = time.time() + 70
    while time.time() < end and json.load(open(cf))["mode"] != "primary": time.sleep(0.5)
    assert json.load(open(cf))["mode"] == "primary", "did not switch back after primary healed"
    tid2 = send("alpha", "hermes", "back on primary"); wait_result(tid2, 40); time.sleep(2); expect_wakes(tid2, "alpha")
    log = open(f"{T}/worker.log").read(); assert log.count("connection -> fallback") >= 1
    return "503 x2 -> fallback; stayed while 503; healed -> primary; started+done each"

@test("box-agent: primary 503 -> fallback, one wake, then switch back")
def _():
    alog = f"{T}/agent.log"; n0, p0 = open(alog).read().count("connection -> fallback"), open(alog).read().count("connection -> primary")
    kill("agent"); BROKEN["on"] = True
    # Leave worker as-is (may be on fallback from prior test); confirm-before-upload still sets started_wake.
    agent(RELAY_URL=f"http://127.0.0.1:{FLAKY}", RELAY_FALLBACK_URL=URL, RELAY_FAIL_THRESHOLD="2", RELAY_COOLDOWN_S="4")
    end = time.time() + 30
    while time.time() < end and open(alog).read().count("connection -> fallback") == n0: time.sleep(0.3)
    assert open(alog).read().count("connection -> fallback") == n0 + 1, "agent did not switch to fallback"
    tid = send("alpha", "hermes", "agent on fallback"); wait_result(tid, 90); time.sleep(2); expect_wakes(tid, "alpha")
    time.sleep(5); assert open(alog).read().count("connection -> primary") == p0, "flipped back while primary still 503"
    BROKEN["on"] = False; end = time.time() + 70  # long-poll (50s) must return before the next probe
    while time.time() < end and open(alog).read().count("connection -> primary") == p0: time.sleep(0.5)
    assert open(alog).read().count("connection -> primary") == p0 + 1, "agent did not switch back"
    tid2 = send("beta", "hermes", "agent back on primary"); wait_result(tid2, 40); time.sleep(2); expect_wakes(tid2, "beta")
    assert open(alog).read().count("connection -> fallback") == n0 + 1, "flip-flopped"
    return "2x503 -> fallback; started+done wakes; healed -> primary"

@test("relay-send: primary down -> enqueues via RELAY_FALLBACK_URL")
def _():
    e = dict(ENV, RELAY_URL=f"http://127.0.0.1:{DEAD}", RELAY_FALLBACK_URL=URL)
    tid = subprocess.run([f"{ROOT}/bin/relay-send", "beta", "hermes", "send via fallback"], env=e, capture_output=True, text=True, check=True).stdout.strip()
    wait_result(tid, 40); time.sleep(2); expect_wakes(tid, "beta"); return "enqueued on fallback, started+done"

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

for n in list(P): kill(n)
# Each task that called /started should contribute exactly 2 wakes (started + final).
# gamma (no wake config) contributes 0. Count by task_id.
from collections import Counter
by = {}
for slug, body, _auth in WAKES:
    by.setdefault(body["task_id"], []).append(body["status"])
bad = {tid: st for tid, st in by.items() if st != ["started", "done"] and st != ["started", "failed"]}
ROWS.append(("global: each task started then done|failed once", "PASS" if not bad else "FAIL", "-",
             f"{len(WAKES)} wakes / {len(by)} tasks" + (f"; bad={bad}" if bad else "")))
with open(os.path.join(ROOT, "TESTS.md"), "w") as f:
    f.write(f"# Self-test results\n\nLoopback run on the dev box, {time.strftime('%Y-%m-%d %H:%M %Z')}. "
            f"`python3 tests/selftest.py{' --quick' if QUICK else ''}`\n\n| Test | Result | Duration | Notes |\n|---|---|---|---|\n")
    for r in ROWS: f.write("| " + " | ".join(str(x).replace("|", "/") for x in r) + " |\n")
print("logs in", T); sys.exit(any(r[1] == "FAIL" for r in ROWS))
