#!/usr/bin/env python3
"""Stub for hermes/deepseek/codex: `stub_tool.py <tool> <prompt>`. Runs in the task outbox (cwd).
Prompt "-" means read the prompt from stdin. Prompt keywords: sleep=N, media (emit png+mp4-ish+txt),
fail (exit 3), big (16MiB+ output), nested (sub/dir/deep.txt), copyin (copy inbox files to outbox),
outmb=N (N MiB of random output as rand.bin). RELAY_STUB_RUNS, if set, gets one line per run."""
import hashlib, os, re, shutil, struct, sys, time, zlib
tool, prompt = sys.argv[1], sys.argv[2]
if prompt == "-":
    prompt = sys.stdin.read()
if os.environ.get("RELAY_STUB_RUNS"):
    open(os.environ["RELAY_STUB_RUNS"], "a").write(os.environ.get("RELAY_TASK_ID", "?") + "\n")
m = re.search(r"sleep=(\d+)", prompt)
time.sleep(int(m.group(1)) if m else 0)
if "fail" in prompt:
    print("stub: about to fail", flush=True); print("stub error: simulated failure", file=sys.stderr); sys.exit(3)
def png():
    raw = b"".join(b"\x00" + b"".join(bytes([x * 16, y * 16, 128]) for x in range(16)) for y in range(16))
    ch = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0)) + ch(b"IDAT", zlib.compress(raw)) + ch(b"IEND", b"")
open("image.png", "wb").write(png())
inbox = os.environ["RELAY_INBOX"]
open("notes.txt", "w").write(f"tool={tool}\nprompt={prompt[:200]}\ninbox={sorted(os.listdir(inbox))}\n")
if "media" in prompt:
    open("clip.mp4", "wb").write(b"\x00\x00\x00\x18ftypmp42" + os.urandom(200_000))
if "nested" in prompt:
    os.makedirs("sub/dir", exist_ok=True); open("sub/dir/deep.txt", "w").write("deep\n")
if "copyin" in prompt:
    for n in os.listdir(inbox):
        shutil.copyfile(os.path.join(inbox, n), "copy-" + n)
m = re.search(r"outmb=(\d+)", prompt)
if m:
    with open("rand.bin", "wb") as f:
        for _ in range(int(m.group(1))):
            f.write(os.urandom(1024 * 1024))
if "big" in prompt:
    # Over the server's default multipart soft cap (16MiB). The worker must PUT this.
    n, block = 16 * 1024 * 1024 + 4096, b"relay-big-block\n" * 256
    with open("big.bin", "wb") as f:
        left = n
        while left:
            c = block if left >= len(block) else block[:left]
            f.write(c); left -= len(c)
print(f"stub {tool} ok: {prompt[:40]} len={len(prompt.encode())} sha={hashlib.sha256(prompt.encode()).hexdigest()[:16]}")
