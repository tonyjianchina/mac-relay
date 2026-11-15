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

DEFAULT_BIG_BYTES = 8 * 1024 * 1024


def big_bytes(env=None):
    """RELAY_BIG_BYTES, default 8 MiB. Attachments or results over this use the public channel first."""
    try:
        return max(0, int((env or os.environ).get("RELAY_BIG_BYTES", DEFAULT_BIG_BYTES)))
    except (TypeError, ValueError):
        return DEFAULT_BIG_BYTES


def is_public_url(url):
    return (url or "").lower().startswith("https://")


def public_endpoint(eps, primary_name="primary"):
    """The VPS public URL: first https fallback, else the first non-primary endpoint (loopback tests)."""
    named = [e for e in eps if e.name != primary_name]
    return next((e for e in named if is_public_url(e.url)), None) or (named[0] if named else None)


def order_for_size(eps, total, primary_name="primary", threshold=None):
    """When total bytes exceed the threshold, try the public channel first; otherwise keep config order."""
    eps = list(eps)
    if total <= (big_bytes() if threshold is None else threshold):
        return eps
    pub = public_endpoint(eps, primary_name)
    if not pub:
        return eps
    return [pub] + [e for e in eps if e is not pub]


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
