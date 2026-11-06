#!/usr/bin/env python3
"""mac-relay server: tiny task queue between box agents and a Mac worker.
Stdlib only. Env: RELAY_SECRET_FILE (required), RELAY_DATA_DIR (default ./data),
RELAY_BIND (127.0.0.1), RELAY_PORT (18790). The secret is never logged."""
import hmac, json, os, re, select, socket, sqlite3, sys, threading, time
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

TOOLS = {"hermes", "deepseek", "codex"}
SAFE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
MAX_BODY = int(os.environ.get("RELAY_MAX_BODY", 512 * 1024 * 1024))
MAX_WAIT = 55
CLAIM_TTL = float(os.environ.get("RELAY_CLAIM_TTL", 20))  # unconfirmed claim -> redeliver

def log(*a):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), *a, file=sys.stderr, flush=True)

def safe_name(n):
    n = os.path.basename(n or "")
    return n if SAFE.match(n) else None

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
        # started_wake: NULL=never confirmed start; 0=pending started wake; 1=started wake sent
        cols = {r[1] for r in self.db.execute("pragma table_info(tasks)")}
        if "started_wake" not in cols:
            self.db.execute("alter table tasks add column started_wake integer")
        self.db.commit()
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)

    def fdir(self, tid, kind):
        p = os.path.join(self.data, "files", tid, kind)
        os.makedirs(p, exist_ok=True)
        return p

    def row(self, tid):
        cur = self.db.execute("select * from tasks where id=?", (tid,))
        r = cur.fetchone()
        return dict(zip([c[0] for c in cur.description], r)) if r else None

    def wait_claim(self, sql, args, upd, wait, alive=lambda: True):
        end = time.time() + wait
        with self.cond:
            while True:
                cur = self.db.execute(sql, args)
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
    """event='started' synthesizes the lightweight started wake payload; else normal result view."""
    v = {k: d[k] for k in ("id", "sender", "tool", "prompt", "state", "status", "summary", "created", "finished")} | {
        "attachments": json.loads(d["attachments"] or "[]"), "files": json.loads(d["files"] or "[]")}
    if event == "started":
        v["status"] = "started"
        v["summary"] = f"Mac 已接单（{d['tool']}）"
        v["files"] = []
        v["finished"] = None
    return v

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mac-relay"

    def log_message(self, fmt, *a):  # path + status only, never headers
        log(self.address_string(), fmt % a)

    def send(self, code, obj=None, raw=None, ctype="application/json"):
        body = raw if raw is not None else json.dumps(obj or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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

    def route(self):
        u = urlparse(self.path)
        return [unquote(p) for p in u.path.strip("/").split("/") if p], parse_qs(u.query)

    def do_GET(self):
        parts, q = self.route()
        if parts == ["health"]:
            return self.send(200, {"ok": True})
        if not self.authed():
            return
        st = self.server.store
        wait = min(float(q.get("wait", ["25"])[0]), MAX_WAIT)
        if parts == ["tasks", "next"]:
            worker = q.get("worker", ["mac"])[0]
            # A claim is only a lease: if the worker never confirms (its long-poll died while we
            # answered), the task is redelivered after CLAIM_TTL. Confirmed tasks are never redelivered.
            d = st.wait_claim("select * from tasks where state='queued' or (state='claimed' and started<?) "
                              "order by created limit 1", (time.time() - CLAIM_TTL,),
                              ("update tasks set state='claimed', worker=?, started=? where id=?",
                               lambda d: (worker, time.time(), d["id"])), wait, self.peer_alive)
            return self.send(200, {"task": task_view(d)}) if d else self.send(204, raw=b"")
        if parts == ["results", "next"]:
            # One wait: pending started wakes first, then undone done/failed deliveries.
            d = st.wait_claim(
                "select * from tasks where started_wake=0 or "
                "(state in ('done','failed') and delivered=0) "
                "order by case when started_wake=0 then 0 else 1 end, "
                "coalesce(started, finished, created) limit 1",
                (), None, wait, self.peer_alive)
            if not d:
                return self.send(204, raw=b"")
            ev = "started" if d.get("started_wake") == 0 else None
            return self.send(200, {"task": task_view(d, event=ev)})
        if len(parts) == 2 and parts[0] == "tasks":
            with st.lock:
                d = st.row(parts[1])
            return self.send(200, {"task": task_view(d)}) if d else self.send(404, {"error": "not found"})
        if len(parts) == 4 and parts[0] == "files" and parts[2] in ("in", "out"):
            tid, kind, name = parts[1], parts[2], safe_name(parts[3])
            if not SAFE.match(tid) or not name:
                return self.send(400, {"error": "bad name"})
            p = os.path.join(st.data, "files", tid, kind, name)
            if not os.path.isfile(p):
                return self.send(404, {"error": "not found"})
            with open(p, "rb") as f:
                return self.send(200, raw=f.read(), ctype="application/octet-stream")
        self.send(404, {"error": "no route"})

    def do_POST(self):
        parts, q = self.route()
        if not self.authed():
            return
        st = self.server.store
        try:
            raw = self.body()
        except ValueError as e:
            return self.send(413, {"error": str(e)})
        if parts == ["tasks"]:
            fields, files = parse_any(self.headers.get("Content-Type", ""), raw)
            tid, sender, tool = fields.get("id", ""), fields.get("sender", ""), fields.get("tool", "")
            if not SAFE.match(tid) or not SAFE.match(sender) or tool not in TOOLS or not fields.get("prompt"):
                return self.send(400, {"error": "need id, sender, tool in hermes|deepseek|codex, prompt"})
            with st.cond:
                if st.row(tid):  # idempotent
                    return self.send(200, {"id": tid, "duplicate": True})
                names = []
                for name, data in files:
                    n = safe_name(name)
                    if not n:
                        return self.send(400, {"error": "bad attachment name"})
                    with open(os.path.join(st.fdir(tid, "in"), n), "wb") as f:
                        f.write(data)
                    names.append(n)
                st.db.execute("insert into tasks(id,sender,tool,prompt,attachments,state,created) values(?,?,?,?,?,'queued',?)",
                              (tid, sender, tool, fields["prompt"], json.dumps(names), time.time()))
                st.db.commit(); st.cond.notify_all()
            log("enqueued", tid, sender, tool, len(names), "attachments")
            return self.send(201, {"id": tid})
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "result":
            tid = parts[1]
            fields, files = parse_any(self.headers.get("Content-Type", ""), raw)
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
                    n = safe_name(name)
                    if n:
                        with open(os.path.join(st.fdir(tid, "out"), n), "wb") as f:
                            f.write(data)
                        names.append(n)
                summary = " ".join((fields.get("summary") or "").split())[:300]
                st.db.execute("update tasks set state=?, status=?, summary=?, files=?, finished=? where id=?",
                              (status, status, summary, json.dumps(names), time.time(), tid))
                st.db.commit(); st.cond.notify_all()
            log("result", tid, status, len(names), "files")
            return self.send(200, {"id": tid})
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "started":
            with st.cond:
                # Mark started_wake=0 only once (NULL -> 0). Already-running/done tasks stay idempotent.
                st.db.execute("update tasks set state='running', started_wake=COALESCE(started_wake, 0) "
                              "where id=? and state in ('claimed','running')", (parts[1],))
                st.db.commit(); st.cond.notify_all()
            return self.send(200, {"id": parts[1]})
        if len(parts) == 3 and parts[0] == "results" and parts[2] == "ack":
            # Optional {"event":"started"} acks the started wake only; default/empty acks done/failed (compat).
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
    srv = ThreadingHTTPServer((os.environ.get("RELAY_BIND", "127.0.0.1"), int(os.environ.get("RELAY_PORT", "18790"))), H)
    srv.daemon_threads = True
    srv.secret = secret
    srv.store = Store(os.environ.get("RELAY_DATA_DIR", "./data"))
    log("mac-relay listening on", *srv.server_address[:2])
    srv.serve_forever()

if __name__ == "__main__":
    main()
