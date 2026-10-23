"""Verify light_jupyter is reachable from THIS Windows machine, end to end."""
import json
import socket
import ssl
import time
import urllib.error
import urllib.request

HOST = "192.168.117.41"
PORT = 8890
TOKEN = "lj-dev-token-8890"
BASE = f"http://{HOST}:{PORT}"

print("=== 1. TCP reachability ===")
s = socket.socket()
s.settimeout(10)
t0 = time.time()
try:
    s.connect((HOST, PORT))
    print(f"  connect OK  {(time.time()-t0)*1000:.0f} ms")
except Exception as e:
    print(f"  FAILED: {type(e).__name__}: {e}")
    raise SystemExit(1)
finally:
    s.close()

print()
print("=== 2. HTTP surface ===")


def get(p, hdrs=None, timeout=15):
    req = urllib.request.Request(BASE + p)
    for k, v in (hdrs or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


st, body = get("/")
print(f"  GET /                 -> {st}  {len(body)} bytes  "
      f"brand={'light_jupyter' in body.decode('utf-8','replace')}")
st, body = get("/static/app.js")
print(f"  GET /static/app.js    -> {st}  {len(body)} bytes")
st, body = get("/static/app.css")
print(f"  GET /static/app.css   -> {st}  {len(body)} bytes")
st, body = get("/api/info?token=" + TOKEN)
print(f"  GET /api/info (token) -> {st}  {body.decode()[:110]}")
st, _ = get("/api/info")
print(f"  GET /api/info (none)  -> {st}  (want 401)")
st, _ = get("/api/info?token=wrong")
print(f"  GET /api/info (wrong) -> {st}  (want 401)")

print()
print("=== 3. file API through the token ===")
st, body = get("/api/list?token=" + TOKEN)
entries = json.loads(body)["entries"]
print(f"  GET /api/list -> {st}  {len(entries)} entries: "
      f"{[e['name'] for e in entries][:6]}")

post_data = json.dumps({"path": "/remote_write_test.txt",
                        "text": "written from Windows\n"}).encode()
req = urllib.request.Request(BASE + "/api/write?token=" + TOKEN, data=post_data,
                            headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=15) as r:
    print(f"  POST /api/write -> {r.status}  {r.read().decode()}")

st, body = get("/api/read?path=/remote_write_test.txt&token=" + TOKEN)
print(f"  GET /api/read  -> {st}  {json.loads(body)['text']!r}")

print()
print("=== 4. websocket terminal over the network ===")
# minimal RFC6455 client so we do not depend on a local tornado install
import base64
import os
import struct


def ws_connect(host, port, path):
    key = base64.b64encode(os.urandom(16)).decode()
    sock = socket.create_connection((host, port), timeout=15)
    req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
    sock.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    status = head.split(b"\r\n")[0].decode()
    return sock, status, rest


def ws_send(sock, text):
    payload = text.encode()
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    n = len(payload)
    if n < 126:
        hdr = struct.pack("!BB", 0x81, 0x80 | n)
    elif n < 65536:
        hdr = struct.pack("!BBH", 0x81, 0x80 | 126, n)
    else:
        hdr = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
    sock.sendall(hdr + mask + masked)


def ws_frames(sock, seconds=14):
    sock.settimeout(1.0)
    end = time.time() + seconds
    buf = b""
    while time.time() < end:
        try:
            d = sock.recv(65536)
            if not d:
                break
            buf += d
        except socket.timeout:
            continue
        except OSError:
            break
    return buf


sock, status, _ = ws_connect(HOST, PORT, f"/ws?token={TOKEN}")
print(f"  WS handshake -> {status}")
ws_send(sock, json.dumps({"type": "run", "cmd": "echo LJ_NET_WS_WORKS; whoami; pwd"}))
raw = ws_frames(sock)
texts, i = [], 0
while i < len(raw):
    b1, b2 = raw[i], raw[i + 1]
    ln = b2 & 0x7F
    off = 2
    if ln == 126:
        ln = struct.unpack("!H", raw[i + 2:i + 4])[0]; off = 4
    elif ln == 127:
        ln = struct.unpack("!Q", raw[i + 2:i + 10])[0]; off = 10
    payload = raw[i + off:i + off + ln]
    if b1 & 0x0F in (1, 2):
        try:
            texts.append(json.loads(payload.decode("utf-8", "replace")))
        except ValueError:
            pass
    i += off + ln

out = "".join(t.get("data", "") for t in texts if t.get("type") == "output")
print(f"  frames decoded: {[t.get('type') for t in texts]}")
print(f"  terminal output contains LJ_NET_WS_WORKS: {'LJ_NET_WS_WORKS' in out}")
print(f"  contains liuxd: {'liuxd' in out}")
print(f"  sample: {out[:200]!r}")
sock.close()

print()
print("=== 5. websocket with a BAD token must be closed ===")
sock2, status2, _ = ws_connect(HOST, PORT, "/ws?token=wrong-token")
print(f"  WS handshake -> {status2}")
tail = ws_frames(sock2, 6)
print(f"  bytes received after handshake: {len(tail)} "
      f"(0 or a close frame = rejected)")
if tail:
    op = tail[0] & 0x0F
    print(f"  first frame opcode: {op} (8 = close, 1 = text)")
sock2.close()

print()
print("=" * 64)
print(f"URL: http://{HOST}:{PORT}/#token={TOKEN}")
print("=" * 64)