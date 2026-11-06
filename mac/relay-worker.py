#!/usr/bin/env python3
"""mac-relay worker (runs on the Mac). Stdlib only, self-contained.
Usage: relay-worker.py /path/to/worker.json
Long-polls the relay, runs each task as a detached subprocess (no timeout), uploads
stdout + outbox files exactly once. Unacked results are re-uploaded after restarts."""
import json, os, signal, subprocess, sys, threading, time, uuid, urllib.request, urllib.parse

def log(*a):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), *a, file=sys.stderr, flush=True)

CFG = json.load(open(sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.config/mac-relay/worker.json")))
STATE = os.path.expanduser(CFG["state_dir"]); TASKS = os.path.join(STATE, "tasks")
os.makedirs(TASKS, exist_ok=True)
SECRET = open(os.path.expanduser(CFG["secret_file"]), "rb").read().strip().decode()
WORKER = CFG.get("worker_name", "mac")
PROCS = {}

class _SrcHTTPHandler(urllib.request.HTTPHandler):
    """Bind the fallback socket to a local source IP (e.g. the Mac's Tailscale IP) so macOS scoped
    routing sends it out the Tailscale utun even if another 100.64/10 route (en0/VPN) shadows it."""
    def __init__(self, src): super().__init__(); self.src = src
    def http_open(self, r):
        import http.client
        return self.do_open(lambda host, **kw: http.client.HTTPConnection(host, source_address=(self.src, 0), **kw), r)

def _fallback_opener():
    hs = [urllib.request.ProxyHandler({})]  # fallback is a tailnet address: never via system/env proxy
    if CFG.get("fallback_source_ip"): hs.append(_SrcHTTPHandler(CFG["fallback_source_ip"]))
    return urllib.request.build_opener(*hs)
FALLBACK_OPENER = _fallback_opener()
PRIMARY_OPENER = urllib.request.build_opener()

def multipart(fields, files):
    b = uuid.uuid4().hex; out = []
    for k, v in fields.items():
        out += [f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n".encode(), str(v).encode(), b"\r\n"]
    for p in files:
        out += [f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{os.path.basename(p)}\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n".encode(), open(p, "rb").read(), b"\r\n"]
    out.append(f"--{b}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={b}"

class Conn:
    """Primary/fallback selection. Switch to fallback only after `fail_threshold` consecutive
    primary failures; stay there (sticky) until a primary probe succeeds after `cooldown_s`,
    or until manual reset (touch <state_dir>/reset-primary or SIGHUP)."""
    def __init__(self):
        self.f = os.path.join(STATE, "conn.json"); self.lock = threading.Lock()
        s = json.load(open(self.f)) if os.path.exists(self.f) else {}
        self.mode, self.since, self.fails = s.get("mode", "primary"), s.get("since", 0), 0
        self.last_probe = 0
    def save(self):
        json.dump({"mode": self.mode, "since": self.since}, open(self.f, "w"))
    def set(self, mode):
        if mode != self.mode:
            log("connection ->", mode); self.mode, self.since, self.fails = mode, time.time(), 0; self.save()
    def base(self):
        with self.lock:
            rp = os.path.join(STATE, "reset-primary")
            if os.path.exists(rp):
                os.remove(rp); self.set("primary")
            if self.mode == "fallback" and time.time() - self.since >= CFG.get("cooldown_s", 300) \
                    and time.time() - self.last_probe >= CFG.get("cooldown_s", 300):
                self.last_probe = time.time()
                try:
                    PRIMARY_OPENER.open(CFG["primary_url"].rstrip("/") + "/health", timeout=5).read(); self.set("primary")
                except Exception:
                    log("primary still down; staying on fallback")
            if self.mode == "fallback": return CFG["fallback_url"], FALLBACK_OPENER
            return CFG["primary_url"], PRIMARY_OPENER
    def failed(self):
        with self.lock:
            if self.mode == "primary":
                self.fails += 1
                if CFG.get("fallback_url") and self.fails >= CFG.get("fail_threshold", 3):
                    self.set("fallback")
    def ok(self):
        with self.lock:
            if self.mode == "primary": self.fails = 0

CONN = Conn()
signal.signal(signal.SIGHUP, lambda *a: CONN.set("primary"))

def req(method, path, body=None, ctype=None, timeout=70):
    base, opener = CONN.base()
    r = urllib.request.Request(base.rstrip("/") + path, data=body, method=method)
    r.add_header("Authorization", "Bearer " + SECRET)
    if ctype: r.add_header("Content-Type", ctype)
    try:
        with opener.open(r, timeout=timeout) as resp:
            data = resp.read(); CONN.ok(); return resp.status, data
    except urllib.error.HTTPError as e:
        # 5xx (502/503/504 from Caddy/CDN) = channel broken -> counts toward failover; 4xx = app-level answer
        CONN.failed() if e.code >= 500 else CONN.ok(); raise
    except Exception:
        CONN.failed(); raise

def start(task):
    tid = task["id"]; d = os.path.join(TASKS, tid)
    if os.path.exists(os.path.join(d, "pid")):
        return confirm(tid)  # already started (idempotent redelivery)
    inbox, outbox = os.path.join(d, "in"), os.path.join(d, "out")
    os.makedirs(inbox, exist_ok=True); os.makedirs(outbox, exist_ok=True)
    for n in task["attachments"]:
        _, data = req("GET", f"/files/{urllib.parse.quote(tid)}/in/{urllib.parse.quote(n)}")
        open(os.path.join(inbox, n), "wb").write(data)
    json.dump(task, open(os.path.join(d, "task.json"), "w"))
    pf = os.path.join(d, "prompt.txt"); open(pf, "w").write(task["prompt"])
    tmpl = CFG["tools"].get(task["tool"])
    if not tmpl:
        open(os.path.join(d, "stderr.txt"), "w").write(f"no command template for tool {task['tool']}\n")
        open(os.path.join(d, "exitcode"), "w").write("127"); open(os.path.join(d, "pid"), "w").write("0"); return
    sub = {"prompt": task["prompt"], "prompt_file": pf, "inbox": inbox, "outbox": outbox, "task_id": tid}
    cmd = [a.format(**sub) for a in tmpl]
    wrapper = 'cd "$RELAY_OUTBOX"; "$@" >"$RELAY_TASK_DIR/stdout.txt" 2>"$RELAY_TASK_DIR/stderr.txt"; ' \
              'echo $? >"$RELAY_TASK_DIR/exitcode.tmp"; mv "$RELAY_TASK_DIR/exitcode.tmp" "$RELAY_TASK_DIR/exitcode"'
    env = dict(os.environ, RELAY_TASK_ID=tid, RELAY_INBOX=inbox, RELAY_OUTBOX=outbox, RELAY_TASK_DIR=d)
    p = subprocess.Popen(["/bin/sh", "-c", wrapper, "relay-task"] + cmd, env=env, start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    PROCS[tid] = p; open(os.path.join(d, "pid"), "w").write(str(p.pid))
    log("started", tid, task["tool"], "pid", p.pid)
    confirm(tid)

def confirm(tid):
    """Tell the relay we own this task so it is not redelivered. Best effort: the sweep re-confirms."""
    try:
        req("POST", f"/tasks/{urllib.parse.quote(tid)}/started", b"{}", "application/json", timeout=15)
        open(os.path.join(TASKS, tid, "confirmed"), "w").write("1")
    except Exception as e:
        log("confirm pending", tid, type(e).__name__)

def alive(pid):
    try: os.kill(pid, 0); return True
    except OSError: return False

def last_line(path):
    try: lines = [l.strip() for l in open(path, errors="replace") if l.strip()]
    except OSError: return ""
    return lines[-1][:240] if lines else ""

def sweep():
    """Upload every finished-but-unacked task. Also detects processes lost across reboots."""
    for tid in os.listdir(TASKS):
        d = os.path.join(TASKS, tid)
        if os.path.exists(os.path.join(d, "uploaded")) or not os.path.exists(os.path.join(d, "pid")):
            continue
        if tid in PROCS: PROCS[tid].poll()
        # Confirm start even after the tool exits (first confirm may have failed mid-failover).
        if not os.path.exists(os.path.join(d, "confirmed")):
            confirm(tid)
        ec_f = os.path.join(d, "exitcode")
        if os.path.exists(ec_f):
            ec = int(open(ec_f).read().strip() or 1)
        elif not alive(int(open(os.path.join(d, "pid")).read() or 0)):
            ec = -1; open(ec_f, "w").write("-1")  # lost (e.g. reboot): report failed once
        else:
            continue
        if ec == 0:
            status, summary = "done", last_line(os.path.join(d, "stdout.txt")) or "completed (no output)"
        else:
            status = "failed"
            summary = ("process lost (worker host restarted?)" if ec == -1 else f"exit {ec}: ") + \
                      (last_line(os.path.join(d, "stderr.txt")) or last_line(os.path.join(d, "stdout.txt")))
        files = [os.path.join(d, n) for n in ("stdout.txt", "stderr.txt")
                 if os.path.exists(os.path.join(d, n)) and os.path.getsize(os.path.join(d, n))]
        out = os.path.join(d, "out")
        files += sorted(os.path.join(out, n) for n in os.listdir(out) if os.path.isfile(os.path.join(out, n)))
        body, ct = multipart({"status": status, "summary": summary}, files)
        try:
            req("POST", f"/tasks/{urllib.parse.quote(tid)}/result", body, ct, timeout=300)
        except Exception as e:
            log("upload pending", tid, type(e).__name__); return False
        open(os.path.join(d, "uploaded"), "w").write(status); PROCS.pop(tid, None)
        log("uploaded", tid, status)
    return True

def poll_loop():
    backoff = 1
    while True:
        try:
            st, data = req("GET", f"/tasks/next?worker={urllib.parse.quote(WORKER)}&wait=50")
            backoff = 1
            if st == 200:
                task = json.loads(data)["task"]
                try:
                    start(task)
                except Exception as e:  # claimed but could not start -> one failed result
                    d = os.path.join(TASKS, task["id"]); os.makedirs(os.path.join(d, "out"), exist_ok=True)
                    open(os.path.join(d, "stderr.txt"), "w").write(f"start failed: {type(e).__name__}: {e}\n")
                    open(os.path.join(d, "exitcode"), "w").write("126"); open(os.path.join(d, "pid"), "w").write("0")
        except Exception as e:
            log("poll error", type(e).__name__, "retry in", backoff); time.sleep(backoff)
            backoff = min(backoff * 2, CFG.get("max_backoff_s", 60))

def main():
    log("worker", WORKER, "state", STATE)
    threading.Thread(target=poll_loop, daemon=True).start()
    backoff = 1
    while True:  # local-only scan of task dirs; network only when something finished
        ok = sweep()
        backoff = 1 if ok else min(backoff * 2, CFG.get("max_backoff_s", 60))
        time.sleep(1 if ok else backoff)

if __name__ == "__main__":
    main()
