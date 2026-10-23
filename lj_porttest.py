"""Real-world test of --port auto: stop everything, start with an empty port,
verify the printed URL answers from THIS machine, then start a second one to
prove two instances coexist."""
import re
import socket
import sys
import time
import urllib.error
import urllib.request

import paramiko

HOST, SSH_PORT, USER, PWD = "192.168.117.41", 8888, "liuxd", "lxd.5883667"
REMOTE = "/home/liuxd/LjDev"
TOKEN = "lj-dev-token-8890"

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, port=SSH_PORT, username=USER, password=PWD,
          timeout=25, banner_timeout=30, auth_timeout=30,
          look_for_keys=False, allow_agent=False)


def run(cmd, timeout=120):
    si, so, se = c.exec_command(cmd, timeout=timeout)
    return so.read().decode("utf-8", "replace"), se.read().decode("utf-8", "replace")


def kill_all():
    out, _ = run("pkill -f 'm light_jupyter'; sleep 2; "
                 "ps aux | grep -c '[l]ight_jupyter'")
    return out.strip()


def banner_port(text):
    m = re.search(r"url\s+:\s+http://[\d.]+:(\d+)/", text)
    return int(m.group(1)) if m else None


def http_ok(port, path, token=None):
    url = f"http://{HOST}:{port}{path}"
    if token:
        url += ("&" if "?" in url else "?") + "token=" + token
    try:
        with urllib.request.urlopen(url, timeout=12) as r:
            return r.status, len(r.read())
    except urllib.error.HTTPError as e:
        return e.code, 0
    except Exception as e:
        return type(e).__name__, 0


print("=" * 66)
print("STEP 1  stop all instances")
print("=" * 66)
print("remaining light_jupyter procs:", kill_all())

print()
print("=" * 66)
print("STEP 2  start with an EMPTY --port")
print("=" * 66)
out, err = run(f"""
cd {REMOTE} || exit 1
rm -f /tmp/lj_a.log /tmp/lj_b.log
nohup .venv/bin/python -m light_jupyter --port '' --root {REMOTE} --token {TOKEN} \\
  > /tmp/lj_a.log 2>&1 &
sleep 6
cat /tmp/lj_a.log
""")
print(out)
if err.strip():
    print("[stderr]", err[:400])

p1 = banner_port(out)
print(f">>> instance A picked port {p1}")

print()
print("=" * 66)
print("STEP 3  verify instance A from THIS machine")
print("=" * 66)
if not p1:
    print("FAIL: could not parse port")
    c.close(); sys.exit(1)

s = socket.socket(); s.settimeout(10); t0 = time.time()
try:
    s.connect((HOST, p1))
    print(f"  TCP  {HOST}:{p1}  OK  {(time.time()-t0)*1000:.0f} ms")
except Exception as e:
    print(f"  TCP  {HOST}:{p1}  FAILED {type(e).__name__}: {e}")
s.close()

for path, tok in [("/", None), ("/static/app.js", None),
                  ("/api/info", TOKEN), ("/api/info", None),
                  ("/api/list", TOKEN)]:
    st, n = http_ok(p1, path, tok)
    extra = "" if tok else "  (no token -> want 401)"
    print(f"  GET {path:20s} {str(tok or '-'):18s} -> {st}  {n} bytes{extra}")

print()
print("=" * 66)
print("STEP 4  start a SECOND instance, also auto -> must not collide")
print("=" * 66)
out2, _ = run(f"""
cd {REMOTE} || exit 1
nohup .venv/bin/python -m light_jupyter --port auto --root {REMOTE} --token second \\
  > /tmp/lj_b.log 2>&1 &
sleep 6
cat /tmp/lj_b.log
""")
p2 = banner_port(out2)
print(f">>> instance A = {p1}")
print(f">>> instance B = {p2}")
print(f">>> no collision: {p1 != p2}")
if p2:
    st, n = http_ok(p2, "/api/info", "second")
    print(f"  instance B answers: {st} ({n} bytes)")

print()
print("=" * 66)
print("STEP 5  explicit --port still works; a busy port fails cleanly")
print("=" * 66)
out3, _ = run(f"""
cd {REMOTE} || exit 1
echo "--- explicit 9999 (print-only, no listen) ---"
.venv/bin/python -m light_jupyter --port 9999 --print-only --token t9999 2>&1 | grep -E 'url|port'
echo "--- busy port {p1} (should error, not traceback) ---"
timeout 12 .venv/bin/python -m light_jupyter --port {p1} --root {REMOTE} --token x 2>&1 | tail -3
echo "--- junk port ---"
.venv/bin/python -m light_jupyter --port notanumber --print-only 2>&1 | tail -2
""")
print(out3)

print()
print("=" * 66)
print("STEP 6  leave ONE clean instance on the documented port 8890")
print("=" * 66)
print("killing:", kill_all())
run(f"""
cd {REMOTE} || exit 1
nohup .venv/bin/python -m light_jupyter --port 8890 --root {REMOTE} --token {TOKEN} \\
  > /tmp/light_jupyter.log 2>&1 &
sleep 5
grep -E 'url|port' /tmp/light_jupyter.log
""")
print(run("ss -tln | grep 8890")[0])
st, n = http_ok(8890, "/api/info", TOKEN)
print(f"  restored on 8890: {st} ({n} bytes)")

c.close()
print()
print("=" * 66)
print(f"URL: http://{HOST}:8890/#token={TOKEN}")
print("=" * 66)