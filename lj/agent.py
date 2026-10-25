"""lj.agent -- the far side's worker: the file and shell operations.

The browser asks for something; the far side carries it out and sends
the answer back. Everything here is a REQUEST-RESPONSE pair, never an
open stream: the far side polls for work and posts the result, so a
dropped connection costs one operation rather than a session.

WHAT THIS IS NOT
----------------
This is not a PTY. There is no interactive program with a controlling
terminal, no windowing, no job control. A command runs to completion
and its output comes back whole.

That is a deliberate limit. A real interactive terminal needs a PTY, a
resize channel, a signal channel and flow control -- and a browser
that emulates one well is xterm.js, which is a large dependency to
carry. What this offers instead is: run a command, get its output, and
see it with colour, which is what a person actually wants from a box
they cannot reach.

SECURITY -- READ THIS
---------------------
Whoever holds the token can read, write, move and DELETE files on the
far side, and can RUN COMMANDS there. That is the whole point of the
tool, and it means the token is now a SHELL CREDENTIAL, not a
messaging credential.

So: the token is 256 bits of CSPRNG, compared in constant time, and
it should travel over HTTPS or over a network you control. Treat it
like a password for the far side.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import subprocess
import sys
import time

# How much of a command's output to keep. A command that prints a
# gigabyte must not take the far side down with it, and the browser
# cannot show a gigabyte either.
MAX_OUTPUT = 512 * 1024

# How long a command may run before it is killed.
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 600.0

# The largest file the browser may ask to read into an editor.
MAX_EDIT_BYTES = 4 * 1024 * 1024

# The largest file the far side will send back as a download.
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024


class OpError(Exception):
    """An operation failed in a way the browser should be told about."""


# ------------------------------------------------------------------ paths

def resolve(root: str, path: str) -> str:
    """Turn a browser-supplied path into an absolute one under `root`.

    The browser sends paths relative to the far side's home. Anything
    that would escape the root is refused: `..` segments, an absolute
    path, and a symlink that points outside.

    This is a jail, not a security boundary -- the far side runs the
    user's own commands, which can leave the jail by design. It exists
    so that a mistyped path cannot delete the wrong thing by accident.
    """
    root = os.path.abspath(os.path.expanduser(root))
    if not path:
        return root
    path = os.path.expanduser(path)
    if os.path.isabs(path):
        candidate = os.path.abspath(path)
    else:
        candidate = os.path.abspath(os.path.join(root, path))

    # Compare with the trailing separator so that "/root2" is not
    # accepted as being inside "/root".
    root_with_sep = root.rstrip(os.sep) + os.sep
    if candidate != root and not candidate.startswith(root_with_sep):
        raise OpError(f"path is outside {root}: {path}")
    return candidate


def _real(path: str) -> str:
    """The path with symlinks resolved, for the containment check."""
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def check_inside(root: str, path: str) -> None:
    """Refuse a path whose symlinks lead outside the root."""
    real_root = _real(os.path.abspath(os.path.expanduser(root)))
    real = _real(path)
    root_with_sep = real_root.rstrip(os.sep) + os.sep
    if real != real_root and not real.startswith(root_with_sep):
        raise OpError(f"path leads outside {real_root}: {path}")


# ------------------------------------------------------------------- ops

def op_ls(args: dict, root: str) -> dict:
    """List a directory."""
    target = resolve(root, args.get("path", "."))
    check_inside(root, target)
    if not os.path.isdir(target):
        raise OpError(f"not a directory: {args.get('path', '.')}")

    show_hidden = bool(args.get("hidden"))
    entries = []
    try:
        names = os.listdir(target)
    except PermissionError:
        raise OpError(f"permission denied: {args.get('path', '.')}")
    except OSError as e:
        raise OpError(str(e))

    for name in sorted(names, key=lambda s: (not s.startswith("."), s.lower())):
        if not show_hidden and name.startswith("."):
            continue
        full = os.path.join(target, name)
        try:
            st = os.lstat(full)
        except OSError:
            continue
        kind = "d" if stat.S_ISDIR(st.st_mode) else (
            "l" if stat.S_ISLNK(st.st_mode) else "f")
        entries.append({
            "name": name,
            "kind": kind,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "mode": stat.S_IMODE(st.st_mode),
        })
    return {"path": args.get("path", "."), "entries": entries}


def op_read(args: dict, root: str) -> dict:
    """Read a file into the editor."""
    target = resolve(root, args.get("path", ""))
    check_inside(root, target)
    if os.path.isdir(target):
        raise OpError("that is a directory")
    try:
        size = os.path.getsize(target)
    except OSError as e:
        raise OpError(str(e))
    if size > MAX_EDIT_BYTES:
        raise OpError(f"too big to edit ({size} bytes, limit {MAX_EDIT_BYTES})")
    try:
        with open(target, "rb") as fh:
            data = fh.read()
    except OSError as e:
        raise OpError(str(e))
    try:
        text = data.decode("utf-8")
        binary = False
    except UnicodeDecodeError:
        text = base64.b64encode(data).decode("ascii")
        binary = True
    return {"path": args.get("path", ""), "text": text, "binary": binary,
            "size": size}


def op_write(args: dict, root: str) -> dict:
    """Write the editor's contents back to a file."""
    target = resolve(root, args.get("path", ""))
    check_inside(root, target)
    text = args.get("text", "")
    binary = bool(args.get("binary"))
    if binary:
        try:
            data = base64.b64decode(text)
        except Exception as e:
            raise OpError(f"not valid base64: {e}")
    else:
        data = text.encode("utf-8")
    if len(data) > MAX_EDIT_BYTES:
        raise OpError(f"too big to write ({len(data)} bytes)")
    parent = os.path.dirname(target)
    if parent and not os.path.isdir(parent):
        raise OpError(f"no such directory: {os.path.dirname(args.get('path', ''))}")
    try:
        with open(target, "wb") as fh:
            fh.write(data)
    except OSError as e:
        raise OpError(str(e))
    return {"path": args.get("path", ""), "size": len(data)}


def op_mkdir(args: dict, root: str) -> dict:
    target = resolve(root, args.get("path", ""))
    check_inside(root, target)
    try:
        os.makedirs(target, exist_ok=bool(args.get("parents", True)))
    except OSError as e:
        raise OpError(str(e))
    return {"path": args.get("path", "")}


def op_delete(args: dict, root: str) -> dict:
    """Delete a file or a directory."""
    target = resolve(root, args.get("path", ""))
    check_inside(root, target)
    if target == os.path.abspath(os.path.expanduser(root)):
        raise OpError("refusing to delete the root")
    if not os.path.lexists(target):
        raise OpError(f"no such file: {args.get('path', '')}")
    if os.path.isdir(target) and not os.path.islink(target):
        if not args.get("recursive"):
            raise OpError("that is a directory -- ask for it recursively")
        try:
            shutil.rmtree(target)
        except OSError as e:
            raise OpError(str(e))
    else:
        try:
            os.remove(target)
        except OSError as e:
            raise OpError(str(e))
    return {"path": args.get("path", "")}


def op_move(args: dict, root: str) -> dict:
    src = resolve(root, args.get("path", ""))
    dst = resolve(root, args.get("to", ""))
    check_inside(root, src)
    check_inside(root, dst)
    if not os.path.lexists(src):
        raise OpError(f"no such file: {args.get('path', '')}")
    parent = os.path.dirname(dst)
    if parent and not os.path.isdir(parent):
        raise OpError(f"no such directory: {os.path.dirname(args.get('to', ''))}")
    try:
        shutil.move(src, dst)
    except (OSError, shutil.Error) as e:
        raise OpError(str(e))
    return {"path": args.get("path", ""), "to": args.get("to", "")}


def op_copy(args: dict, root: str) -> dict:
    src = resolve(root, args.get("path", ""))
    dst = resolve(root, args.get("to", ""))
    check_inside(root, src)
    check_inside(root, dst)
    if not os.path.lexists(src):
        raise OpError(f"no such file: {args.get('path', '')}")
    if os.path.isdir(src):
        try:
            shutil.copytree(src, dst, dirs_exist_ok=bool(args.get("overwrite")))
        except (OSError, shutil.Error) as e:
            raise OpError(str(e))
    else:
        parent = os.path.dirname(dst)
        if parent and not os.path.isdir(parent):
            raise OpError(f"no such directory: {os.path.dirname(args.get('to', ''))}")
        if os.path.exists(dst) and not args.get("overwrite"):
            raise OpError(f"already there: {args.get('to', '')}")
        try:
            shutil.copy2(src, dst)
        except OSError as e:
            raise OpError(str(e))
    return {"path": args.get("path", ""), "to": args.get("to", "")}


def op_stat(args: dict, root: str) -> dict:
    target = resolve(root, args.get("path", ""))
    try:
        st = os.stat(target)
    except OSError as e:
        raise OpError(str(e))
    return {
        "path": args.get("path", ""),
        "size": st.st_size,
        "mtime": st.st_mtime,
        "mode": stat.S_IMODE(st.st_mode),
        "is_dir": stat.S_ISDIR(st.st_mode),
    }


# ----------------------------------------------------------------- shell

def op_exec(args: dict, root: str) -> dict:
    """Run a command and return its output.

    The command runs with the far side's own environment, in the given
    directory. It is a plain subprocess, not a shell: the caller sends
    the program and its arguments separately, so nothing is re-parsed
    by a shell and quoting is the caller's problem.

    Colour is kept: the output is bytes, and ANSI sequences survive
    untouched, so the browser can render them.
    """
    argv = args.get("argv") or []
    if isinstance(argv, str):
        raise OpError("argv must be a list, not a string")
    if not argv:
        raise OpError("no command given")
    cmd = str(argv[0])
    rest = [str(a) for a in argv[1:]]

    cwd = root
    if args.get("cwd"):
        cwd = resolve(root, args["cwd"])
        check_inside(root, cwd)
        if not os.path.isdir(cwd):
            raise OpError(f"no such directory: {args['cwd']}")

    try:
        timeout = float(args.get("timeout", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    timeout = max(1.0, min(timeout, MAX_TIMEOUT))

    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    env["LANG"] = env.get("LANG", "C.UTF-8")

    started = time.time()
    try:
        proc = subprocess.run(
            [cmd] + rest,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        output = proc.stdout or b""
        code = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired as e:
        output = e.stdout or b""
        code = 124
        timed_out = True
    except FileNotFoundError:
        raise OpError(f"no such command: {cmd}")
    except PermissionError:
        raise OpError(f"not executable: {cmd}")
    except OSError as e:
        raise OpError(str(e))

    truncated = len(output) > MAX_OUTPUT
    if truncated:
        output = output[:MAX_OUTPUT]

    return {
        "code": code,
        "output": base64.b64encode(output).decode("ascii"),
        "bytes": len(output),
        "truncated": truncated,
        "timed_out": timed_out,
        "secs": round(time.time() - started, 3),
        "cwd": args.get("cwd") or ".",
    }


# -------------------------------------------------------------- registry

OPS = {
    "ls": op_ls,
    "read": op_read,
    "write": op_write,
    "mkdir": op_mkdir,
    "delete": op_delete,
    "move": op_move,
    "copy": op_copy,
    "stat": op_stat,
    "exec": op_exec,
}


def run_op(op: str, args: dict, root: str) -> dict:
    """Run one operation and return its result.

    The result always has the same shape, so the browser does not have
    to guess: `ok` says whether it worked, and `error` explains why
    not.
    """
    fn = OPS.get(op)
    if fn is None:
        return {"ok": False, "error": f"no such operation: {op}"}
    try:
        data = fn(args or {}, root)
    except OpError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:                      # noqa: BLE001
        # An unexpected failure is still a failure the browser must be
        # told about, not a crash that takes the agent down.
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    out = {"ok": True}
    out.update(data)
    return out


def handle(raw: str, root: str) -> str:
    """Handle one request and return the JSON response.

    Never raises: the far side must answer every request, or the
    browser waits forever.
    """
    try:
        request = json.loads(raw)
    except ValueError as e:
        return json.dumps({"ok": False, "error": f"bad request: {e}"})
    if not isinstance(request, dict):
        return json.dumps({"ok": False, "error": "bad request: not an object"})

    req_id = request.get("id")
    op = request.get("op", "")
    args = request.get("args") or {}

    result = run_op(op, args, root)
    result["id"] = req_id
    result["op"] = op
    try:
        return json.dumps(result)
    except (TypeError, ValueError) as e:
        return json.dumps({"ok": False, "id": req_id, "op": op,
                           "error": f"cannot encode the result: {e}"})
