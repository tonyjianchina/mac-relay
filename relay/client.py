"""Box-side helpers shared by relay-send and relay-box-agent (the Mac worker embeds net.py)."""
import json, os, uuid, urllib.parse
from relay.net import Endpoint

def secret(path=None):
    """First non-empty line: during a rotation the file may hold "new\\nold"."""
    with open(path or os.environ["RELAY_SECRET_FILE"], "rb") as f:
        return next((l.strip() for l in f.read().decode().splitlines() if l.strip()), "")

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

def endpoints_from_env(log=None, sec=None):
    """RELAY_URL is the primary channel; RELAY_FALLBACK_URL is a comma-separated list of backups.
    Names: primary, backup, backup2, ... (used in logs)."""
    e = os.environ
    sec = sec or secret()
    urls = [e["RELAY_URL"]] + [u.strip() for u in (e.get("RELAY_FALLBACK_URL") or "").split(",") if u.strip()]
    out, seen = [], set()
    for i, u in enumerate(urls):
        if u.rstrip("/") in seen:
            continue
        seen.add(u.rstrip("/"))
        out.append(Endpoint(u, sec, "primary" if i == 0 else ("backup" if i == 1 else f"backup{i}"), log=log))
    return out
