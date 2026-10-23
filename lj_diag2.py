import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("192.168.117.41", port=8888, username="liuxd", password="lxd.5883667",
          timeout=25, banner_timeout=30, auth_timeout=30,
          look_for_keys=False, allow_agent=False)


def run(cmd, t=90):
    si, so, se = c.exec_command(cmd, timeout=t)
    return so.read().decode("utf-8", "replace"), se.read().decode("utf-8", "replace")


print("kill all:", run("pkill -9 -f '[l]ight_jupyter'; sleep 2; "
                       "ps aux | grep -c '[l]ight_jupyter'")[0].strip())
print("ports free:", run("ss -tln | grep -E '88[0-9][0-9]|90[0-9][0-9]|91[0-9][0-9]|92[0-9][0-9]|20[0-9]{3}|30[0-9]{3}' || echo none")[0].strip())

# start ONE, in the foreground of a setsid'd script, and immediately curl it
script = """#!/bin/bash
cd /home/liuxd/LjDev || exit 1
exec .venv/bin/python -m light_jupyter --port 8890 --root /home/liuxd/LjDev
"""
sftp = c.open_sftp()
with sftp.open("/tmp/lj_launch.sh", "wb") as f:
    f.write(script.encode())
sftp.chmod("/tmp/lj_launch.sh", 0o755)
sftp.close()

print(run("rm -f /tmp/lj.log; setsid /tmp/lj_launch.sh </dev/null >/tmp/lj.log 2>&1 & "
          "sleep 5; echo launched")[0].strip())
print("listening:", run("ss -tln | grep 8890")[0].strip())
print("--- curl loopback ---")
print(run("for i in 1 2 3; do timeout 6 curl -sS -o /dev/null "
          "-w 'try'$i' http=%{http_code} t=%{time_total}\\n' "
          "http://127.0.0.1:8890/ 2>&1; done")[0])
print("--- log ---")
print(run("tail -20 /tmp/lj.log")[0])
c.close()