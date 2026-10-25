# AGENTS.md — conventions for this repo

## README

**Do not describe retired versions.** When a version is superseded, its
description is removed from the README rather than kept for history. The
git history and the tags are where the history lives; the README
describes the software as it is now.

Concretely: no "v1.x was ..." paragraph, no "the old way was ..." notes,
no "previously it did ..." asides. If a behaviour is gone, it is simply
not there.

## Versioning

The number in `pyproject.toml`, the fallback `__version__` in
`lj/__init__.py`, the installed distribution metadata and the git tag
must all agree. `python -m lj.selftest` checks the first three and fails
if they differ, so bump all of them in the same commit.

- a commit → patch number (`2.7.1` → `2.7.2`)
- a push with a new feature → minor number (`2.7.x` → `2.8.0`)
- a push with a breaking change → major number (`2.x` → `3.0.0`)

Tag the release and push the tag with the branch.

## The self-test must not leave anything running

Every child process is registered with `_remember()` and reaped through
`atexit`. A run that is killed outright cannot run `atexit`, so the pids
are written to a file that the NEXT run cleans up (`_reap_stale`).

A child whose stdout is a pipe must have that pipe drained, or the child
blocks on a full buffer and never exits. For a child that prints a banner
and is not read, redirect to a file.

## Platform

Windows is a supported target, and it is where the bugs live. Anything
that touches a path, a byte stream or a subprocess has to work there:

- `openssl` is a C program: give it forward slashes, or it cannot open
  the file
- `os.path.splitext` on a path with mixed separators leaves the directory
  in the stem
- decode a child's output with `errors="replace"` -- a Windows console
  code page is not valid UTF-8, and a strict decode kills the reader
  thread, which makes the parent's read block forever
- `os.kill(pid, 0)` is a probe on POSIX but SIGNALISES on Windows; use
  `tasklist` there

## The MSI installer

`pyappdist build` produces `appdist/windows/dist/lj-<version>.msi`. It
bundles a Python runtime, so the installer works on a machine with
nothing on it.

Things that are not obvious and cost time to rediscover:

- **WiX v7 needs the OSMF EULA accepted** and stops with
  `WIX7015`. v6 has no such licence question and produces the same
  output, so v6 is what is used. The toolset can be unpacked without
  installing it:
  ```
  msiexec /a wix-cli-x64.msi /qn TARGETDIR=<dir>
  ```
  A normal install fails with `1603` because the MSI's launch condition
  wants .NET Desktop Runtime 3.10 exactly, and a newer one is present.
  The administrative extract sidesteps the check.
- **pyappdist needs the dependency manager named**, even when there are
  no dependencies: `manager = "requirements.txt"` in
  `[tool.pyappdist]`, with an empty `requirements.txt` saying so. The
  accepted values are `uv`, `poetry`, `pipenv`, `pdm` and
  `requirements.txt` -- `pip` is NOT one of them.
- **The entry point is `pyappdist.exe`**, not `python -m pyappdist`;
  the package has no `__main__`.
- **The build tree is `.appdist-build/`** and holds a whole Python
  runtime -- about 150 MB. It is gitignored, and it must stay that way:
  an early commit included it, 5501 files, and the history had to be
  rewritten.
- The pipeline can be run a stage at a time -- `build-wheels`,
  `fetch-runtime`, `build-image`, `build-launchers`, `gen-wix` -- which
  is how you rebuild the MSI without re-downloading the runtime.
- The MSI is unsigned, so Windows warns about an unknown publisher.
  `pyappdist` has a `sign:` stage for when there is a certificate.

## Scope

`lj` carries messages. It is not a transport for byte streams, and there
is no PTY, shell or file access. `lj/selftest.py` asserts this and will
fail if a `--raw` / `--pty` / `--shell` flag or a `/raw` / `/ws` endpoint
appears. Do not widen it without changing that decision on purpose.
