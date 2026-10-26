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
- **`WixUI_InstallDir` needs a property the package has to define.**
  The set's "Destination Folder" page has a PathEdit control bound to
  `WIXUI_INSTALLDIR`, and the set does not define that property. Without
  it the installer stops with
  `Error 2819: Control Folder on dialog InstallDirDlg needs a property
  linked to it` (wixtoolset/issues#818). So the package carries
  `<Property Id="WIXUI_INSTALLDIR" Value="INSTALLFOLDER" />`.
  The value is the directory's **Id**, not a reference to it: a value
  like `[INSTALLFOLDER]` is a property reference, which wix warns about
  (WIX1077) and the installer does not resolve.
- **A silent install does not prove the UI works.** Error 2819 only
  fires when the installer actually shows the folder page, so
  `msiexec /i ... /qn` succeeding says nothing about it. Check the MSI's
  Control and Property tables instead.
- The general procedure is written up as a skill:
  `python-package-release` -> `references/msi-installer-ui.md`, with
  `scripts/msi_ui.py` to switch the UI set and to check a built MSI.
- The MSI is unsigned, so Windows warns about an unknown publisher.
  `pyappdist` has a `sign:` stage for when there is a certificate.
- The build prints
  `Error compiling 'python\tcl\tix8.4.3\pref\WmDefault.py'...` and
  carries on. That file belongs to the bundled Python, not to lj, and
  the mixed tabs and spaces are upstream's. It does not stop the build
  and it does not affect the installer.

## The Linux binary

`tools/build_linux_binary.py` freezes the whole dispatcher into one
file with PyInstaller, so it can run on a machine with no Python, no
conda and no pip -- which is the whole point of the far side.

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
  PyInstaller wants a script. It freezes the DISPATCHER, not one
  subcommand: freezing `lj.dialer:main` gives a binary that can only
  be a dialer, and the far side's worker mode then does not exist in
  it. Freeze `lj.main` and the binary carries every subcommand.
- **The build needs the pyinstaller that belongs to the interpreter
  running it.** `shutil.which("pyinstaller")` finds the one on PATH,
  which is not necessarily the one in the environment the script is
  running under -- so run the build with the environment's own python:
  `~/miniconda3/envs/lj-dev/bin/python tools/build_linux_binary.py`.
  With the system python it says "pyinstaller is not installed" and
  stops.
- **A copy in the home directory is a copy, not a link.** `~/lj` is
  the frozen binary, put there so it can be run from anywhere. It has
  to be refreshed after a rebuild: `cp ~/LjDev/dist/linux/lj ~/lj`.

## The tags are history, and moving them breaks the versions

A tag names a release. A release has a version number in it, and the
version number is in `pyproject.toml` at that commit. So moving a tag
to a different commit makes the tag name and the version it installs
disagree -- `v3.0.0` pointing at a tree that says `4.0.0` -- and the
check in `lj/selftest.py` that the tag, the file and the installed
metadata agree stops being about the same thing.

So a tag is left where it is, and the sequence carries the history:
`v3.0.0` (https) -> `v3.0.1` (https removed) -> `v3.1.0` -> `v3.1.1`
-> `v4.0.0` (`lj remote` and `lj dialer` merged). The README describes
the product as it is now; the tags say what each release was.

The reason a version was removed rather than deprecated is in the
README: the traffic is plain HTTP, and the token is a shell credential,
so running it on a network you do not control is the thing to avoid.

## The Windows launcher has no console

The Windows launcher is built on `pythonw.exe` (PE subsystem 2), so it
has no console. Anything printed to stdout or stderr goes nowhere,
and the process appears to do nothing at all.

Two consequences:

- **With no arguments the dispatcher opens the window.** The Start Menu
  shortcut runs the launcher with no arguments; printing the usage
  there is invisible, so it opens the GUI instead. The console
  launcher still prints the usage.
- **A crash has to be shown in a message box.** A traceback to stderr
  from a pythonw process is invisible, so the dispatcher catches it and
  puts it in a `MessageBoxW`.

How to tell whether there is a console, on Windows:

```python
buffer = (ctypes.c_ulong * 8)()
count = kernel32.GetConsoleProcessList(buffer, len(buffer))
has_console = count > 0
```

`AttachConsole(ATTACH_PARENT_PROCESS)` is the tempting wrong answer:
it succeeds precisely when the process has NO console of its own and
can attach to the parent's -- the opposite of the question. Measured:
`python.exe` gives `GetConsoleProcessList` = 5, `pythonw.exe` gives 0;
`AttachConsole` gives false and true respectively.

## Scope

`lj` is a file manager and a command runner for a machine you cannot
reach, with a message queue on the side.

What it is not: there is no PTY, no byte stream, no interactive session,
no window size, no signal channel and no job control. A command runs to
completion and its output comes back whole.

The reason is the shape, not the feature list. Every operation is a
request that comes back whole, so a timeout costs one operation rather
than a session that has to be detected and rebuilt. A real interactive
terminal needs a PTY, a resize channel, a signal channel and flow
control, and a browser that emulates one properly is xterm.js -- a large
dependency to carry for a tool whose point is being small.

The command line is split into arguments the way a shell would split it,
but it is not handed to a shell: no expansion, no globbing, no pipes, no
redirection. The process is run directly.

`lj/selftest.py` asserts the boundary that is left: it fails if a
`/raw` / `/stream` / `/ws` endpoint appears, or if a PTY is opened
anywhere in the agent. Do not widen it without changing that decision on
purpose.

## The far side is ONE command and it is the worker

`lj dialer` is the far side: it polls for work, does it, and brings the
answers back. That is all it does.

It used to have two modes. There was a `--worker` flag, and without it
the command was a messenger -- push a message, or collect what the page
sends. The messenger is GONE, and so are the flags that drove it: `-m`,
`-f`, `--repeat`, `--listen`, `--show`, `--interactive`, `--poll`.

**Do not bring any of it back.** One command that does one thing is
easier to reason about than one command that does two things depending
on a flag, and the messenger was the half nobody used.

Three things follow from the removal and are easy to get wrong:

- **The endpoints that carried messages are gone: `/msg`, `/in` and
  `/send`.** They 404 now. The suite asserts that they 404 -- not
  that they are untested -- because a quiet success would let a stale
  client think it still works.
- **The page has no chat drawer, and the window has no send box.**
  The suite asserts the elements are ABSENT, for the same reason.
- **The flags must not come back quietly.** The suite reads the
  dialer's own help text and asserts that `--worker`, `-m`, `--message`,
  `--file`, `--repeat`, `--listen`, `--show` and `--interactive` are
  not in it.

There was a `--worker` flag until v5.0.0 and a `lj remote` command until
v4.0.0. They had the same arguments, the same target format, the same
token handling and the same "dial out, never accept" shape, so two
commands meant two parsers, two sets of flags, two help texts, and a
user who had to know which half of the far side they were running.

**`lj remote` must not quietly still work either.** The suite asserts
that the dispatcher answers "unknown command" for it.

## The token is a shell credential

Since 3.0.0 the token lets the holder read, write and delete files on
the far side and run commands there. It is a password for that machine,
not a messaging credential.

So: the token must travel over HTTPS or a network you control; the far
side's operations are confined to `--root`; and the account the far side
runs as is an account you would be willing to lose. Say so in the README
whenever the token's meaning changes -- it changed once already, and the
documentation is where a reader finds out.
