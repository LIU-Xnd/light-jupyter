import socket, time
import paramiko

HOST, SSH_PORT, USER, PWD = "192.168.117.41", 8888, "liuxd", "lxd.5883667"

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, port=SSH_PORT, username=USER, password=PWD,
          timeout=25, banner_timeout=30, auth_timeout=30,
          look_for_keys=False, allow_agent=False)


def run(cmd, timeout=90):
    si, so, se = c.exec_command(cmd, timeout=timeout)
    return so.read().decode("utf-8", "replace"), se.read().decode("utf-8", "replace")


print("=== who holds 8890 ===")
print(run("ss -tlnp | grep 8890; echo '--- lj procs ---'; ps aux | grep '[l]ight_jupyter' | head")[0])

print("=== 8890 bind test log ===")
print(run("cat /tmp/pc_8890.log 2>/dev/null | tail -5")[0])

print("=== cleanup any leftovers ===")
print(run("pkill -f lj_portcheck.py; sleep 1; ss -tln | grep -E '88[0-9][0-9]|900[0-9]|9100|9200' || echo clean")[0])

print()
print("=== restart light_jupyter on 8890 and retest from Windows ===")
run("""
cd /home/liuxd/LjDev
nohup .venv/bin/python -m light_jupyter --port 8890 --root /home/liuxd/LjDev --token lj-dev-token-8890 > /tmp/light_jupyter.log 2>&1 &
sleep 5
grep -E 'url|port' /tmp/light_jupyter.log
""")
print(run("ss -tln | grep 8890")[0])

c.close()

for p in [8890, 8892, 22, 8888]:
    s = socket.socket(); s.settimeout(8)
    t0 = time.time()
    try:
        s.connect((HOST, p))
        print(f"  {p:5d} REACHABLE  {(time.time()-t0)*1000:.0f} ms")
    except socket.timeout:
        print(f"  {p:5d} TIMEOUT")
    except Exception as e:
        print(f"  {p:5d} {type(e).__name__}")
    finally:
        s.close()