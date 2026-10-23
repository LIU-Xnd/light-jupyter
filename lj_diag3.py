import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("192.168.117.41", port=8888, username="liuxd", password="lxd.5883667",
          timeout=25, banner_timeout=30, auth_timeout=30,
          look_for_keys=False, allow_agent=False)


def run(cmd, t=90):
    si, so, se = c.exec_command(cmd, timeout=t)
    return so.read().decode("utf-8", "replace"), se.read().decode("utf-8", "replace")


print("=== is the light_jupyter process still alive? ===")
print(run("ps aux | grep '[l]ight_jupyter'; echo rc=$?")[0])

print("=== curl exists? ===")
print(run("which curl; curl --version 2>&1 | head -2")[0])

print("=== curl with explicit verbose ===")
print(run("timeout 8 curl -v http://127.0.0.1:8890/ 2>&1 | head -20")[0])

print("=== does loopback work AT ALL on this host? ===")
print(run("timeout 8 python3 -c \""
          "import socket,threading,time\n"
          "s=socket.socket();s.bind(('127.0.0.1',0));s.listen(1)\n"
          "p=s.getsockname()[1]\n"
          "def srv():\n"
          "    c,_=s.accept(); c.sendall(b'HELLO'); c.close()\n"
          "threading.Thread(target=srv,daemon=True).start()\n"
          "time.sleep(0.2)\n"
          "cl=socket.create_connection(('127.0.0.1',p),3)\n"
          "print('loopback OK:',cl.recv(10))\n" " 2>&1 | tail -3")[0])

print("=== python loopback to 8890, 20s patience ===")
print(run("timeout 25 python3 -c \""
          "import socket\n"
          "cl=socket.create_connection(('127.0.0.1',8890),10)\n"
          "cl.sendall(b'GET / HTTP/1.0\\r\\nHost: x\\r\\n\\r\\n')\n"
          "print('resp:', cl.recv(120))\n" " 2>&1 | tail -3")[0])

print("=== thread count / GIL? check if the loop is even spinning ===")
print(run("top -b -n1 -p $(pgrep -f '[l]ight_jupyter' | head -1) 2>/dev/null | tail -4")[0])

print("=== is it a HTTPServer with add_socket but no run? try the socket directly ===")
print(run("timeout 10 python3 -c \""
          "import socket\n"
          "srv=socket.socket(); srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
          "srv.bind(('127.0.0.1',8899)); srv.listen(1)\n"
          "cl=socket.create_connection(('127.0.0.1',8899),3)\n"
          "print('fresh server reachable:', srv.accept()[0] is not None)\n"
          " 2>&1 | tail -3")[0])
c.close()