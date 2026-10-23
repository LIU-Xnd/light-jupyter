"""Verify key-grade tokens over HTTP from ON the server (bypasses the flaky
network path between this box and server41)."""
import re
import sys
import urllib.error
import urllib.request

import paramiko

HOST, SSH_PORT, USER, PWD = "192.168.117.41", 8888, "liuxd", "lxd.5883667"
REMOTE = "/home/liuxd/LjDev"
TOKFILE = "/home/liuxd/.light_jupyter/token"
WEB = 8890

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, port=SSH_PORT, username=USER, password=PWD,
          timeout=25, banner_timeout=30, auth_timeout=30,
          look_for_keys=False, allow_agent=False)
sftp = c.open_sftp()


def run(cmd, timeout=120):
    si, so, se = c.exec_command(cmd, timeout=timeout)
    return so.read().decode("utf-8", "replace"), se.read().decode("utf-8", "replace")


# restart cleanly
run("pkill -f '[l]ight_jupyter'; sleep 2")
script = f"""#!/bin/bash
cd {REMOTE} || exit 1
exec .venv/bin/python -m light_jupyter --port {WEB} --root {REMOTE}
"""
with sftp.open("/tmp/lj_launch.sh", "wb") as f:
    f.write(script.encode())
sftp.chmod("/tmp/lj_launch.sh", 0o755)
run("rm -f /tmp/lj.log; setsid /tmp/lj_launch.sh </dev/null >/tmp/lj.log 2>&1 & "
    "sleep 6; echo started")

print("=" * 62)
print("server-side HTTP checks against 127.0.0.1")
print("=" * 62)

PROBE = """import urllib.request, urllib.error

B = "http://127.0.0.1:%d"
TOK = open("%s").read().strip()

def get(p, cookie=None):
    req = urllib.request.Request(B + p)
    if cookie:
        req.add_header("Cookie", "lj_token=" + cookie)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

print("token:", TOK[:24] + "...", "(%%d chars)" %% len(TOK))
s, b = get("/")
print("  GET /                    ->", s, len(b), "bytes")
s, _ = get("/api/info?token=" + TOK)
print("  correct token            ->", s, "(want 200)")
s, _ = get("/api/info?token=" + TOK[:-1])
print("  off-by-one token         ->", s, "(want 401)")
s, _ = get("/api/info?token=" + TOK.upper())
print("  wrong-case token         ->", s, "(want 401)")
s, _ = get("/api/info")
print("  no token                 ->", s, "(want 401)")
s, _ = get("/api/info", cookie=TOK)
print("  cookie token             ->", s, "(want 200)")

print("  --- throttle ---")
codes = [get("/api/info?token=wrong")[0] for _ in range(8)]
print("  8 bad tokens             ->", codes)
print("  429 present              ->", 429 in codes)
""" % (WEB, TOKFILE)

with sftp.open("/tmp/lj_probe.py", "wb") as f:
    f.write(PROBE.encode())

out, err = run("python3 /tmp/lj_probe.py 2>&1")
print(out)
if "->" not in out:
    print("[server log]", run("tail -20 /tmp/lj.log")[0])

c.close()