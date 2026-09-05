# apitrace

A Win32 API call tracer, the Windows analogue of `strace`. It launches a program as a
debuggee, plants software breakpoints on the entry point of every exported function in
the DLLs you select, and logs each call: thread, `MODULE!Function`, return address, and
the first few argument slots.

It only observes. It does not alter what the traced program does.

## Two implementations

`apitrace.c` is the original. `apitrace.py` is a port of it, and its docstring says so.
They do the same job by the same method, and on the same target they hook the same
exports and print the same shape of line.

| | `apitrace.c` | `apitrace.py` |
| --- | --- | --- |
| Needs | a C compiler, once | a Python 3 interpreter |
| Build | `cl /W4 apitrace.c` | none |
| Runs as | a native `.exe` | a script |
| Overhead | lower | higher, on top of an already slow method |

Reach for the C one if you are tracing anything busy, and the Python one if you want to
change what gets logged without setting up a toolchain. Neither has dependencies.

## Requirements

- **Windows.** The C one builds nowhere else. The Python one imports anywhere, which
  is deliberate so its structure layout can be checked off Windows, but `init_win()`
  exits as soon as it is actually run.
- **Bitness must match the target.** A 64-bit build traces 64-bit targets and a 32-bit
  build traces 32-bit ones. Mismatched modules are skipped rather than misread, by
  comparing the module's PE magic against the tracer's own pointer size, so a mismatch
  shows up as nothing being hooked rather than as garbage.
- **For `apitrace.py`:** Python 3, of matching bitness. Standard library only, nothing
  to install.
- **For `apitrace.c`:** any Windows C compiler. It uses only `windows.h` and the C
  standard library, and links against nothing beyond the default.

Launching a child, which is the only thing this does, needs no special rights. Attaching
to an existing process would, but that is not implemented.

## Building the C one

There is no makefile, because there is nothing to make beyond a single translation unit.
The commands are in the file's own header:

```
cl /W4 apitrace.c                                          MSVC
x86_64-w64-mingw32-gcc -O2 -o apitrace64.exe apitrace.c    64-bit MinGW
i686-w64-mingw32-gcc  -O2 -o apitrace32.exe apitrace.c     32-bit MinGW
```

The MSVC line was run for this README, from a `vcvars64.bat` shell, and produced a
working `apitrace64.exe`. It reports two `C4996` warnings, for `strncpy` and `fopen`,
which are Microsoft's suggestion to use its `_s` variants rather than anything wrong
with the code. Define `_CRT_SECURE_NO_WARNINGS` if you want them quiet.

## Running

```
apitrace64.exe          [options] -- <target.exe> [target args...]
python apitrace.py      [options] -- <target.exe> [target args...]
```

The options are the same either way. Everything after `--` is the target and its
arguments.

| Option | Meaning |
| --- | --- |
| `-m <module>` | Hook this DLL's exports. Repeatable, case-insensitive. Default: `kernel32.dll ntdll.dll` |
| `-f <substr>` | Only hook functions whose name contains this substring, case-insensitive |
| `-o <file>` | Write the trace to a file instead of stdout |
| `-s` | Best effort: render pointer arguments that point at readable ASCII or UTF-16 as quoted strings |
| `-h`, `--help`, `/?` | Help |

Exit status is the target's own exit code, or 2 when no target was given. On an unknown
option the C one exits 2 and the Python one exits 1, which is the one place the two
disagree on a documented interface.

## A worked example

```
> python apitrace.py -f CreateFile -- cmd.exe /c "echo hello"
[apitrace] launched pid 21164: cmd.exe /c "echo hello"
[apitrace] hooked 1 exports in ntdll.dll
[apitrace] hooked 11 exports in KERNEL32.dll
[tid  6116] ntdll.dll!NtCreateFile      ret=0x00007ffda363d1d8 args: 0x000000755e5be818 ...
hello
[apitrace] target exited, code 0
```

That run is real, from Windows 11 with 64-bit Python 3.12. Without `-f` the same command
hooks several thousand exports and the trace is correspondingly enormous, which is the
point of the filters.

If you run this from a shell that rewrites paths, such as git bash, note that `/c` is
mangled into `C:/` before it ever reaches the target. Use PowerShell or cmd, or quote it
so the shell leaves it alone. The symptom is a target that starts interactively instead
of running the command you gave it.

## Limitations

These are inherent to the approach rather than things left undone.

- **Only exported functions are visible.** Inlined code, internal helpers, and anything
  resolved past the export table are not traced.
- **There is a small multithreading race.** Between restoring the original byte and
  re-arming the breakpoint, another thread could run that address unlogged. The fix,
  suspending sibling threads across the step, and the alternative, inline trampoline
  hooks, are both described at the foot of `apitrace.py`.
- **It is slow.** Every hooked call traps into the debugger and back. Narrow the scope
  with `-m` and `-f`; the difference between filtered and unfiltered is large.

## Where the two implementations differ

Found by reading both and running both. None of it is a reason to prefer one over the
other for ordinary use, but it is the sort of thing that wastes an afternoon when you
hit it and did not know.

- **Non-ANSI target paths.** The C one calls `CreateProcessA`, so a target whose path
  will not survive the ANSI code page cannot be launched. The Python one calls
  `CreateProcessW` and is unaffected.
- **Exports near the end of a mapped region.** The C one reads a fixed 256 bytes for
  each export name and skips the export outright if that read fails, which it does when
  the name sits within 256 bytes of the end of a committed region. The Python's
  `read_cstr()` retries at 128, 64, 32, 16 and 8 bytes, so it recovers those names. The
  practical effect is that the C one can silently hook slightly fewer exports.
- **WOW64 single-step exceptions.** Both treat `0x4000001F`, the WX86 breakpoint, as a
  breakpoint, but only the Python also treats `0x4000001E`, the WX86 single step, as a
  single step. Since bitness has to match anyway, this should not arise in normal use;
  it is an asymmetry in the C one rather than a bug anyone will trip over.
- **Command line length.** The C one assembles the target command line into a fixed
  8192-byte buffer and refuses anything longer with `command line too long`. The Python
  has no such limit.
- **Thread handle rights.** The C one opens threads with `THREAD_ALL_ACCESS`; the Python
  asks only for the three context rights it needs.

One difference that is not a difference: the Python goes to some trouble to hand
`GetThreadContext` a 16-byte aligned `CONTEXT`, and the C one does not need to, because
`windows.h` already declares the structure with the required alignment.

## Checking it still works

No Windows CI exists for this, and neither implementation can run off Windows. Levels of
checking, cheapest first:

```bash
python -m py_compile apitrace.py          # syntax, any platform
```

```
cl /W4 apitrace.c                         # the C one, Windows only
```

A build at `/W4` is the C equivalent of the syntax check, and it is the only automated
check that half has. Two `C4996` warnings are expected; anything else is new.

```bash
# ABI layout, any platform. All Windows calls are deferred into init_win(), so the
# import succeeds on Linux and macOS too.
python - <<'EOF'
import ctypes, importlib.util
spec = importlib.util.spec_from_file_location("apitrace", "apitrace.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
assert ctypes.sizeof(m.CONTEXT64) == 1232
assert ctypes.sizeof(m.CONTEXT32) == 716
assert m.CONTEXT64.Rip.offset == 0xf8
assert m.DEBUG_EVENT.u.offset == 0x10
print("layout ok")
EOF
```

Run that second one after touching any `ctypes.Structure`. It catches a mis-sized
`CONTEXT` or `DEBUG_EVENT`, which otherwise shows up only as garbage register reads on
real hardware.

Behaviour has to be checked on a real Windows host. The example above is the smoke test:
if `NtCreateFile` lines appear and the target's own output still comes through, the
breakpoint and step-over machinery is intact.

## Notes for anyone editing it

[AGENTS.md](AGENTS.md) holds the architecture, the invariants that look safe to change
and are not, and the extension points. Read it first. Several of the details in there
have already caused hard-to-diagnose failures, and it marks which of them are about the
Python's use of `ctypes` and which apply to both files.

A change to the traced behaviour of one implementation is a change the other one wants
too, or the table above grows a row. Say which you did.

`ruff.toml` in this directory turns off three stylistic rules for this file only, with
the reasoning written out in the file. The program is committed exactly as written, byte
for byte, so that a future revision arrives as a clean diff.
