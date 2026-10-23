"""Diagnose the two reports on server41:
1. ctrl-c shutdown still slow/stuck with v0.4.3?
2. is the auto-picked port reachable from outside?

Reproduce with a fresh instance (-X faulthandler, port 8890 = known-reachable),
SIGINT it, time the exit; if stuck, SIGABRT dumps stacks.
"""
import time

import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("192.168.117.41", port=8888, username="liuxd", password="lxd.5883667",
          timeout=25, banner_timeout=30, auth_timeout=30,
          look_for_keys=False, allow_agent=False)


def run(cmd, t=60):
    si, so, se = c.exec_command(cmd, timeout=t)
    return so.read().decode("utf-8", "replace").strip(), se.read().decode(
        "utf-8", "replace").strip()


print("=== running instances right now ===")
out, _ = run("pgrep -af '[l]ight_jupyter' || echo none")
print(" ", out.replace("\n", "\n  "))
for pid_line in out.splitlines():
    if pid_line == "none" or not pid_line.strip().isdigit():
        continue
    pid = pid_line.strip()
    cwd, _ = run(f"readlink /proc/{pid}/cwd")
    marker, _ = run(f"grep -c 'os._exit(0)' {cwd}/light_jupyter/__main__.py 2>/dev/null")
    port, _ = run(f"ss -tlnp 2>/dev/null | grep 'pid={pid},' | head -2")
    print(f"  pid {pid}: cwd={cwd} os._exit-marker={marker} listening=[{port}]")

print()
print("=== reproduce: fresh instance, port 8890, faulthandler on ===")
launcher = ("#!/bin/bash\ncd /home/liuxd/LjDev || exit 1\n"
            "exec .venv/bin/python -X faulthandler -m light_jupyter "
            "--port 8890 --root /home/liuxd/LjDev\n")
sftp = c.open_sftp()
with sftp.open("/tmp/lj_diag_launch.sh", "wb") as f:
    f.write(launcher.encode())
sftp.chmod("/tmp/lj_diag_launch.sh", 0o755)
sftp.close()
run("pkill -9 -f 'lj_diag_launch' 2>/dev/null; rm -f /tmp/lj_diag.log; "
    "setsid /tmp/lj_diag_launch.sh </dev/null >/tmp/lj_diag.log 2>&1 & sleep 6")
pid, _ = run("pgrep -f '[f]aulthandler -m light_jupyter' | head -1")
print("  test pid:", pid)
print("  banner:", run("grep -E 'url' /tmp/lj_diag.log")[0].strip()[:110])

# SIGINT = what ctrl-c delivers
t0 = time.time()
run(f"kill -INT {pid}")
status = "still alive"
for i in range(60):                     # poll up to 12 s
    time.sleep(0.2)
    alive, _ = run(f"kill -0 {pid} 2>/dev/null && echo yes || echo no")
    if alive == "no":
        status = f"exited after {time.time()-t0:.1f}s"
        break
print(f"  after SIGINT: {status}")

if status.startswith("still"):
    print("  STUCK -> SIGABRT for stack dump")
    run(f"kill -ABRT {pid}; sleep 2")
    out, _ = run("tail -40 /tmp/lj_diag.log")
    print(out)
run("pkill -9 -f '[l]ight_jupyter' 2>/dev/null")
c.close()