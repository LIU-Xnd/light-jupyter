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
- **`tools/build_msi.py` is the build to use**, not `pyappdist build`.
  It runs the stages one at a time, switches the UI set and calls wix
  itself, because pyappdist cannot express what the installer needs:
  - pyappdist only grows a UI when `[tool.pyappdist.targets].license`
    names an .rtf file. Without one the MSI has no `<UI>` section at
    all and Windows shows a bare progress bar that closes with nothing
    said -- no finish page, no folder choice.
  - it then hardcodes `WixUI_Minimal`, which has no folder page. The
    stock `WixUI_InstallDir` is the same set plus a "Destination
    Folder" page and a browse dialog, so the .wxs is edited between
    `gen-wix` and the wix build.
  - running the stages by hand means the files the .wxs refers to by
    bare name (the licence RTF and the product icon) are not staged
    for it, so the script copies them next to the .wxs itself.
- **The UI extension has to be fetched.** wix looks for it under
  `<cwd>/.wix/extensions/<id>/<version>/` -- relative to the working
  directory, not the user's home -- and the WiX install ships only an
  empty placeholder. The real payload is the NuGet package
  `wixtoolset.ui.wixext`; the build script downloads it if it is
  missing. `wix extension add -g` does not do it: it needs the network
  and does nothing when the package is already unpacked this way.
- `INSTALLFOLDER` is the public property, so a custom folder is
  `msiexec /i lj.msi INSTALLFOLDER=D:\somewhere\lj`. The directory
  page binds to `WIXUI_INSTALLDIR` and writes it back there.
- The MSI is unsigned, so Windows warns about an unknown publisher.
  `pyappdist` has a `sign:` stage for when there is a certificate.
- The build prints
  `Error compiling 'python\tcl\tix8.4.3\pref\WmDefault.py'...` and
  carries on. That file belongs to the bundled Python, not to lj, and
  the mixed tabs and spaces are upstream's. It does not stop the build
  and it does not affect the installer.

## The Linux binary

`tools/build_linux_binary.py` freezes the dialer into one file with
PyInstaller, so it can run on a machine with no Python, no conda and no
pip -- which is the whole point of the dialer side.

Things that are not obvious:

- **It has to be built on Linux.** PyInstaller is not a cross-compiler,
  and a binary built on Windows cannot run on Linux at all.
- **Build it on the machine it will run on, or an older one.** The
  binary links against the glibc of the system that built it, and glibc
  only ever goes forwards. Built on Ubuntu 20.04 it needs GLIBC_2.14,
  so it runs on 20.04 and everything newer; built on 24.04 it would not
  run on 20.04.
- **A fully static build needs musl**, which needs root: `musl-tools`
  is not installed and `sudo` wants a password. Docker would do it but
  the socket is `root:docker` and the user is not in that group. The
  glibc dependency is low enough that this does not matter in practice.
- **The window is excluded.** tkinter is Windows-only here, and
  excluding it keeps the binary smaller.
- The entry point is `tools/freeze_entry.py`, not `lj.dialer:main` --
  PyInstaller wants a script.

## Scope

`lj` carries messages. It is not a transport for byte streams, and there
is no PTY, shell or file access. `lj/selftest.py` asserts this and will
fail if a `--raw` / `--pty` / `--shell` flag or a `/raw` / `/ws` endpoint
appears. Do not widen it without changing that decision on purpose.
