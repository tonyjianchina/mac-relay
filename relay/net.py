# --- BEGIN mac-relay net (keep identical to the copy in mac/relay-worker.py; tests check) ---
# Stdlib HTTP for mac-relay clients: hard deadlines, stall detection, chunked resumable
# upload with sha256, resumable verified download. Shared by box tools and the Mac worker.
import hashlib, http.client, json, os, select, socket, ssl, threading, time, urllib.error, urllib.parse, urllib.request

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


def _note_early(conn, exc):
    """If the server answered and closed while the body was still uploading, keep that
    response on the OSError. urllib otherwise turns the reset into URLError and drops it."""
    try:
        sock = conn.sock
        if sock is None:
            return
        sock.settimeout(1)
        resp = conn.getresponse()
        body = resp.read(65536)
        if int(resp.status) < 400:
            return
        exc.early_status = int(resp.status)
        exc.early_reason = resp.reason if isinstance(resp.reason, str) else ""
        exc.early_body = bytes(body or b"")
    except Exception:
        return


def _early_response(exc):
    """(status, reason, body) stashed by _note_early, walking URLError.reason / __context__."""
    cur, seen = exc, set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        status = getattr(cur, "early_status", None)
        if isinstance(status, int):
            body = getattr(cur, "early_body", b"") or b""
            reason = getattr(cur, "early_reason", "") or ""
            return status, str(reason), body
        nxt = getattr(cur, "reason", None)
        if not isinstance(nxt, BaseException):
            nxt = getattr(cur, "__context__", None)
        if not isinstance(nxt, BaseException) or nxt is cur:
            return None
        cur = nxt
    return None


class _PeerAnswered(Exception):
    """The peer sent an HTTP error before the request body finished."""
    def __init__(self, status, reason, body):
        super().__init__(status)
        self.early_status, self.early_reason, self.early_body = status, reason, body


class _EarlyBody:
    """Send a request body without deadlocking if the peer answers first.

    A full 8MiB chunk does not fit in a typical TCP window. If the server or a
    proxy replies (409 busy, 400 short body) and stops reading, a blocking
    send waits until the stall watchdog. Polling for a readable socket lets
    the HTTP error be handled, and the chunk retried, instead.
    """
    def _read_answered(self):
        sock = self.sock
        if sock is None:
            return None
        try:
            sock.settimeout(1)
            resp = self.getresponse()
            body = resp.read(65536)
        except Exception:
            return None
        if int(resp.status) < 400:
            return None
        reason = resp.reason if isinstance(resp.reason, str) else ""
        return int(resp.status), reason, bytes(body or b"")

    def _send_body(self, body):
        sock = self.sock
        if sock is None:
            raise ConnectionError("no socket")
        prev = sock.gettimeout()
        sock.setblocking(False)
        try:
            pending = b""
            while True:
                if not pending:
                    pending = body.read(self.blocksize)
                    if not pending:
                        return
                try:
                    n = sock.send(pending)
                except ssl.SSLWantReadError:
                    got = self._read_answered()
                    if got:
                        raise _PeerAnswered(*got)
                    continue
                except ssl.SSLWantWriteError:
                    readable, _ = _poll_send(sock)
                    if readable:
                        got = self._read_answered()
                        if got:
                            raise _PeerAnswered(*got)
                    continue
                except BlockingIOError:
                    readable, _ = _poll_send(sock)
                    if readable:
                        got = self._read_answered()
                        if got:
                            raise _PeerAnswered(*got)
                    continue
                if not n:
                    raise ConnectionError("upload socket closed")
                pending = pending[n:]
        finally:
            try:
                sock.settimeout(prev)
            except OSError:
                pass

    def _send_output(self, message_body=None, encode_chunked=False):
        try:
            # File bodies are polled so an early 4xx is not stuck behind an 8MiB send.
            if message_body is not None and hasattr(message_body, "read") and not encode_chunked:
                super()._send_output(None)
                self._send_body(message_body)
                return
            super()._send_output(message_body, encode_chunked=encode_chunked)
        except _PeerAnswered as e:
            err = ConnectionError("peer answered before the upload body finished")
            err.early_status, err.early_reason, err.early_body = e.early_status, e.early_reason, e.early_body
            raise err from e
        except OSError as e:
            _note_early(self, e)
            raise


def _poll_send(sock):
    """(readable, writable) with a short wait so a stall watchdog can still run."""
    r, w, _ = select.select([sock], [sock], [], 0.25)
    return sock in r, sock in w


class _HTTPConn(_EarlyBody, http.client.HTTPConnection):
    pass


class _HTTPSConn(_EarlyBody, http.client.HTTPSConnection):
    pass


class _HTTP(urllib.request.HTTPHandler):
    def __init__(self, reg, src=None):
        super().__init__(); self.reg, self.src = reg, src

    def http_open(self, req):
        def mk(host, **kw):
            if self.src:
                kw["source_address"] = (self.src, 0)
            c = _HTTPConn(host, **kw); self.reg.conns.append(c); return c
        return self.do_open(mk, req)


class _HTTPS(urllib.request.HTTPSHandler):
    def __init__(self, reg):
        super().__init__(); self.reg = reg

    def https_open(self, req):
        def mk(host, **kw):
            c = _HTTPSConn(host, **kw); self.reg.conns.append(c); return c
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
            early = _early_response(e)
            if early:
                status, reason, raw = early
                err = urllib.error.HTTPError(r.full_url, status, reason or "error", None, None)
                err.relay_body = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
                (self._fail(err) if status >= 500 else self._ok())
                raise err from e
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


# How many times a 400 short body may come back without the resume point moving.
# Progress (partial > offset just sent) is bounded by the file size, since the offset only grows.
_SHORT_STALLS = 2
# 409 busy means the previous request still holds the upload. Retry a few times, then
# give up so the caller can use another channel instead of looping forever.
_BUSY_TRIES = 4


def _short_partial(err, size, sent):
    """Resume offset for a validated 400 {"error":"short body","partial":N}, else None.

    N must be an int (not a bool) and sent <= N <= size. A missing/malformed partial, a
    different error (bad name, bad offset/total), or N < sent is not this case: the caller
    raises and leaves fatal 400 handling unchanged. N == sent is valid but made no progress.
    """
    if getattr(err, "code", None) != 400:
        return None
    try:
        j = json.loads(getattr(err, "relay_body", "") or "")
    except ValueError:
        return None
    if not isinstance(j, dict) or j.get("error") != "short body" or "partial" not in j:
        return None
    p = j.get("partial")
    if type(p) is not int or p < sent or p > size:
        return None
    return p


def upload_file(ep, tid, kind, path, name, chunk=CHUNK_BYTES, stall=60, log=None):
    """Upload one file as `name` (may contain '/' on new servers). New servers: chunked PUT
    with offset; resumes from the server's partial size; the server checks sha256 before the
    file becomes visible. A 400 short body whose partial is in range resumes in this attempt.
    Old servers: one streaming PUT. Returns {"name","size","sha256"}."""
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
    stalls = 0  # consecutive short bodies that did not advance
    busy = 0
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
                    stalls = 0
                    if j.get("busy"):
                        busy += 1
                        if busy > _BUSY_TRIES:
                            raise ConnectionError(f"{ep.name}: upload still busy") from e
                        time.sleep(0.5)
                    else:
                        busy = 0
                    continue
                p = _short_partial(e, size, off)
                if p is not None and p > off:
                    off, reset, stalls = p, False, 0
                    log("resume", tid, name, f"{off}/{size}", "on", ep.name)
                    continue
                if p is not None:
                    stalls += 1
                    if stalls <= _SHORT_STALLS:
                        continue
                if e.code == 422:
                    raise Mismatch(f"{name}: server sha256 mismatch") from e
                raise
            reset = False
            stalls = 0
            busy = 0
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
