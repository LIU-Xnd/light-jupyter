"""Does setsid break it, or is it something else? Same command, three ways."""
import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("192.168.117.41", port=8888, username="liuxd", password="lxd.5883667",
          timeout=25, banner_timeout=30, auth_timeout=30,
          look_for_keys=False, allow_agent=False)
sftp = c.open_sftp()

T = '''import subprocess, time, urllib.request, urllib.error, os

BIN = "/home/liuxd/LjDev/.venv/bin/python"
APP = "/home/liuxd/LjDev"


def probe(port=8890, timeout=6):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=timeout) as r:
            return f"{r.status} ({len(r.read())} bytes)"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def kill():
    subprocess.run(["pkill", "-9", "-f", "[l]ight_jupyter"],
                   capture_output=True)
    time.sleep(1.5)


def launch(mode):
    kill()
    cmd = [BIN, "-m", "light_jupyter", "--port", "8890", "--root", APP,
           "--token", "diagtoken"]
    log = open("/tmp/lj_diag.log", "w")
    if mode == "setsid":
        p = subprocess.Popen(["setsid"] + cmd, stdout=log, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, cwd=APP)
    elif mode == "popen":
        p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, cwd=APP,
                             start_new_session=True)
    elif mode == "shell_amp":
        # emulate the naive `... &` inside a shell
        subprocess.run(
            f"cd {APP} && setsid {BIN} -m light_jupyter --port 8890 "
            f"--root {APP} --token diagtoken </dev/null >/tmp/lj_diag.log 2>&1 &",
            shell=True, capture_output=True)
        p = None
    time.sleep(5)
    return probe()


print("=== does an EARLIER-started listener still answer? ===")
# start one in the foreground of this script's own child, keep it running
kill()
log = open("/tmp/lj_diag.log", "w")
proc = subprocess.Popen([BIN, "-m", "light_jupyter", "--port", "8890",
                         "--root", APP, "--token", "diagtoken"],
                        stdout=log, stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL, cwd=APP,
                        start_new_session=True)
time.sleep(5)
print("  plain Popen(start_new_session) ->", probe())
print("  log:", open("/tmp/lj_diag.log").read()[:150].replace("\\n", " | "))
proc.kill()

print()
print("=== now via setsid from a shell ===")
print("  setsid + shell &    ->", launch("shell_amp"))
print("  log:", open("/tmp/lj_diag.log").read()[:150].replace("\\n", " | "))

kill()
'''
with sftp.open("/tmp/t.py", "wb") as f:
    f.write(T.encode())

# The diagnostic spawns children; if they inherit this channel's stdout the
# read below blocks until timeout. Send all of it to a file instead.
si, so, se = c.exec_command(
    "python3 /tmp/t.py >/tmp/t.out 2>&1 </dev/null; echo DONE", timeout=240)
so.read()
with sftp.open("/tmp/t.out", "rb") as f:
    out = f.read().decode("utf-8", "replace")
print(out)
c.close()