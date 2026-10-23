import socket
import sys
import time
import urllib.error
import urllib.request

import paramiko

HOST, SSH_PORT, USER, PWD = "192.168.117.41", 8888, "liuxd", "lxd.5883667"
REMOTE = "/home/liuxd/LjDev"
TOKEN = "lj-dev-token-8890"
CANDS = [8890, 8892, 8894, 8896, 8897, 8899, 8788, 9000, 9001, 9100, 9200]

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, port=SSH_PORT, username=USER, password=PWD,
          timeout=25, banner_timeout=30, auth_timeout=30,
          look_for_keys=False, allow_agent=False)


def run(cmd, timeout=90):
    si, so, se = c.exec_command(cmd, timeout=timeout)
    return so.read().decode("utf-8", "replace"), se.read().decode("utf-8", "replace")


print("kill:", run("pkill -f 'm light_jupyter' 2>/dev/null; sleep 1; echo done")[0].strip())

SRC = '''import socket, sys, time
p = int(sys.argv[1])
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", p)); s.listen(16)
print("BOUND", p, flush=True)
time.sleep(120)
'''

sftp = c.open_sftp()
with sftp.open("/tmp/lj_portcheck.py", "wb") as f:
    f.write(SRC.encode())
sftp.close()

serving, reachable, dead = [], [], []
for p in CANDS:
    run(f"pkill -f 'lj_portcheck.py {p}' 2>/dev/null")
    out, _ = run(f"nohup python3 -u /tmp/lj_portcheck.py {p} > /tmp/pc_{p}.log 2>&1 & sleep 1.5; cat /tmp/pc_{p}.log")
    if "BOUND" not in out:
        print(f"  {p}: server could not bind")
        continue
    serving.append(p)

    s = socket.socket(); s.settimeout(7)
    t0 = time.time()
    try:
        s.connect((HOST, p))
        dt = (time.time() - t0) * 1000
        print(f"  {p:5d}: server BOUND, client REACHABLE ({dt:.0f} ms)  <== usable")
        reachable.append(p)
    except socket.timeout:
        print(f"  {p:5d}: server BOUND, client TIMEOUT                 <== unusable")
        dead.append(p)
    except Exception as e:
        print(f"  {p:5d}: server BOUND, client {type(e).__name__}")
    finally:
        s.close()
    run(f"pkill -f 'lj_portcheck.py {p}' 2>/dev/null")

print()
print("server-bindable:", serving)
print("client-reachable:", reachable)
print("bound-but-unreachable:", dead)

# sanity: from ON the server, can it reach its own bound port? distinguishes
# "firewall drops us" from "the service is broken"
if reachable:
    p = reachable[0]
    run(f"nohup python3 -u /tmp/lj_portcheck.py {p} > /tmp/pc2.log 2>&1 & sleep 1.5")
    out, _ = run(f"timeout 6 python3 -c \"import socket;s=socket.create_connection(('127.0.0.1',{p}),4);print('loopback ok')\" 2>&1 | tail -1")
    print("loopback check on", p, "->", out.strip())
    run("pkill -f 'lj_portcheck.py'")

c.close()

# Write the verified list back out so ports.py can use it
with open(r"D:\AgentWorkspace\projects\ServerHelper\lj_reachable_ports.txt", "w") as fh:
    fh.write(", ".join(str(p) for p in reachable))
print()
print("wrote reachable list:", reachable)