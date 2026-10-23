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


print("=== FULL lj.log ===")
print(run("cat /tmp/lj.log")[0])

print("=== who listens on 8890 ===")
print(run("ss -tlnp | grep 8890")[0])

print("=== all light_jupyter processes ===")
print(run("ps aux | grep '[l]ight_jupyter'")[0])

print("=== curl from ON the server (independent client) ===")
print(run("""timeout 10 curl -sS -o /dev/null -w 'http=%{http_code} time=%{time_total}\\n' \
  http://127.0.0.1:8890/ 2>&1; echo "---"; \
  timeout 10 curl -sS -o /dev/null -w 'http=%{http_code} time=%{time_total}\\n' \
  'http://127.0.0.1:8890/api/info?token=bad' 2>&1""")[0])

print("=== python socket, raw ===")
print(run("timeout 8 python3 -c \"import socket;s=socket.create_connection(('127.0.0.1',8890),4);"
          "s.sendall(b'GET / HTTP/1.0\\r\\n\\r\\n');print(s.recv(80))\" 2>&1 | tail -2")[0])

c.close()