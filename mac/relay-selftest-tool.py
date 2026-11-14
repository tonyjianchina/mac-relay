#!/usr/bin/env python3
"""Deterministic test tool for mac-relay end-to-end checks (stdlib only, no model, no network).
Usage (worker tool template): relay-selftest-tool.py {prompt_file}
Enable with worker.json: tools.selftest = [python3, this file, "{prompt_file}"] and
prompt_routes = [{"prefix": "MAC-RELAY-SELFTEST:", "tool": "selftest"}].
Directives in the prompt: sleep=N, outmb=N (N MiB random rand.bin), copyin (copy inbox files to
the outbox), nested (sub/dir/deep.txt), fail (exit 3). Always writes manifest.json with the size
and sha256 of every inbox and outbox file, and prints one summary line."""
import hashlib, json, os, re, shutil, sys, time

def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

prompt = open(sys.argv[1], encoding="utf-8").read()
inbox, out = os.environ["RELAY_INBOX"], os.environ["RELAY_OUTBOX"]
m = re.search(r"\bsleep=(\d+)", prompt)
if m:
    time.sleep(int(m.group(1)))
if re.search(r"\bfail\b", prompt):
    print("selftest: failing on request", file=sys.stderr); sys.exit(3)
m = re.search(r"\boutmb=(\d+)", prompt)
if m:
    with open(os.path.join(out, "rand.bin"), "wb") as f:
        for _ in range(int(m.group(1))):
            f.write(os.urandom(1 << 20))
if re.search(r"\bcopyin\b", prompt):
    for n in os.listdir(inbox):
        shutil.copyfile(os.path.join(inbox, n), os.path.join(out, "copy-" + n))
if re.search(r"\bnested\b", prompt):
    os.makedirs(os.path.join(out, "sub", "dir"), exist_ok=True)
    open(os.path.join(out, "sub", "dir", "deep.txt"), "w").write("deep\n")
man = {"prompt_bytes": len(prompt.encode()), "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
       "inbox": {n: {"size": os.path.getsize(os.path.join(inbox, n)), "sha256": sha(os.path.join(inbox, n))}
                 for n in sorted(os.listdir(inbox))}, "outbox": {}}
for root, _, names in os.walk(out):
    for n in names:
        p = os.path.join(root, n)
        man["outbox"][os.path.relpath(p, out)] = {"size": os.path.getsize(p), "sha256": sha(p)}
json.dump(man, open(os.path.join(out, "manifest.json"), "w"), indent=1)
print(f"selftest ok prompt_bytes={man['prompt_bytes']} prompt_sha={man['prompt_sha256'][:16]} "
      f"inbox={len(man['inbox'])} outbox={len(man['outbox'])}")
