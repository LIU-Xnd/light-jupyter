import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("192.168.117.41", port=8888, username="liuxd", password="lxd.5883667",
          timeout=25, banner_timeout=30, auth_timeout=30,
          look_for_keys=False, allow_agent=False)
sftp = c.open_sftp()

# previous run already wrote /tmp/t.out before the channel died
for f in ("/tmp/t.out", "/tmp/lj_diag.log"):
    try:
        with sftp.open(f, "rb") as fh:
            print("=" * 20, f, "=" * 20)
            print(fh.read().decode("utf-8", "replace")[:1200])
    except IOError:
        print(f, "(missing)")

# kill whatever the diag left running
si, so, se = c.exec_command("pkill -9 -f '[l]ight_jupyter'; sleep 1; "
                            "ps aux | grep -c '[l]ight_jupyter'", timeout=40)
so.read()
c.close()