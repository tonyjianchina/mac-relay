#!/usr/bin/env python3
"""mac-relay worker (runs on the Mac). Stdlib only, self-contained.
Usage: relay-worker.py /path/to/worker.json

Channels: `endpoints` in worker.json is an ordered list of relay servers ({"name","url"},
first = primary). Old configs with primary_url/fallback_url still work. The worker long-polls
EVERY endpoint at once, so a task enqueued on any channel is found as soon as that channel is
reachable; there is no sticky failover state to recover from.

At most once: the task directory is the ledger. The first claim of a task id writes
task.json under a lock; a later claim of the same id from any channel (redelivery, or the
same task posted to two servers) is confirmed but never run again.

Results go back to the channel(s) the task was claimed from, primary first. If the result
files together exceed `big_bytes` (default 8 MiB, or RELAY_BIG_BYTES), they go to the public
channel first (the first https endpoint, or `public_endpoint` in the config) — the task is
mirrored there if it was not claimed on that channel. If none of the origins accepts the
result for `mirror_after_s` (default 60), the worker mirrors the task onto another channel
(POST /tasks {"mirror":true}) and returns the result there; box agents consume every channel
and dedupe by task id.

Files: outputs (stdout, stderr, everything under the outbox, recursively, keeping relative
paths) are uploaded with chunked, resumable PUTs (8 MiB, sha256 checked by the server) on
servers that support it, and one streaming PUT on old servers. Attachments are downloaded
with Range resume and checked against the sha256 the sender supplied. Every request has a
hard deadline and a stall watchdog. Acceptance (/started) is confirmed before attachments
are downloaded, so a large input does not hold the sender's started wait.
Big attachments are fetched from the public https endpoint first (the same
`public_endpoint` / first-https rule as big results), so a claim on a slow
URL of the same server still uses the fast path and can resume mid-file.

One task's upload failure does not stop the others. HTTP 4xx (other than 404 unknown task)
counts toward upload_4xx_quarantine (default 3): a quarantine marker is written and the files
stay. Test hook: `prompt_routes` [{"prefix": "...", "tool": "..."}] runs another configured
tool when a prompt starts with that prefix (used for the deterministic selftest tool)."""
import json, os, re, signal, subprocess, sys, threading, time, urllib.error

def log(*a):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), *a, file=sys.stderr, flush=True)

# --- BEGIN mac-relay net (keep identical to the copy in mac/relay-worker.py; tests check) ---
# Stdlib HTTP for mac-relay clients: hard deadlines, stall detection, chunked resumable
# upload with sha256, resumable verified download. Shared by box tools and the Mac worker.
import hashlib, http.client, json, os, socket, threading, time, urllib.error, urllib.parse, urllib.request

CHUNK_BYTES = 8 * 1024 * 1024


def qseg(s):
    """Percent-encode one URL path segment (slashes too)."""
    return urllib.parse.quote(s, safe="")


def is_direct(url):
    """Tailnet and loopback hosts never use an HTTP proxy (Shadowrocket hijacks 100.64/10)."""
    host = (urllib.parse.urlparse(url).hostname or "").rstrip(".").lower()
    return (host.startswith("100.") or host.endswith(".ts.net") or host.startswith("127.")
            or host in ("localhost", "::1"))


class _Watch:
    """Closes the sockets of one request when it makes no progress for `stall` seconds or
    passes its wall-clock deadline, so a hung request fails in bounded time."""
    def __init__(self, conns, deadline, stall):
        self.conns, self.end, self.stall = conns, time.monotonic() + deadline, stall
        self.last, self.fired, self.ev = time.monotonic(), False, threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def kick(self):
        self.last = time.monotonic()

    def _run(self):
        while not self.ev.is_set():
            now = time.monotonic()
            limit = min(self.end, self.last + self.stall) if self.stall else self.end
            if now >= limit:
                self.fired = True
                for c in list(self.conns):
                    try:
                        if c.sock is not None:
                            c.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                return
            self.ev.wait(min(1.0, max(0.05, limit - now)))

    def done(self):
        self.ev.set()


class _Reg(threading.local):
    def __init__(self):
        self.conns = []


class _HTTP(urllib.request.HTTPHandler):
    def __init__(self, reg, src=None):
        super().__init__(); self.reg, self.src = reg, src

    def http_open(self, req):
        def mk(host, **kw):
            if self.src:
                kw["source_address"] = (self.src, 0)
            c = http.client.HTTPConnection(host, **kw); self.reg.conns.append(c); return c
        return self.do_open(mk, req)


class _HTTPS(urllib.request.HTTPSHandler):
    def __init__(self, reg):
        super().__init__(); self.reg = reg

    def https_open(self, req):
        def mk(host, **kw):
            c = http.client.HTTPSConnection(host, **kw); self.reg.conns.append(c); return c
        return self.do_open(mk, req, context=self._context)


class _Body:
    """File-like request body of exactly n bytes that reports progress to the watchdog."""
    def __init__(self, f, n, watch):
        self.f, self.left, self.watch = f, n, watch

    def read(self, k=65536):
        if self.left <= 0:
            return b""
        b = self.f.read(min(k, self.left, 256 * 1024))
        self.left -= len(b); self.watch.kick()
        return b


class Endpoint:
    """One relay base URL. `name` labels it in logs and in on-disk state."""
    def __init__(self, url, secret, name=None, source_ip=None, log=None):
        self.url, self.secret = url.rstrip("/"), secret
        self.name = name or urllib.parse.urlparse(url).hostname or url
        self.log = log or (lambda *a: None)
        self.reg = _Reg()
        hs = [_HTTP(self.reg, source_ip), _HTTPS(self.reg)]
        if is_direct(url):
            hs.insert(0, urllib.request.ProxyHandler({}))
        self.opener = urllib.request.build_opener(*hs)
        self.lock = threading.Lock()
        self.last_ok, self.down_since, self.fails = 0.0, None, 0
        self.caps, self.caps_at = None, 0.0  # server feature set; None = not probed yet

    def __repr__(self):
        return f"<{self.name} {self.url}>"

    def _ok(self):
        with self.lock:
            self.last_ok, self.fails = time.time(), 0
            if self.down_since is not None:
                self.log("channel", self.name, "recovered after", int(time.time() - self.down_since), "s")
                self.down_since, self.caps = None, None  # the server may have been upgraded

    def _fail(self, e):
        with self.lock:
            self.fails += 1
            if self.down_since is None:
                self.down_since = time.time()
                self.log("channel", self.name, "down:", type(e).__name__, str(e)[:160])

    def healthy(self):
        return self.down_since is None

    def call(self, method, path, body=None, ctype=None, headers=None, deadline=70, stall=None,
             length=None, sink=None, auth=True):
        """Returns (status, headers, data). sink(status, headers) -> write(bytes) streams the
        body instead (data is None). Raises urllib.error.HTTPError (with .relay_body) for >= 400
        and TimeoutError when the watchdog fired. 5xx and transport errors mark the channel
        down; 4xx do not. A short body (fewer bytes than Content-Length) is an error."""
        r = urllib.request.Request(self.url + path, method=method)
        if auth:
            r.add_header("Authorization", "Bearer " + self.secret)
        if ctype:
            r.add_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        self.reg.conns = []
        w = _Watch(self.reg.conns, deadline, stall)
        if hasattr(body, "read"):
            r.data = _Body(body, length, w); r.add_header("Content-Length", str(length))
        elif body is not None:
            r.data = body
        try:
            with self.opener.open(r, timeout=min(deadline, stall or deadline, 120)) as resp:
                w.kick()
                st, hd = resp.status, resp.headers
                write = sink(st, hd) if sink else None
                out, got = bytearray(), 0
                while True:
                    b = resp.read(256 * 1024)
                    if not b:
                        break
                    got += len(b); w.kick()
                    if write:
                        write(b)
                    else:
                        out += b
                cl = hd.get("Content-Length")
                if cl is not None and int(cl) != got:
                    raise ConnectionError(f"short body {got}/{cl}")
                data = None if sink else bytes(out)
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()
            except Exception:
                raw = b""
            e.relay_body = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
            (self._fail(e) if e.code >= 500 else self._ok())
            raise
        except Exception as e:
            if w.fired:
                e2 = TimeoutError(f"{self.name}: stalled or past deadline ({method} {path.split('?')[0]})")
                self._fail(e2); raise e2 from e
            self._fail(e); raise
        finally:
            w.done()
        self._ok()
        return st, hd, data

    def json(self, method, path, obj=None, deadline=30, **kw):
        body = None if obj is None else json.dumps(obj).encode()
        st, hd, data = self.call(method, path, body, "application/json" if body is not None else None,
                                 deadline=deadline, **kw)
        if data and hd.get_content_type() == "application/json":
            return st, json.loads(data)
        return st, {}

    def status(self):
        """GET /status on new servers: {"version", "features", "workers": {name: idle_s}}.
        Old servers answer 404; that is cached as an empty feature set and returns None."""
        try:
            _, j = self.json("GET", "/status", deadline=10, stall=10)
        except urllib.error.HTTPError as e:
            if e.code in (404, 405):
                self.caps, self.caps_at = set(), time.time(); return None
            raise
        self.caps, self.caps_at = set(j.get("features") or []), time.time()
        return j

    def capabilities(self):
        """Cached feature set. Re-probed after a recovery and every 10 minutes."""
        if self.caps is None or time.time() - self.caps_at > 600:
            self.status()
        return self.caps


_SHA = {}


def sha256_file(path):
    s = os.stat(path)
    key = (path, s.st_size, s.st_mtime_ns)
    if key not in _SHA:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(1024 * 1024), b""):
                h.update(b)
        _SHA[key] = h.hexdigest()
    return _SHA[key]


class Mismatch(Exception):
    """Checksum or size verification failed; the partial data was discarded."""


def upload_file(ep, tid, kind, path, name, chunk=CHUNK_BYTES, stall=60, log=None):
    """Upload one file as `name` (may contain '/' on new servers). New servers: chunked PUT
    with offset; resumes from the server's partial size; the server checks sha256 before the
    file becomes visible. Old servers: one streaming PUT. Returns {"name","size","sha256"}."""
    log = log or (lambda *a: None)
    size, sha = os.path.getsize(path), sha256_file(path)
    meta = {"name": name, "size": size, "sha256": sha}
    base = f"/tasks/{qseg(tid)}/files/{qseg(name)}?kind={kind}"
    if "chunked" not in ep.capabilities():
        with open(path, "rb") as f:
            ep.call("PUT", base, f, "application/octet-stream", length=size,
                    deadline=600 + size // (32 * 1024), stall=stall)
        return meta
    _, s = ep.json("GET", f"/uploads/{qseg(tid)}/{qseg(name)}?kind={kind}")
    if s.get("complete") and s.get("size") == size and s.get("sha256") == sha:
        return meta
    off = int(s.get("partial") or 0)
    reset = off > size
    if off and not reset:
        log("resume", tid, name, f"{off}/{size}", "on", ep.name)
    with open(path, "rb") as f:
        while True:
            if reset:
                off = 0
            n = min(chunk, size - off)
            f.seek(off)
            q = f"{base}&offset={off}&total={size}" + (f"&sha256={sha}" if off + n == size else "") + \
                ("&reset=1" if reset else "")
            try:
                _, _, data = ep.call("PUT", q, f, "application/octet-stream", length=n,
                                     deadline=120 + n // (16 * 1024), stall=stall)
                j = json.loads(data or b"{}")
                if "partial" not in j:
                    # An old server stored this chunk as a whole file. Never treat that as
                    # progress; re-probe and fall back to a full PUT on the next attempt.
                    ep.caps = None
                    raise ConnectionError(f"{ep.name}: server does not support chunked PUT")
            except urllib.error.HTTPError as e:
                if e.code == 409:  # offset disagrees with the server, or another upload holds it
                    try:
                        j = json.loads(e.relay_body or "{}")
                    except ValueError:
                        j = {}
                    off, reset = int(j.get("partial") or 0), False
                    if j.get("busy"):
                        time.sleep(2)
                    continue
                if e.code == 422:
                    raise Mismatch(f"{name}: server sha256 mismatch") from e
                raise
            reset = False
            if j.get("complete"):
                return meta
            off = int(j.get("partial", off + n))


def download_file(ep, tid, kind, name, dest, size=None, sha=None, stall=60):
    """GET /files/{tid}/{kind}/{name} into dest through dest.partial. Resumes with Range when
    the server answers 206; a 200 rewrites from zero. Checks size and sha256 when known.
    dest only appears once complete (and verified)."""
    part = dest + ".partial"
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if size is not None and have > size:
        have = 0
    if size is None or have != size:
        hdrs = {"Range": f"bytes={have}-"} if have else {}
        fh = []

        def sink(st, hd):
            f = open(part, "ab" if (st == 206 and have) else "wb"); fh.append(f); return f.write
        try:
            ep.call("GET", f"/files/{qseg(tid)}/{kind}/{qseg(name)}", headers=hdrs,
                    deadline=600 + (size or 0) // (16 * 1024), stall=stall, sink=sink)
        finally:
            for f in fh:
                f.close()
    if not os.path.exists(part):
        open(part, "wb").close()
    got = os.path.getsize(part)
    if size is not None and got != size:
        raise ConnectionError(f"{name}: have {got} of {size} bytes")
    if sha:
        h = sha256_file(part)
        if h != sha:
            os.remove(part)
            raise Mismatch(f"{name}: sha256 {h[:12]} != {sha[:12]}")
    os.replace(part, dest)
    return got


def safe_rel(n):
    """Relative file path for results/attachments: segments joined by '/', none empty, '.',
    '..', or containing a backslash, NUL, or control character. None if unsafe."""
    if not isinstance(n, str) or not n or n.startswith("/") or len(n) > 1024:
        return None
    segs = n.split("/")
    if len(segs) > 16:
        return None
    for s in segs:
        if s in ("", ".", "..") or "\\" in s or any(ord(c) < 32 or ord(c) == 127 for c in s):
            return None
        if len(s) > 128 or len(s.encode("utf-8", "surrogatepass")) > 247:
            return None
    return n
# --- END mac-relay net ---

CFG = json.load(open(sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.config/mac-relay/worker.json")))
STATE = os.path.expanduser(CFG["state_dir"]); TASKS = os.path.join(STATE, "tasks")
os.makedirs(TASKS, exist_ok=True)
SECRET = next((l.strip() for l in open(os.path.expanduser(CFG["secret_file"]), "rb").read().decode().splitlines()
               if l.strip()), "")  # first line; a rotation may leave "new\nold"
WORKER = CFG.get("worker_name", "mac")
SOFT = int(CFG.get("max_soft_file", 16 * 1024 * 1024))
MIRROR_AFTER = float(CFG.get("mirror_after_s", 60))
CLOSEOUT_AFTER = float(CFG.get("closeout_after_s", 600))
MAX_BACKOFF = float(CFG.get("max_backoff_s", 60))
MAX_DELIVER = int(CFG.get("max_parallel_uploads", 3))
ATTACH_DEADLINE = float(CFG.get("attachment_deadline_s", 6 * 3600))
_IP = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

def _endpoints():
    eps = list(CFG.get("endpoints") or [])
    if not eps:
        if CFG.get("primary_url"):
            eps.append({"name": "primary", "url": CFG["primary_url"]})
        if CFG.get("fallback_url"):
            eps.append({"name": "fallback", "url": CFG["fallback_url"], "source_ip": CFG.get("fallback_source_ip")})
    out = []
    for i, e in enumerate(eps):
        src = e.get("source_ip") if _IP.match(str(e.get("source_ip") or "")) else None
        out.append(Endpoint(e["url"], SECRET, e.get("name") or f"ch{i}", src, log))
    return out

EPS = _endpoints()
BYNAME = {e.name: e for e in EPS}
LEDGER = threading.Lock()

def _big_bytes():
    try:
        return max(0, int(CFG.get("big_bytes", os.environ.get("RELAY_BIG_BYTES", 8 * 1024 * 1024))))
    except (TypeError, ValueError):
        return 8 * 1024 * 1024

BIG_BYTES = _big_bytes()

def public_ep():
    """VPS public channel: CFG public_endpoint (name or URL), else the first https endpoint."""
    want = CFG.get("public_endpoint")
    if want:
        if want in BYNAME:
            return BYNAME[want]
        for e in EPS:
            if e.url.rstrip("/") == str(want).rstrip("/"):
                return e
    return next((e for e in EPS if e.url.lower().startswith("https://")), None)

def files_total(files):
    return sum(os.path.getsize(p) for p, _ in files if os.path.isfile(p))

def attach_sources(tid, total=0):
    """Download sources: public first when attachments are big or it is already an origin.
    vps and vps-ts can be the same server; trying the public URL first avoids the slow
    tailnet path after a claim on the tailnet URL."""
    org = list(origins(tid))
    pub = public_ep()
    candidates = list(org)
    if pub and pub not in candidates and total > BIG_BYTES:
        candidates = [pub] + candidates
    return sorted(candidates, key=lambda e: (not e.healthy(), e is not pub))

PROCS, PREP, DELIVERING, NEXT_TRY, FAIL_SINCE = {}, set(), set(), {}, {}

def tdir(tid, *p):
    return os.path.join(TASKS, tid, *p)

def _write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)

def origins(tid):
    """Endpoints this task was claimed from, in config order. Legacy dirs: all endpoints."""
    try:
        names = json.load(open(tdir(tid, "origins.json")))
    except (OSError, ValueError):
        return list(EPS)
    return [e for e in EPS if e.name in names] or list(EPS)

def add_origin(tid, ep):
    p = tdir(tid, "origins.json")
    try:
        names = json.load(open(p))
    except (OSError, ValueError):
        names = []
    if ep.name not in names:
        names.append(ep.name); _write(p, json.dumps(names))

def drop_origin(tid, ep):
    names = [e.name for e in origins(tid) if e.name != ep.name]
    _write(tdir(tid, "origins.json"), json.dumps(names))

def claim(task, ep):
    """Ledger: True only for the first claim of this task id on this Mac, from any channel."""
    tid = task["id"]
    with LEDGER:
        new = not os.path.exists(tdir(tid, "task.json")) and not os.path.exists(tdir(tid, "pid"))
        if new:
            os.makedirs(tdir(tid, "in"), exist_ok=True); os.makedirs(tdir(tid, "out"), exist_ok=True)
            _write(tdir(tid, "task.json"), json.dumps(task))
            _write(tdir(tid, "claimed"), ep.name)  # marks a v2 claim whose preparation may be resumed
        add_origin(tid, ep)
    return new

def confirm(ep, tid):
    """Tell that relay we own this task so it is not redelivered. The sweep retries."""
    try:
        ep.json("POST", f"/tasks/{qseg(tid)}/started", {}, deadline=20, stall=20)
        _write(tdir(tid, "confirmed." + ep.name), "1")
    except urllib.error.HTTPError as e:
        if e.code == 404:  # that server does not hold this task: nothing to confirm there
            _write(tdir(tid, "confirmed." + ep.name), "404")
        else:
            log("confirm pending", tid, ep.name, "HTTP", e.code)
    except Exception as e:
        log("confirm pending", tid, ep.name, type(e).__name__)

def confirmed(tid, ep):
    return os.path.exists(tdir(tid, "confirmed." + ep.name)) or os.path.exists(tdir(tid, "confirmed"))

def start_prep(tid):
    with LEDGER:
        if tid in PREP or os.path.exists(tdir(tid, "pid")):
            return
        PREP.add(tid)
    threading.Thread(target=prep, args=(tid,), daemon=True, name="prep-" + tid).start()

def fail_start(tid, msg):
    os.makedirs(tdir(tid, "out"), exist_ok=True)
    open(tdir(tid, "stderr.txt"), "w").write(msg + "\n")
    _write(tdir(tid, "exitcode"), "126"); _write(tdir(tid, "pid"), "0")
    log("start failed", tid, msg[:200])

def prep(tid):
    """Download attachments (resumable, verified), then launch the tool."""
    try:
        task = json.load(open(tdir(tid, "task.json")))
        inbox = tdir(tid, "in")
        meta = {m.get("name"): m for m in task.get("attachments_meta") or [] if isinstance(m, dict)}
        end, backoff = time.time() + ATTACH_DEADLINE, 2
        total = 0
        for n in task.get("attachments") or []:
            m = meta.get(n, {})
            try:
                total += int(m.get("size") or 0)
            except (TypeError, ValueError):
                pass
        srcs = attach_sources(tid, total)
        if srcs:
            log("attach", tid, total, "bytes via", ",".join(e.name for e in srcs))
        for n in task.get("attachments") or []:
            rel = safe_rel(n)
            if not rel:
                return fail_start(tid, f"start failed: bad attachment name {n!r}")
            dest = os.path.join(inbox, *rel.split("/"))
            m = meta.get(n, {})
            while not os.path.exists(dest):
                err = None
                for ep in srcs:
                    try:
                        download_file(ep, tid, "in", n, dest, m.get("size"), m.get("sha256"))
                        log("downloaded", tid, n, "via", ep.name)
                        break
                    except Exception as e:
                        err = e
                        if isinstance(e, urllib.error.HTTPError) and e.code == 404:
                            continue
                if os.path.exists(dest):
                    break
                if time.time() > end:
                    return fail_start(tid, f"start failed: attachment {n}: {type(err).__name__}: {err}")
                log("attachment retry", tid, n, type(err).__name__, str(err)[:120])
                time.sleep(backoff); backoff = min(backoff * 2, MAX_BACKOFF)
        launch(tid, task)
    except Exception as e:
        fail_start(tid, f"start failed: {type(e).__name__}: {e}")
    finally:
        with LEDGER:
            PREP.discard(tid)

def launch(tid, task):
    d = tdir(tid); inbox, outbox = tdir(tid, "in"), tdir(tid, "out")
    pf = os.path.join(d, "prompt.txt")
    with open(pf, "w") as f:
        f.write(task["prompt"])
    tool = task["tool"]
    for r in CFG.get("prompt_routes") or []:
        if r.get("prefix") and task["prompt"].startswith(r["prefix"]) and r.get("tool") in CFG["tools"]:
            tool = r["tool"]
    tmpl = CFG["tools"].get(tool)
    if not tmpl:
        open(os.path.join(d, "stderr.txt"), "w").write(f"no command template for tool {tool}\n")
        _write(os.path.join(d, "exitcode"), "127"); _write(os.path.join(d, "pid"), "0"); return
    sub = {"prompt": task["prompt"], "prompt_file": pf, "inbox": inbox, "outbox": outbox, "task_id": tid}
    stdin = subprocess.DEVNULL
    over = (CFG.get("stdin_prompt_over") or {}).get(tool)
    if over is not None and len(task["prompt"].encode()) > int(over) and "{prompt}" in tmpl:
        # Long prompts go through stdin ("-") instead of argv (macOS ARG_MAX is about 1 MiB).
        tmpl = ["-" if a == "{prompt}" else a for a in tmpl]; stdin = open(pf, "rb")
    cmd = [a.format(**sub) for a in tmpl]
    wrapper = 'cd "$RELAY_OUTBOX"; "$@" >"$RELAY_TASK_DIR/stdout.txt" 2>"$RELAY_TASK_DIR/stderr.txt"; ' \
              'echo $? >"$RELAY_TASK_DIR/exitcode.tmp"; mv "$RELAY_TASK_DIR/exitcode.tmp" "$RELAY_TASK_DIR/exitcode"'
    env = dict(os.environ, RELAY_TASK_ID=tid, RELAY_INBOX=inbox, RELAY_OUTBOX=outbox, RELAY_TASK_DIR=d)
    p = subprocess.Popen(["/bin/sh", "-c", wrapper, "relay-task"] + cmd, env=env, start_new_session=True,
                         stdin=stdin, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    PROCS[tid] = p; _write(os.path.join(d, "pid"), str(p.pid))
    log("started", tid, tool, "pid", p.pid)

def alive(pid):
    try: os.kill(pid, 0); return True
    except OSError: return False

def last_line(path):
    try: lines = [l.strip() for l in open(path, errors="replace") if l.strip()]
    except OSError: return ""
    return lines[-1][:240] if lines else ""

def result_files(d):
    """[(path, relative name)]: stdout/stderr, then the outbox recursively (relative paths)."""
    files = [(os.path.join(d, n), n) for n in ("stdout.txt", "stderr.txt")
             if os.path.exists(os.path.join(d, n)) and os.path.getsize(os.path.join(d, n))]
    out = os.path.join(d, "out")
    found = []
    for root, dirs, names in os.walk(out):
        dirs.sort()
        for n in names:
            p = os.path.join(root, n)
            rel = os.path.relpath(p, out).replace(os.sep, "/")
            if os.path.isfile(p):  # an unsafe name is sent anyway: the server's 400 quarantines
                found.append((p, rel))
    return files + sorted(found, key=lambda x: x[1])

def upload_result(ep, tid, status, summary, files):
    metas = []
    nested = "relpath" in ep.capabilities()
    for path, rel in files:
        name = rel if nested else rel.replace("/", "__")
        metas.append(upload_file(ep, tid, "out", path, name, log=progress_log(tid)))
        log("put-file", tid, "out", name, metas[-1]["size"], "->", ep.name)
    ep.json("POST", f"/tasks/{qseg(tid)}/result", {"status": status, "summary": summary,
            "files": [m["name"] for m in metas], "files_meta": metas}, deadline=60)

RESUMED = {}

def progress_log(tid):
    """A resume point that grew since the last attempt means the channel is slow, not dead:
    restart the mirror clock so a big upload over a weak link is not abandoned for a
    non-resumable copy elsewhere."""
    def f(*a):
        log(*a)
        if len(a) > 3 and a[0] == "resume":
            try:
                off = int(str(a[3]).split("/")[0])
            except ValueError:
                return
            if off > RESUMED.get((tid, a[2]), 0):
                RESUMED[(tid, a[2])] = off
                if tid in FAIL_SINCE:  # failing, but the last attempt moved data
                    FAIL_SINCE[tid] = time.time()
    return f

def mirror(ep, tid):
    """Create this task on another channel (not claimable on new servers) so its result can
    come back there. An old server queues it; our poller then sees a duplicate claim."""
    t = json.load(open(tdir(tid, "task.json")))
    ep.json("POST", "/tasks", {"id": tid, "sender": t["sender"], "tool": t["tool"],
                               "prompt": t.get("prompt") or "(mirror)", "mirror": True, "worker": WORKER}, deadline=60)

class Fatal4xx(Exception):
    def __init__(self, ep, code, body):
        super().__init__(f"{code} {body[:300]}"); self.ep, self.code, self.body = ep, code, body

def deliver(tid, status, summary, files):
    """Origins first (healthy ones first); after MIRROR_AFTER of failures, other channels.
    Results larger than big_bytes go to the public channel first, without waiting."""
    last = None
    pub = public_ep()
    if pub and files_total(files) > BIG_BYTES:
        try:
            if pub not in origins(tid):
                log("big-result", tid, files_total(files), "bytes mirror ->", pub.name)
                mirror(pub, tid); add_origin(tid, pub)
            else:
                log("big-result", tid, files_total(files), "bytes ->", pub.name)
            upload_result(pub, tid, status, summary, files)
            org = [o for o in origins(tid) if o is not pub]
            if org:
                _write(tdir(tid, "closeout.json"), json.dumps({
                    "eps": [o.name for o in org], "after": time.time() + CLOSEOUT_AFTER,
                    "status": status, "summary": f"{summary} [result files delivered via {pub.name}]"}))
            return pub.name
        except urllib.error.HTTPError as e:
            body = (getattr(e, "relay_body", "") or "").replace("\n", " ")
            if 400 <= e.code < 500 and not (e.code == 404 and "unknown task" in body):
                raise Fatal4xx(pub.name, e.code, body) from e
            last = e
            log("big-result", tid, "public failed:", type(e).__name__, str(e)[:200], "- falling back")
        except Exception as e:
            last = e
            log("big-result", tid, "public failed:", type(e).__name__, str(e)[:200], "- falling back")
    org = origins(tid)
    for ep in sorted(org, key=lambda e: not e.healthy()):
        try:
            upload_result(ep, tid, status, summary, files); return ep.name
        except urllib.error.HTTPError as e:
            body = (getattr(e, "relay_body", "") or "").replace("\n", " ")
            if e.code == 404 and "unknown task" in body:
                log("result", tid, "unknown on", ep.name, "- not an origin"); drop_origin(tid, ep); last = e; continue
            if 400 <= e.code < 500:
                raise Fatal4xx(ep.name, e.code, body) from e
            last = e
        except Exception as e:
            last = e
    since = FAIL_SINCE.setdefault(tid, time.time())
    others = [e for e in EPS if e not in org]
    if others and time.time() - since >= MIRROR_AFTER:
        for ep in sorted(others, key=lambda e: not e.healthy()):
            try:
                log("mirror", tid, "->", ep.name, "(origins failing for", int(time.time() - since), "s)")
                mirror(ep, tid); add_origin(tid, ep)
                upload_result(ep, tid, status, summary, files)
                # The origins still hold the task as running. Close them later with a file-less
                # result; by then the box-agent has the mirrored copy and only acks the closeout.
                _write(tdir(tid, "closeout.json"), json.dumps({
                    "eps": [o.name for o in org], "after": time.time() + CLOSEOUT_AFTER,
                    "status": status, "summary": f"{summary} [result files delivered via {ep.name}]"}))
                return ep.name
            except Exception as e:
                last = e
    raise last or RuntimeError("no channel")

def _read_int(p):
    try: return int(open(p).read().strip() or "0")
    except (OSError, ValueError): return 0

def _quarantine_after():
    try: return max(1, int(CFG.get("upload_4xx_quarantine", 3)))
    except (TypeError, ValueError): return 3

def deliver_task(tid, status, summary, files):
    d = tdir(tid)
    try:
        via = deliver(tid, status, summary, files)
    except Fatal4xx as e:
        log("upload error", tid, e.code, e.body[:300], "on", e.ep)
        n = _read_int(os.path.join(d, "upload-4xx")) + 1
        _write(os.path.join(d, "upload-4xx"), str(n))
        if n >= _quarantine_after():
            _write(os.path.join(d, "quarantine"), str(n)); log("quarantine", tid, "consecutive-4xx", n)
        backoff(tid)
    except Exception as e:
        log("upload pending", tid, type(e).__name__, str(e)[:200]); backoff(tid)
    else:
        try: os.remove(os.path.join(d, "upload-4xx"))
        except OSError: pass
        _write(os.path.join(d, "uploaded"), status); _write(os.path.join(d, "delivered_via"), via)
        PROCS.pop(tid, None); FAIL_SINCE.pop(tid, None); NEXT_TRY.pop(tid, None)
        for k in [k for k in RESUMED if k[0] == tid]: RESUMED.pop(k, None)
        log("uploaded", tid, status, len(files), "files via", via)
    finally:
        with LEDGER:
            DELIVERING.discard(tid)

def backoff(tid):
    prev = NEXT_TRY.get(tid, (0, 1))[1]
    step = min(prev * 2, MAX_BACKOFF)
    NEXT_TRY[tid] = (time.time() + step, step)

def closeout(tid):
    p = tdir(tid, "closeout.json")
    try:
        c = json.load(open(p))
    except (OSError, ValueError):
        return
    if time.time() < c.get("after", 0):
        return
    left = []
    for ep in [e for e in EPS if e.name in c.get("eps", [])]:
        if not ep.healthy():
            left.append(ep.name); continue
        try:
            ep.json("POST", f"/tasks/{qseg(tid)}/result",
                    {"status": c["status"], "summary": c["summary"], "files": []}, deadline=20, stall=20)
            log("closeout", tid, "on", ep.name)
        except urllib.error.HTTPError as e:
            log("closeout", tid, "on", ep.name, "HTTP", e.code, "- dropped")  # unknown/finished there
        except Exception as e:
            left.append(ep.name)
    if left:
        c["eps"] = left; c["after"] = time.time() + 30; _write(p, json.dumps(c))
    else:
        os.remove(p)

def sweep():
    """Schedule uploads of finished-but-unacked tasks; restart interrupted preparation;
    detect processes lost across reboots. Network only for tasks that need it."""
    for tid in sorted(os.listdir(TASKS)):
        d = tdir(tid)
        if os.path.exists(os.path.join(d, "closeout.json")):
            closeout(tid)
        if not os.path.isdir(d) or os.path.exists(os.path.join(d, "uploaded")) \
                or os.path.exists(os.path.join(d, "quarantine")):
            continue
        if not os.path.exists(os.path.join(d, "pid")):
            if os.path.exists(os.path.join(d, "claimed")):
                start_prep(tid)  # worker restarted while downloading attachments
            continue  # a pre-v2 dir without pid was never started; never run it late
        if tid in PROCS: PROCS[tid].poll()
        for ep in origins(tid):
            if not confirmed(tid, ep) and ep.healthy():
                confirm(ep, tid)
        ec_f = os.path.join(d, "exitcode")
        if os.path.exists(ec_f):
            ec = int(open(ec_f).read().strip() or 1)
        elif not alive(int(open(os.path.join(d, "pid")).read() or 0)):
            ec = -1; _write(ec_f, "-1")  # lost (e.g. reboot): report failed once
        else:
            continue
        if tid in DELIVERING or time.time() < NEXT_TRY.get(tid, (0, 1))[0] or len(DELIVERING) >= MAX_DELIVER:
            continue
        if ec == 0:
            status, summary = "done", last_line(os.path.join(d, "stdout.txt")) or "completed (no output)"
        else:
            status = "failed"
            summary = ("process lost (worker host restarted?)" if ec == -1 else f"exit {ec}: ") + \
                      (last_line(os.path.join(d, "stderr.txt")) or last_line(os.path.join(d, "stdout.txt")))
        with LEDGER:
            DELIVERING.add(tid)
        threading.Thread(target=deliver_task, args=(tid, status, summary, result_files(d)),
                         daemon=True, name="up-" + tid).start()

def poll_loop(ep):
    back = 1
    while True:
        try:
            st, _, data = ep.call("GET", f"/tasks/next?worker={qseg(WORKER)}&wait=50", deadline=95)
            back = 1
            if st == 200:
                task = json.loads(data)["task"]
                tid = task["id"]
                if claim(task, ep):
                    log("claimed", tid, task["tool"], "via", ep.name)
                    confirm(ep, tid); start_prep(tid)
                else:
                    log("duplicate claim", tid, "via", ep.name, "- already in ledger, not run again")
                    confirm(ep, tid)
        except Exception as e:
            if back == 1 or back >= MAX_BACKOFF:
                log("poll", ep.name, type(e).__name__, str(e)[:120], "retry in", back)
            time.sleep(back); back = min(back * 2, MAX_BACKOFF)

def main():
    log("worker", WORKER, "state", STATE, "channels", ", ".join(f"{e.name}={e.url}" for e in EPS))
    signal.signal(signal.SIGHUP, lambda *a: log("SIGHUP ignored: every channel is polled; nothing to reset"))
    for ep in EPS:
        threading.Thread(target=poll_loop, args=(ep,), daemon=True, name="poll-" + ep.name).start()
    while True:  # local scan of task dirs; network only when something finished
        try:
            sweep()
        except Exception as e:
            log("sweep error", type(e).__name__, str(e)[:200])
        time.sleep(1)

if __name__ == "__main__":
    main()
