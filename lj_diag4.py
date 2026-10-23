"""Upload-run pattern (no heredoc, no shell quoting games)."""
import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("192.168.117.41", port=8888, username="liuxd", password="lxd.5883667",
          timeout=25, banner_timeout=30, auth_timeout=30,
          look_for_keys=False, allow_agent=False)
sftp = c.open_sftp()

DIAG = '''import socket, threading, time, traceback

def line(*a):
    print(*a, flush=True)

# 1. does loopback work AT ALL for a plain socket server?
line("--- A. plain socket loopback ---")
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 0))
srv.listen(1)
p = srv.getsockname()[1]
box = {}


def serve():
    try:
        conn, _ = srv.accept()
        conn.sendall(b"HELLO")
        conn.close()
        box["served"] = True
    except Exception as e:
        box["err"] = repr(e)


t = threading.Thread(target=serve, daemon=True)
t.start()
time.sleep(0.3)
try:
    cl = socket.create_connection(("127.0.0.1", p), 3)
    data = cl.recv(20)
    cl.close()
    line("    loopback response:", data)
except Exception as e:
    line("    loopback FAILED:", type(e).__name__, e)
line("    server side:", box)

# 2. does the running light_jupyter answer?
line("--- B. light_jupyter on 8890 ---")
try:
    cl = socket.create_connection(("127.0.0.1", 8890), 5)
    cl.sendall(b"GET / HTTP/1.0\\r\\nHost: x\\r\\n\\r\\n")
    cl.settimeout(8)
    data = cl.recv(200)
    cl.close()
    line("    response:", data[:120])
except Exception as e:
    line("    FAILED:", type(e).__name__, e)

# 3. is the process in epoll (a running Tornado) or blocked?
line("--- C. process state ---")
import subprocess
out = subprocess.run(["ps", "-o", "pid,stat,pcpu,time,wchan:20,cmd",
                      "-p", subprocess.run(["pgrep", "-f", "light_jupyter"],
                                            capture_output=True).stdout.decode().split()[0]],
                     capture_output=True, text=True)
line("   ", out.stdout.strip() or out.stderr.strip())
out2 = subprocess.run(["cat", "/proc/net/tcp"], capture_output=True, text=True)
for ln in out2.stdout.splitlines()[1:]:
    f = ln.split()
    if len(f) > 3:
        port = int(f[1].split(":")[1], 16)
        if port == 8890:
            line("    /proc/net/tcp local:", f[1], "state:", f[3], "tx/rx:",
                 f[4], f[5], "(0A = LISTEN)")

# 4. can WE start a minimal tornado in the SAME venv, detached?
line("--- D. minimal tornado in this venv ---")
code = """
import asyncio, tornado.httpserver, tornado.ioloop
async def h(req):
    req.write("HI"); req.finish()
app = tornado.web.Application([(r"/", h)])
app.listen(8897, address="127.0.0.1")
print("listening 8897", flush=True)
tornado.ioloop.IOLoop.current().start()
"""
with open("/tmp/mini_tornado.py", "w") as f:
    f.write(code)

import subprocess as sp
sp.run(["pkill", "-f", "[m]ini_tornado"])
time.sleep(1)
with open("/tmp/mini.log", "w") as lf:
    p = sp.Popen(["setsid", "/home/liuxd/LjDev/.venv/bin/python", "/tmp/mini_tornado.py"],
                 stdout=lf, stderr=sp.STDOUT, stdin=sp.DEVNULL)
time.sleep(4)
try:
    import urllib.request
    with urllib.request.urlopen("http://127.0.0.1:8897/", timeout=6) as r:
        line("    minimal tornado ->", r.status, r.read()[:20])
except Exception as e:
    line("    minimal tornado FAILED:", type(e).__name__, e)
with open("/tmp/mini.log") as lf:
    line("    mini.log:", lf.read()[:200])
sp.run(["pkill", "-f", "[m]ini_tornado"])
'''

with sftp.open("/tmp/diag.py", "wb") as f:
    f.write(DIAG.encode())

si, so, se = c.exec_command("python3 /tmp/diag.py 2>&1", timeout=180)
print(so.read().decode("utf-8", "replace"))
e = se.read().decode("utf-8", "replace")
if e.strip():
    print("[stderr]", e[:1000])
c.close()