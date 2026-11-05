"""Minimal stdlib HTTP client helpers shared by box tools (the Mac worker embeds its own copy)."""
import json, os, uuid, urllib.request, urllib.error

def secret(path=None):
    with open(path or os.environ["RELAY_SECRET_FILE"], "rb") as f:
        return f.read().strip().decode()

def multipart(fields, files):
    b = uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out += [f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n".encode(), str(v).encode(), b"\r\n"]
    for path in files:
        name = os.path.basename(path)
        with open(path, "rb") as f:
            data = f.read()
        out += [f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n".encode(), data, b"\r\n"]
    out.append(f"--{b}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={b}"

def request(base, sec, method, path, body=None, ctype=None, timeout=70):
    req = urllib.request.Request(base.rstrip("/") + path, data=body, method=method)
    req.add_header("Authorization", "Bearer " + sec)
    if ctype:
        req.add_header("Content-Type", ctype)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        return r.status, (json.loads(data) if data and r.headers.get_content_type() == "application/json" else data)

class Failover:
    """Primary/fallback selection for box tools; same policy as the Mac worker's Conn.
    Switch to fallback only after `fail_threshold` consecutive primary failures (connection
    error or HTTP 5xx); stay there (sticky) until a primary /health probe succeeds, probed at
    most once per `cooldown_s`. Fallback requests never use a proxy (tailnet address)."""
    def __init__(self, primary, fallback=None, fail_threshold=3, cooldown_s=300, log=None):
        import threading, time
        self.primary, self.fallback = primary, fallback or None
        self.threshold, self.cooldown = int(fail_threshold), float(cooldown_s)
        self.mode, self.since, self.fails, self.last_probe = "primary", 0.0, 0, 0.0
        self.lock, self._time = threading.Lock(), time.time
        self.log = log or (lambda *a: None)
        self.fb_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.pr_opener = urllib.request.build_opener()

    @classmethod
    def from_env(cls, log=None):
        e = os.environ
        return cls(e["RELAY_URL"], e.get("RELAY_FALLBACK_URL"), e.get("RELAY_FAIL_THRESHOLD", 3),
                   e.get("RELAY_COOLDOWN_S", 300), log)

    def set(self, mode):
        if mode != self.mode:
            self.log("connection ->", mode); self.mode, self.since, self.fails = mode, self._time(), 0

    def base(self):
        with self.lock:
            now = self._time()
            if self.mode == "fallback" and now - self.since >= self.cooldown and now - self.last_probe >= self.cooldown:
                self.last_probe = now
                try:
                    with self.pr_opener.open(self.primary.rstrip("/") + "/health", timeout=5) as r: r.read()
                    self.set("primary")
                except Exception:
                    self.log("primary still down; staying on fallback")
            if self.mode == "fallback": return self.fallback, self.fb_opener
            return self.primary, self.pr_opener

    def failed(self):
        with self.lock:
            if self.mode == "primary":
                self.fails += 1
                if self.fallback and self.fails >= self.threshold: self.set("fallback")

    def ok(self):
        with self.lock:
            if self.mode == "primary": self.fails = 0

    def request(self, sec, method, path, body=None, ctype=None, timeout=70):
        base, opener = self.base()
        req = urllib.request.Request(base.rstrip("/") + path, data=body, method=method)
        req.add_header("Authorization", "Bearer " + sec)
        if ctype: req.add_header("Content-Type", ctype)
        try:
            with opener.open(req, timeout=timeout) as r:
                data = r.read(); st = r.status
                ct = r.headers.get_content_type()
        except urllib.error.HTTPError as e:
            if e.code >= 500: self.failed()
            else: self.ok()
            raise
        except Exception:
            self.failed(); raise
        self.ok()
        return st, (json.loads(data) if data and ct == "application/json" else data)
