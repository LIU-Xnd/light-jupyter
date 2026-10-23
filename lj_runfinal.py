"""Upload lj_final_probe.py to server41, run it detached-safe, print its output."""
import paramiko

LOCAL = r"D:\AgentWorkspace\projects\ServerHelper\lj_final_probe.py"
REMOTE = "/tmp/lj_final_probe.py"
OUT = "/tmp/lj_final.out"

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("192.168.117.41", port=8888, username="liuxd", password="lxd.5883667",
          timeout=25, banner_timeout=30, auth_timeout=30,
          look_for_keys=False, allow_agent=False)
sftp = c.open_sftp()

with open(LOCAL, "rb") as src:
    data = src.read()
with sftp.open(REMOTE, "wb") as f:
    f.write(data)
print(f"uploaded {len(data)} bytes")

# The probe leaves light_jupyter running, so the channel never reaches EOF.
# Give it a short window, then read the result file instead of the channel.
ch = c.get_transport().open_session()
ch.settimeout(60)
ch.exec_command(f"python3 {REMOTE} >{OUT} 2>&1 </dev/null; echo MARKER")
buf = b""
try:
    while b"MARKER" not in buf:
        d = ch.recv(4096)
        if not d:
            break
        buf += d
except Exception:
    pass

with sftp.open(OUT, "rb") as f:
    print(f.read().decode("utf-8", "replace"))
c.close()