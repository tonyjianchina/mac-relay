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
