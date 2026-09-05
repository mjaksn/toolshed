# apitrace

A Win32 API call tracer, the Windows analogue of `strace`. It launches a program as a
debuggee, plants software breakpoints on the entry point of every exported function in
the DLLs you select, and logs each call: thread, `MODULE!Function`, return address, and
the first few argument slots.

One file, pure `ctypes`, no dependencies and no build step. `apitrace.py` is the whole
program.

It only observes. It does not alter what the traced program does.

## Requirements

- **Windows.** `init_win()` exits on any other platform.
- **Python 3, with bitness matching the target.** A 64-bit interpreter traces 64-bit
  targets and a 32-bit one traces 32-bit targets. Mismatched modules are skipped rather
  than misread, by comparing the module's PE magic against the interpreter's pointer
  size, so a mismatch shows up as nothing being hooked.
- **Nothing to install.** Standard library only.

Launching a child, which is the only thing this does, needs no special rights. Attaching
to an existing process would, but that is not implemented.

## Running

```
python apitrace.py [options] -- <target.exe> [target args...]
```

Everything after `--` is the target and its arguments.

| Option | Meaning |
| --- | --- |
| `-m <module>` | Hook this DLL's exports. Repeatable, case-insensitive. Default: `kernel32.dll ntdll.dll` |
| `-f <substr>` | Only hook functions whose name contains this substring, case-insensitive |
| `-o <file>` | Write the trace to a file instead of stdout |
| `-s` | Best effort: render pointer arguments that point at readable ASCII or UTF-16 as quoted strings |
| `-h`, `--help`, `/?` | Help |

Exit status is the target's own exit code, or 2 when no target was given and 1 on an
unknown option.

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

That run is real, from Windows 11 with 64-bit Python 3.12. Without `-f` it would try
every named export the two default modules have, which on this Windows 11 is 1,693 in
`kernel32.dll` and 2,516 in `ntdll.dll`, so roughly 4,200 breakpoints and a trace to
match. An unfiltered run of even `cmd.exe /c exit` takes minutes rather than seconds.
That is what the filters are for.

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

## Checking it still works

No Windows CI exists for this, and the tool cannot run at all off Windows. Three levels,
cheapest first:

```bash
python -m py_compile apitrace.py          # syntax, any platform
```

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
have already caused hard-to-diagnose failures.

`ruff.toml` in this directory turns off three stylistic rules for this file only, with
the reasoning written out in the file. The program is committed exactly as written, byte
for byte, so that a future revision arrives as a clean diff.
