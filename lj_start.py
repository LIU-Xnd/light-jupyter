"""Start light_jupyter on server41 and verify it from this machine."""
import sys, time
import paramiko

HOST, PORT, USER, PWD = "192.168.117.41", 8888, "liuxd", "lxd.5883667"
REMOTE = "/home/liuxd/LjDev"
WEB_PORT = 8890
TOKEN = "lj-dev-token-8890"

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, port=PORT, username=USER, password=PWD,
          timeout=25, banner_timeout=30, auth_timeout=30,
          look_for_keys=False, allow_agent=False)


def run(cmd, timeout=120):
    si, so, se = c.exec_command(cmd, timeout=timeout)
    return so.read().decode("utf-8", "replace"), se.read().decode("utf-8", "replace")


print("=== stop any old instance ===")
# pkill -f on the module name misses when the cmdline is
# "/path/.venv/bin/python -m light_jupyter --port 8890"; match on the port.
print(run(f"pkill -f 'light_jupyter --port {WEB_PORT}' 2>/dev/null; "
          f"pkill -f 'm light_jupyter' 2>/dev/null; sleep 2; "
          f"ss -tln | grep {WEB_PORT} || echo 'port free'")[0])

print("=== start ===")
out, err = run(f"""
cd {REMOTE} || exit 1
rm -f /tmp/light_jupyter.log
nohup .venv/bin/python -m light_jupyter --port {WEB_PORT} --root {REMOTE} --token {TOKEN} \\
  > /tmp/light_jupyter.log 2>&1 &
sleep 5
cat /tmp/light_jupyter.log
echo '--- listening ---'
ss -tln | grep {WEB_PORT}
""")
print(out)
if err.strip():
    print("[stderr]", err[:500])

print("=== smoke test FROM ON THE SERVER (over ssh, not from Windows) ===")
out, err = run(f"""
python3 - <<'PYEOF'
import urllib.request, urllib.error
B = "http://127.0.0.1:{WEB_PORT}"
T = "{TOKEN}"

def get(p):
    try:
        with urllib.request.urlopen(B + p, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")

s, b = get("/")
print("GET /            ->", s, "html bytes:", len(b), "has brand:", "light_jupyter" in b)
s, b = get("/static/app.js")
print("GET /static/app.js ->", s, "bytes:", len(b))
s, b = get("/static/app.css")
print("GET /static/app.css ->", s, "bytes:", len(b))
s, b = get("/api/info?token=" + T)
print("GET /api/info    ->", s, b[:120])
s, b = get("/api/info")
print("GET /api/info (no token) ->", s, "(want 401)")
s, b = get("/api/info?token=wrong")
print("GET /api/info (bad token) ->", s, "(want 401)")
PYEOF
""", timeout=120)
print(out)
if err.strip():
    print("[stderr]", err[:800])

c.close()