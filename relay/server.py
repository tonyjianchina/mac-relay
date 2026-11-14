#!/usr/bin/env python3
"""mac-relay server: tiny task queue between box agents and a Mac worker.
Stdlib only. Env: RELAY_SECRET_FILE (required), RELAY_DATA_DIR (default ./data),
RELAY_BIND (127.0.0.1), RELAY_PORT (18790), RELAY_MAX_BODY (512MiB hard),
RELAY_MAX_SOFT_BODY (16MiB multipart soft limit). The secret is never logged.

Large result files: PUT /tasks/{id}/files/{name} (octet-stream, streamed to disk),
then POST /tasks/{id}/result as application/json with status/summary/files[].
Multipart result bodies over the soft limit are rejected (413).

Protocol v2 (GET /status lists the features; old clients keep working unchanged):
chunked PUT with ?offset=&total=&sha256= (resumable, verified before the file appears),
GET /uploads/{id}/{name}?kind= (resume point), Range downloads, staged enqueue for large
attachments (POST /tasks {"staged":true} -> PUT kind=in -> POST /tasks/{id}/commit),
POST /tasks/{id}/withdraw (queued only), mirror rows for results returned over another
channel, /results/next?exclude=, and nested result paths (a/b.txt)."""
import hashlib, hmac, json, os, re, select, socket, sqlite3, sys, threading, time
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

TOOLS = {"hermes", "deepseek", "codex", "schwab", "selftest"}
CHUNK_STALL = float(os.environ.get("RELAY_CHUNK_STALL_S", "30"))
FEATURES = ["status", "chunked", "range", "staged", "withdraw", "mirror", "exclude", "relpath"]
# Task ids and sender slugs. Do not loosen this for file names.
SAFE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
# One path segment. Unicode (including CJK) is allowed. 128 characters matches the
# old ASCII cap; 247 UTF-8 bytes leaves room for the ".partial" temp suffix (NAME_MAX 255).
_NAME_MAX_CHARS = 128
_NAME_MAX_BYTES = 255 - len(".partial")
MAX_BODY = int(os.environ.get("RELAY_MAX_BODY", 512 * 1024 * 1024))
MAX_SOFT_BODY = int(os.environ.get("RELAY_MAX_SOFT_BODY", 16 * 1024 * 1024))
CHUNK = 1024 * 1024
MAX_WAIT = 55
CLAIM_TTL = float(os.environ.get("RELAY_CLAIM_TTL", 20))  # unconfirmed claim -> redeliver

def log(*a):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), *a, file=sys.stderr, flush=True)

def safe_name(n):
    """Attachment or result file name. Unicode, including CJK, is allowed.

    Rejects path traversal and names that cannot be stored as a single segment:
    empty, ".", "..", "/", "\\", NUL and other controls, over 128 characters,
    or over 247 UTF-8 bytes. Task ids stay on SAFE (ASCII only).
    """
    if not isinstance(n, str) or n in ("", ".", ".."):
        return None
    if "/" in n or "\\" in n or "\x00" in n:
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in n):
        return None
    try:
        raw = n.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(n) > _NAME_MAX_CHARS or len(raw) > _NAME_MAX_BYTES:
        return None
    return n

def safe_rel(n):
    """Relative path of one or more safe_name segments joined by '/' (max 16 deep)."""
    if not isinstance(n, str) or not n or n.startswith("/") or len(n) > 1024:
        return None
    segs = n.split("/")
    if len(segs) > 16 or not all(safe_name(x) for x in segs):
        return None
    return n

def sha256_path(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()

class Store:
    def __init__(self, data):
        self.data = data
        os.makedirs(os.path.join(data, "files"), exist_ok=True)
        self.db = sqlite3.connect(os.path.join(data, "relay.db"), check_same_thread=False)
        self.db.execute("pragma journal_mode=wal")
        self.db.execute("""create table if not exists tasks(
            id text primary key, sender text, tool text, prompt text, attachments text,
            state text, worker text, created real, started real, finished real,
            status text, summary text, files text, delivered integer default 0)""")
        # started_wake: NULL=not confirmed; 1=accepted and not a wake.
        # Legacy 0 meant "pending started wake". Rewrite it so those rows are not pending.
        cols = {r[1] for r in self.db.execute("pragma table_info(tasks)")}
        if "started_wake" not in cols:
            self.db.execute("alter table tasks add column started_wake integer")
        if "meta" not in cols:  # JSON: attachments_meta / files_meta [{name,size,sha256}]
            self.db.execute("alter table tasks add column meta text")
        self.db.execute("update tasks set started_wake=1 where started_wake=0")
        self.db.commit()
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.workers = {}  # name -> {"last": ts, "active": open long-polls}
        self.flocks, self.flocks_guard = {}, threading.Lock()

    def fdir(self, tid, kind):
        p = os.path.join(self.data, "files", tid, kind)
        os.makedirs(p, exist_ok=True)
        return p

    def fpath(self, tid, kind, rel):
        p = os.path.join(self.fdir(tid, kind), *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    def meta_path(self, tid, kind, rel):
        p = os.path.join(self.data, "files", tid, kind + ".meta", *rel.split("/")) + ".json"
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    def file_meta(self, tid, kind, rel):
        """{"name","size","sha256"} for a complete file; sha256 cached in a sidecar."""
        p = os.path.join(self.data, "files", tid, kind, *rel.split("/"))
        if not os.path.isfile(p):
            return None
        mp, size = self.meta_path(tid, kind, rel), os.path.getsize(p)
        try:
            m = json.load(open(mp))
            if m.get("size") == size and m.get("mtime") == os.path.getmtime(p):
                return {"name": rel, "size": size, "sha256": m["sha256"]}
        except (OSError, ValueError, KeyError):
            pass
        m = {"size": size, "mtime": os.path.getmtime(p), "sha256": sha256_path(p)}
        json.dump(m, open(mp, "w"))
        return {"name": rel, "size": size, "sha256": m["sha256"]}

    def flock(self, key):
        with self.flocks_guard:
            return self.flocks.setdefault(key, threading.Lock())

    def seen(self, worker, delta):
        with self.lock:
            w = self.workers.setdefault(worker, {"last": 0.0, "active": 0})
            w["active"] += delta; w["last"] = time.time()

    def row(self, tid):
        cur = self.db.execute("select * from tasks where id=?", (tid,))
        r = cur.fetchone()
        return dict(zip([c[0] for c in cur.description], r)) if r else None

    def wait_claim(self, sql, args, upd, wait, alive=lambda: True):
        end = time.time() + wait
        with self.cond:
            while True:
                cur = self.db.execute(sql, args() if callable(args) else args)  # re-evaluated each pass
                r = cur.fetchone()
                if r and not alive():
                    return None  # client hung up during the long-poll: do not hand it a task
                if r:
                    d = dict(zip([c[0] for c in cur.description], r))
                    if upd:
                        self.db.execute(upd[0], upd[1](d)); self.db.commit()
                    return d
                left = end - time.time()
                if left <= 0:
                    return None
                self.cond.wait(min(left, 2))

def task_view(d, event=None):
    """Task JSON. event='started' builds the old lightweight payload; /results/next does not send it."""
    meta = json.loads(d.get("meta") or "{}")
    v = {k: d[k] for k in ("id", "sender", "tool", "prompt", "state", "status", "summary", "created", "finished")} | {
        "attachments": json.loads(d["attachments"] or "[]"), "files": json.loads(d["files"] or "[]"),
        "attachments_meta": meta.get("attachments_meta") or [], "files_meta": meta.get("files_meta") or []}
    if event == "started":
        v["status"] = "started"
        v["summary"] = f"Mac 已接单（{d['tool']}）"
        v["files"] = []
        v["finished"] = None
    return v

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mac-relay"
    timeout = float(os.environ.get("RELAY_SOCKET_TIMEOUT", 120))  # a stalled upload frees its lock

    def log_message(self, fmt, *a):  # path + status only, never headers
        log(self.address_string(), fmt % a)

    def send(self, code, obj=None, raw=None, ctype="application/json"):
        body = raw if raw is not None else json.dumps(obj or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if getattr(self, "close_connection", 0):
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path):
        size, start = os.path.getsize(path), 0
        m = re.match(r"^bytes=(\d+)-$", self.headers.get("Range", "").strip())
        if m:
            start = int(m.group(1))
            if start >= size:
                return self.send(416, {"error": "range", "size": size})
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
        else:
            self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size - start))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def authed(self):
        h = self.headers.get("Authorization", "")
        ok = h.startswith("Bearer ") and hmac.compare_digest(h[7:].strip().encode(), self.server.secret)
        if not ok:
            self.send(401, {"error": "unauthorized"})
        return ok

    def peer_alive(self):
        try:
            if select.select([self.connection], [], [], 0)[0]:
                return self.connection.recv(1, socket.MSG_PEEK) != b""
            return True
        except OSError:
            return False

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("body too large")
        return self.rfile.read(n)

    def stream_to_file(self, path, n, h=None):
        """Read exactly n bytes from the request into path (via .partial + replace)."""
        tmp = path + ".partial"
        left = n
        try:
            with open(tmp, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(CHUNK, left))
                    if not chunk:
                        raise ValueError("short body")
                    f.write(chunk)
                    if h:
                        h.update(chunk)
                    left -= len(chunk)
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def route(self):
        u = urlparse(self.path)
        return [unquote(p) for p in u.path.strip("/").split("/") if p], parse_qs(u.query)

    def do_GET(self):
        parts, q = self.route()
        if parts == ["health"]:
            return self.send(200, {"ok": True, "soft_body": MAX_SOFT_BODY, "max_body": MAX_BODY})
        if not self.authed():
            return
        st = self.server.store
        wait = min(float(q.get("wait", ["25"])[0]), MAX_WAIT)
        if parts == ["status"]:
            now = time.time()
            with st.lock:
                workers = {k: (0.0 if w["active"] > 0 else round(now - w["last"], 1)) for k, w in st.workers.items()}
            return self.send(200, {"ok": True, "version": 2, "features": FEATURES, "workers": workers,
                                   "soft_body": MAX_SOFT_BODY, "max_body": MAX_BODY})
        if len(parts) == 3 and parts[0] == "uploads":
            tid, rel, kind = parts[1], safe_rel(parts[2]), (q.get("kind", ["out"])[0] or "out")
            if kind not in ("in", "out") or not SAFE.match(tid) or not rel:
                return self.send(400, {"error": "bad name"})
            dest = os.path.join(st.data, "files", tid, kind, *rel.split("/"))
            part = dest + ".upload"
            m = st.file_meta(tid, kind, rel) if os.path.isfile(dest) else None
            return self.send(200, {"partial": os.path.getsize(part) if os.path.isfile(part) else 0,
                                   "complete": bool(m), "size": m and m["size"], "sha256": m and m["sha256"]})
        if parts == ["tasks", "next"]:
            worker = q.get("worker", ["mac"])[0]
            # A claim is only a lease: if the worker never confirms (its long-poll died while we
            # answered), the task is redelivered after CLAIM_TTL. Confirmed tasks are never redelivered.
            st.seen(worker, 1)
            try:
                d = st.wait_claim("select * from tasks where state='queued' or (state='claimed' and started<?) "
                                  "order by created limit 1", lambda: (time.time() - CLAIM_TTL,),
                                  ("update tasks set state='claimed', worker=?, started=? where id=?",
                                   lambda d: (worker, time.time(), d["id"])), wait, self.peer_alive)
            finally:
                st.seen(worker, -1)
            return self.send(200, {"task": task_view(d)}) if d else self.send(204, raw=b"")
        if parts == ["results", "next"]:
            # Done/failed only. Acceptance is GET /tasks/{id} (state=running); it is not a wake.
            # Legacy started_wake=0 is not selected, so it cannot sit ahead of delivered=0.
            # exclude= lets a consumer skip results it is still downloading.
            ex = [x for x in ",".join(q.get("exclude", [])).split(",") if SAFE.match(x)][:64]
            d = st.wait_claim(
                "select * from tasks where state in ('done','failed') and delivered=0 "
                + (f"and id not in ({','.join('?' * len(ex))}) " if ex else "")
                + "order by coalesce(finished, created) limit 1",
                tuple(ex), None, wait, self.peer_alive)
            if not d:
                return self.send(204, raw=b"")
            return self.send(200, {"task": task_view(d)})
        if len(parts) == 2 and parts[0] == "tasks":
            with st.lock:
                d = st.row(parts[1])
            return self.send(200, {"task": task_view(d)}) if d else self.send(404, {"error": "not found"})
        if parts and parts[0] == "files" and len(parts) >= 3 and parts[2] in ("in", "out"):
            # Missing, empty, or multi-segment names (../evil) are 400, not 404.
            tid, kind = parts[1], parts[2]
            name = safe_rel(parts[3]) if len(parts) == 4 else None
            if not SAFE.match(tid) or not name:
                return self.send(400, {"error": "bad name"})
            p = os.path.join(st.data, "files", tid, kind, *name.split("/"))
            if not os.path.isfile(p):
                return self.send(404, {"error": "not found"})
            return self.send_file(p)
        self.send(404, {"error": "no route"})

    def do_PUT(self):
        parts, q = self.route()
        if not self.authed():
            return
        # PUT /tasks/{tid}/files/{name}?kind=out|in  — stream octet-stream to disk
        if parts and parts[0] == "tasks" and len(parts) >= 3 and parts[2] == "files":
            # One segment only. ../evil, an empty name, or a non-ASCII task id is 400.
            tid = parts[1]
            name = safe_rel(parts[3]) if len(parts) == 4 else None
            kind = (q.get("kind", ["out"])[0] or "out")
            if kind not in ("in", "out") or not SAFE.match(tid) or not name:
                return self.send(400, {"error": "bad name"})
            n = int(self.headers.get("Content-Length") or 0)
            chunked = "offset" in q
            if n <= 0 and not chunked:
                return self.send(400, {"error": "Content-Length required"})
            if n > MAX_BODY:
                return self.send(413, {"error": "body too large"})
            st = self.server.store
            with st.lock:
                if not st.row(tid):
                    return self.send(404, {"error": "unknown task"})
            dest = st.fpath(tid, kind, name)
            if chunked:
                return self.put_chunk(st, tid, kind, name, dest, n, q)
            h = hashlib.sha256()
            try:
                self.stream_to_file(dest, n, h)
            except ValueError as e:
                return self.send(400, {"error": str(e)})
            json.dump({"size": n, "mtime": os.path.getmtime(dest), "sha256": h.hexdigest()},
                      open(st.meta_path(tid, kind, name), "w"))
            log("put-file", tid, kind, name, n)
            return self.send(200, {"id": tid, "name": name, "bytes": n, "kind": kind})
        self.send(404, {"error": "no route"})

    def put_chunk(self, st, tid, kind, name, dest, n, q):
        """Append n bytes at ?offset= to dest.upload. offset must equal the bytes already held
        (else 409 with the resume point). Bytes that arrived before a disconnect are kept. When
        offset+n == total the sha256 (if given) is checked and the file is renamed into place;
        a mismatch deletes the partial and answers 422."""
        try:
            off, total = int(q["offset"][0]), int(q.get("total", ["-1"])[0])
        except (ValueError, KeyError):
            return self.send(400, {"error": "bad offset/total"})
        want = (q.get("sha256", [""])[0] or "").lower()
        if total < 0 or total > MAX_BODY or off < 0 or off + n > total:
            return self.send(400, {"error": "bad offset/total"})
        part = dest + ".upload"
        lk = st.flock(part)
        if not lk.acquire(blocking=False):
            self.close_connection = True
            return self.send(409, {"busy": True, "partial": os.path.getsize(part) if os.path.exists(part) else 0})
        try:
            if q.get("reset", [""])[0] == "1" and off == 0 and os.path.exists(part):
                os.remove(part)
            cur = os.path.getsize(part) if os.path.exists(part) else 0
            if off != cur:
                self.close_connection = True  # the request body was not read
                return self.send(409, {"partial": cur})
            left = n
            # A stalled sender must not hold the lock for the full socket timeout: its resume
            # retry would get 409 busy. Bytes already written stay (resume point).
            self.connection.settimeout(CHUNK_STALL)
            with open(part, "ab") as f:
                while left > 0:
                    try:
                        b = self.rfile.read(min(CHUNK, left))
                    except OSError:  # includes the stall timeout
                        b = b""
                    if not b:
                        break
                    f.write(b); left -= len(b)
            cur = os.path.getsize(part)
            if left:
                self.close_connection = True
                return self.send(400, {"error": "short body", "partial": cur})
            if cur < total:
                return self.send(200, {"partial": cur, "complete": False})
            got = sha256_path(part)
            if want and got != want:
                os.remove(part)
                log("put-chunk sha256 mismatch", tid, kind, name)
                return self.send(422, {"error": "sha256 mismatch", "partial": 0})
            os.replace(part, dest)
            json.dump({"size": cur, "mtime": os.path.getmtime(dest), "sha256": got},
                      open(st.meta_path(tid, kind, name), "w"))
            log("put-file", tid, kind, name, cur, "chunked sha256 ok" if want else "chunked")
            return self.send(200, {"partial": cur, "complete": True, "sha256": got})
        finally:
            lk.release()

    def do_POST(self):
        parts, q = self.route()
        if not self.authed():
            return
        st = self.server.store
        ctype = self.headers.get("Content-Type", "")
        n = int(self.headers.get("Content-Length") or 0)
        # Soft-limit multipart so email parser cannot balloon host memory (see 2026-11-07 OOM).
        if ctype.startswith("multipart/") and n > MAX_SOFT_BODY:
            self.close_connection = True
            return self.send(413, {
                "error": f"multipart over soft limit {MAX_SOFT_BODY}; "
                         "PUT /tasks/{id}/files/{name} then POST JSON result"})
        try:
            raw = self.body()
        except ValueError as e:
            return self.send(413, {"error": str(e)})
        if parts == ["tasks"]:
            fields, files = parse_any(ctype, raw)
            tid, sender, tool = fields.get("id", ""), fields.get("sender", ""), fields.get("tool", "")
            if not SAFE.match(tid) or not SAFE.match(sender) or tool not in TOOLS or not fields.get("prompt"):
                return self.send(400, {"error": "need id, sender, tool in hermes|deepseek|codex|schwab, prompt"})
            staged, mirror = fields.get("staged") in ("true", "1"), fields.get("mirror") in ("true", "1")
            ameta = []
            if staged:
                try:
                    for a in json.loads(fields.get("attachments") or "[]"):
                        nm = safe_rel(a.get("name") if isinstance(a, dict) else a)
                        if not nm:
                            return self.send(400, {"error": "bad attachment name"})
                        ameta.append({"name": nm, "size": a.get("size"), "sha256": a.get("sha256")} if isinstance(a, dict) else {"name": nm})
                except (ValueError, AttributeError):
                    return self.send(400, {"error": "bad attachments"})
            with st.cond:
                old = st.row(tid)
                if old:  # idempotent
                    return self.send(200, {"id": tid, "duplicate": True, "state": old["state"]})
                names = []
                for name, data in files:
                    nme = safe_name(name)
                    if not nme:
                        return self.send(400, {"error": "bad attachment name"})
                    with open(os.path.join(st.fdir(tid, "in"), nme), "wb") as f:
                        f.write(data)
                    names.append(nme)
                names += [a["name"] for a in ameta]
                # staging: waits for PUT kind=in + /commit. mirror: a result returned over this
                # channel for a task the worker claimed elsewhere; never handed to a worker.
                state = "staging" if staged else "running" if mirror else "queued"
                st.db.execute("insert into tasks(id,sender,tool,prompt,attachments,state,created,meta,worker,started_wake) "
                              "values(?,?,?,?,?,?,?,?,?,?)",
                              (tid, sender, tool, fields["prompt"], json.dumps(names), state, time.time(),
                               json.dumps({"attachments_meta": ameta}) if ameta else None,
                               fields.get("worker") if mirror else None, 1 if mirror else None))
                st.db.commit(); st.cond.notify_all()
            log("enqueued", tid, sender, tool, len(names), "attachments", state)
            return self.send(201, {"id": tid, "state": state})
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] in ("commit", "withdraw"):
            tid = parts[1]
            with st.cond:
                d = st.row(tid)
                if not d:
                    return self.send(404, {"error": "unknown task"})
                if parts[2] == "withdraw":
                    # Only a queued task can be withdrawn: nothing has been handed to a worker.
                    ok = d["state"] in ("queued", "staging")
                    if ok:
                        st.db.execute("update tasks set state='withdrawn' where id=?", (tid,)); st.db.commit()
                    log("withdraw", tid, "ok" if ok else "refused:" + d["state"])
                    return self.send(200, {"id": tid, "withdrawn": ok, "state": "withdrawn" if ok else d["state"]})
                if d["state"] != "staging":
                    return self.send(200, {"id": tid, "state": d["state"]})
                meta = json.loads(d.get("meta") or "{}").get("attachments_meta") or []
                bad = []
                for a in meta:
                    m = st.file_meta(tid, "in", a["name"])
                    if not m or (a.get("size") is not None and m["size"] != a["size"]) \
                            or (a.get("sha256") and m["sha256"] != a["sha256"]):
                        bad.append(a["name"])
                if bad:
                    return self.send(409, {"error": "attachments incomplete", "missing": bad})
                st.db.execute("update tasks set state='queued' where id=?", (tid,))
                st.db.commit(); st.cond.notify_all()
            log("committed", tid, len(meta), "attachments")
            return self.send(200, {"id": tid, "state": "queued"})
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "result":
            tid = parts[1]
            fields, files = parse_any(ctype, raw)
            status = fields.get("status")
            if status not in ("done", "failed"):
                return self.send(400, {"error": "status must be done|failed"})
            with st.cond:
                d = st.row(tid)
                if not d:
                    return self.send(404, {"error": "unknown task"})
                if d["state"] in ("done", "failed"):  # single-shot: first result wins
                    return self.send(200, {"id": tid, "duplicate": True})
                names = []
                for name, data in files:
                    nme = safe_name(name)
                    if nme:
                        with open(os.path.join(st.fdir(tid, "out"), nme), "wb") as f:
                            f.write(data)
                        names.append(nme)
                outdir = st.fdir(tid, "out")
                # Files pre-uploaded via PUT /tasks/{id}/files/{name}
                listed = fields.get("files")
                if listed:
                    try:
                        arr = json.loads(listed) if isinstance(listed, str) else listed
                    except Exception:
                        arr = []
                    if isinstance(arr, list):
                        for item in arr:
                            nme = safe_rel(item if isinstance(item, str) else "")
                            if nme and os.path.isfile(os.path.join(outdir, *nme.split("/"))) and nme not in names:
                                names.append(nme)
                summary = " ".join((fields.get("summary") or "").split())[:300]
                meta = json.loads(d.get("meta") or "{}")
                meta["files_meta"] = [m for m in (st.file_meta(tid, "out", n) for n in names) if m]
                st.db.execute("update tasks set state=?, status=?, summary=?, files=?, finished=?, meta=? where id=?",
                              (status, status, summary, json.dumps(names), time.time(), json.dumps(meta), tid))
                st.db.commit(); st.cond.notify_all()
            log("result", tid, status, len(names), "files")
            return self.send(200, {"id": tid})
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "started":
            with st.cond:
                # Record acceptance (queryable state=running) and mark it handled (started_wake=1).
                # Do not leave started_wake=0: that legacy value was a pending started wake.
                # Already done/failed rows stay as they are, so a fast finish is not rewound.
                st.db.execute("update tasks set state='running', started_wake=1 "
                              "where id=? and state in ('claimed','running')", (parts[1],))
                st.db.commit(); st.cond.notify_all()
            return self.send(200, {"id": parts[1]})
        if len(parts) == 3 and parts[0] == "results" and parts[2] == "ack":
            # {"event":"started"} only retires a legacy started_wake=0. It does not set delivered,
            # so a later done/failed result is still handed out. Default/empty acks done/failed.
            try:
                ev = (json.loads(raw or b"{}") or {}).get("event") if raw else None
            except Exception:
                ev = None
            with st.cond:
                if ev == "started":
                    st.db.execute("update tasks set started_wake=1 where id=? and started_wake=0", (parts[1],))
                else:
                    st.db.execute("update tasks set delivered=1 where id=?", (parts[1],))
                st.db.commit()
            return self.send(200, {"id": parts[1]})
        self.send(404, {"error": "no route"})

def parse_any(ctype, raw):
    if ctype.startswith("application/json"):
        j = json.loads(raw or b"{}")
        return {k: (v if isinstance(v, str) else json.dumps(v)) for k, v in j.items()}, []
    if ctype.startswith("multipart/form-data"):
        msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw)
        fields, files = {}, []
        for part in msg.iter_parts():
            name, fn = part.get_param("name", header="content-disposition"), part.get_filename()
            data = part.get_payload(decode=True) or b""
            if fn:
                files.append((fn, data))
            elif name:
                fields[name] = data.decode("utf-8", "replace")
        return fields, files
    return {}, []

def main():
    sf = os.environ.get("RELAY_SECRET_FILE")
    if not sf:
        sys.exit("RELAY_SECRET_FILE is required")
    with open(sf, "rb") as f:
        secret = f.read().strip()
    if len(secret) < 16:
        sys.exit("secret too short (need >=16 bytes)")
    # Comma-separated RELAY_BIND lets the box listen on localhost (box-agent) and the
    # Tailscale address (Mac worker) without binding 0.0.0.0 on a public/LAN NIC.
    binds = [b.strip() for b in os.environ.get("RELAY_BIND", "127.0.0.1").split(",") if b.strip()]
    port = int(os.environ.get("RELAY_PORT", "18790"))
    store = Store(os.environ.get("RELAY_DATA_DIR", "./data"))
    servers = []
    for b in binds:
        srv = ThreadingHTTPServer((b, port), H)
        srv.daemon_threads = True
        srv.secret = secret
        srv.store = store
        servers.append(srv)
        log("mac-relay listening on", *srv.server_address[:2], "soft_body", MAX_SOFT_BODY)
    if not servers:
        sys.exit("RELAY_BIND is empty")
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True, name=f"relay-{srv.server_address[0]}").start()
    servers[0].serve_forever()

if __name__ == "__main__":
    main()
