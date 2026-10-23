"""Real test of key-grade tokens on server41: persistence across restarts,
--show-token, --regen-token, and the throttle over the wire."""
import re
import socket
import sys
import time
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


def get(path, timeout=12):
    url = f"http://{HOST}:{WEB}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return type(e).__name__, b""


print("=" * 66)
print("1. mint a token, check the file")
print("=" * 66)
print(run(f"rm -f {TOKFILE}")[0])
out, _ = run(f"cd {REMOTE} && .venv/bin/python -m light_jupyter --show-token 2>&1")
tok1 = out.strip().splitlines()[-1]
print(f"  token   : {tok1}")
print(f"  length  : {len(tok1)} chars, body {len(tok1)-3} + 3 separators")
print(run(f"ls -l {TOKFILE}; echo '--- perms (want 600) ---'; "
          f"stat -c '%a %n' {TOKFILE}")[0])

print()
print("=" * 66)
print("2. --show-token is stable across invocations")
print("=" * 66)
out2, _ = run(f"cd {REMOTE} && .venv/bin/python -m light_jupyter --show-token 2>&1")
tok2 = out2.strip().splitlines()[-1]
print(f"  same token: {tok1 == tok2}")

print()
print("=" * 66)
print("3. --regen-token mints a new one")
print("=" * 66)
out3, _ = run(f"cd {REMOTE} && .venv/bin/python -m light_jupyter --regen-token --show-token 2>&1")
tok3 = out3.strip().splitlines()[-1]
print(f"  old : {tok1}")
print(f"  new : {tok3}")
print(f"  differs: {tok1 != tok3}")

print()
print("=" * 66)
print("4. start the server, verify the token works over the network")
print("=" * 66)
LAUNCHER = "/tmp/lj_launch.sh"


def install_launcher():
    """Write a launcher script.

    The `cmd &` form inside exec_command keeps the shell alive (the background
    job still holds the channel's stdout), so exec_command blocks until
    timeout. Detach by handing the whole thing to setsid in a separate script
    whose stdout is already redirected.
    """
    script = f"""#!/bin/bash
cd {REMOTE} || exit 1
exec .venv/bin/python -m light_jupyter --port {WEB} --root {REMOTE}
"""
    with sftp.open(LAUNCHER, "wb") as f:
        f.write(script.encode())
    sftp.chmod(LAUNCHER, 0o755)


def kill_light_jupyter():
    """pkill -f matches the FULL command line, so a plain pattern would match
    this very shell's own cmdline (which contains the pattern) and kill it --
    leaving the ssh channel with no EOF. The bracket trick avoids that."""
    return run("pkill -f '[l]ight_jupyter'; sleep 2; "
               "ps aux | grep -c '[l]ight_jupyter'")[0].strip()


def start(logfile):
    """Start fully detached so exec_command returns immediately."""
    install_launcher()
    kill_light_jupyter()
    return run(f"rm -f {logfile}; "
               f"setsid {LAUNCHER} </dev/null >{logfile} 2>&1 & "
               f"sleep 6; echo started")[0].strip()


start("/tmp/light_jupyter.log")
print(run("grep -E 'url|port|token' /tmp/light_jupyter.log")[0])

s = socket.socket(); s.settimeout(8)
try:
    s.connect((HOST, WEB)); print(f"  TCP {HOST}:{WEB} OK")
except Exception as e:
    print(f"  TCP FAILED: {type(e).__name__}: {e}")
s.close()

st, _ = get(f"/api/info?token={tok3}")
print(f"  correct token  -> {st}  (want 200)")
st, _ = get(f"/api/info?token={tok1}")
print(f"  old token      -> {st}  (want 401)")
st, _ = get("/api/info")
print(f"  no token       -> {st}  (want 401)")
st, _ = get("/api/info?token=" + tok3[:-1])
print(f"  off-by-one tok -> {st}  (want 401)")

print()
print("=" * 66)
print("5. throttle fires over the wire")
print("=" * 66)
codes = [get("/api/info?token=wrong")[0] for _ in range(7)]
print(f"  sequence: {codes}")
print(f"  ends in 429: {429 in codes}")

print()
print("=" * 66)
print("6. restart -> SAME token still works (persistence is the point)")
print("=" * 66)
start("/tmp/lj_restart.log")
out4, _ = run("grep 'token :' /tmp/lj_restart.log")
shown = out4.strip()
print(f"  banner: {shown}")
st, _ = get(f"/api/info?token={tok3}")
print(f"  pre-restart token after restart -> {st}  (want 200)")

print()
print("=" * 66)
print("7. entropy check")
print("=" * 66)
print(run(f"cd {REMOTE} && .venv/bin/python -c \""
          f"from light_jupyter.tokens import entropy_bits, is_wellformed;"
          f"t=open('{TOKFILE}').read().strip();"
          f"print('bits:', entropy_bits(t));"
          f"print('well-formed:', is_wellformed(t))\"")[0])

c.close()
print()
print("=" * 66)
print(f"URL: http://{HOST}:{WEB}/#token={tok3}")
print("=" * 66)