#!/usr/bin/env python3
"""Stub for hermes/deepseek/codex: `stub_tool.py <tool> <prompt>`. Runs in the task outbox (cwd).
Prompt keywords: sleep=N, media (emit png+mp4-ish+txt), fail (exit 3)."""
import os, re, struct, sys, time, zlib
tool, prompt = sys.argv[1], sys.argv[2]
m = re.search(r"sleep=(\d+)", prompt)
time.sleep(int(m.group(1)) if m else 0)
if "fail" in prompt:
    print("stub: about to fail", flush=True); print("stub error: simulated failure", file=sys.stderr); sys.exit(3)
def png():
    raw = b"".join(b"\x00" + bytes([x * 16, y * 16, 128]) * 1 for y in range(16) for x in range(16))
    raw = b"".join(b"\x00" + b"".join(bytes([x * 16, y * 16, 128]) for x in range(16)) for y in range(16))
    ch = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0)) + ch(b"IDAT", zlib.compress(raw)) + ch(b"IEND", b"")
open("image.png", "wb").write(png())
open("notes.txt", "w").write(f"tool={tool}\nprompt={prompt}\ninbox={sorted(os.listdir(os.environ['RELAY_INBOX']))}\n")
if "media" in prompt:
    open("clip.mp4", "wb").write(b"\x00\x00\x00\x18ftypmp42" + os.urandom(200_000))
if "big" in prompt:
    # Over the server's default multipart soft cap (16MiB). The worker must PUT this.
    n, block = 16 * 1024 * 1024 + 4096, b"relay-big-block\n" * 256
    with open("big.bin", "wb") as f:
        left = n
        while left:
            c = block if left >= len(block) else block[:left]
            f.write(c); left -= len(c)
print(f"stub {tool} ok: {prompt[:40]}")
