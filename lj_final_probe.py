import os
import socket
import subprocess
import time
import urllib.error
import urllib.request

BIN = "/home/liuxd/LjDev/.venv/bin/python"
APP = "/home/liuxd/LjDev"
LOG = "/tmp/lj_final.log"
TOKFILE = "/home/liuxd/.light_jupyter/token"


def probe(url, timeout=6):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return f"{r.status} ({len(r.read())} bytes)"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def state(pid):
    try:
        out = subprocess.run(["ps", "-o", "stat,pcpu,time,wchan:18", "-p", str(pid)],
                             capture_output=True, text=True).stdout.splitlines()
        return out[1].strip() if len(out) > 1 else "gone"
    except Exception as e:
        return repr(e)


def kill_by_pid():
    """Kill leftovers by scanning /proc. NEVER pkill -f: the pattern matches
    our own command line and kills the calling process."""
    killed = []
    me = os.getpid()
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as fh:
                cl = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        if "light_jupyter" in cl and int(d) != me:
            try:
                os.kill(int(d), 9)
                killed.append(d)
            except OSError:
                pass
    time.sleep(1.5)
    return killed


print("killed leftovers:", kill_by_pid() or "none")

log = open(LOG, "w")
proc = subprocess.Popen([BIN, "-m", "light_jupyter", "--port", "8890", "--root", APP],
                        stdout=log, stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL, cwd=APP, start_new_session=True)
print("started pid", proc.pid)
time.sleep(6)

print("  process state  :", state(proc.pid))
print("  loopback GET / :", probe("http://127.0.0.1:8890/"))
print("  process state  :", state(proc.pid))

tok = open(TOKFILE).read().strip()
print("  correct token  :", probe("http://127.0.0.1:8890/api/info?token=" + tok))
print("  no token       :", probe("http://127.0.0.1:8890/api/info"))

codes = []
for _ in range(8):
    try:
        urllib.request.urlopen("http://127.0.0.1:8890/api/info?token=bad", timeout=6)
        codes.append(200)
    except urllib.error.HTTPError as e:
        codes.append(e.code)
    except Exception as e:
        codes.append(type(e).__name__)
print("  throttle seq   :", codes)
print("  final state    :", state(proc.pid))
print("RESULT_DONE")