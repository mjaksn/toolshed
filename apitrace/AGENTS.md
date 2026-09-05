# AGENTS.md

Guidance for working on **apitrace.py**, a self-contained Win32 API call tracer
written in pure `ctypes`. Read this before editing: several things here look
harmless to change but silently corrupt the debugger.

[README.md](README.md) covers what the tool is and how to run it. This file is
about changing it.

## What this is

A single-file, dependency-free Python program that traces a Windows process the
way `strace` traces a Linux one. It launches a target as a debuggee, plants
software breakpoints (`INT3` / `0xCC`) on the entry point of every *exported*
function in selected DLLs, and logs each call (thread, `MODULE!Function`, return
address, first few argument slots) before transparently stepping over the
breakpoint and re-arming it.

There is no build step. `apitrace.py` is the whole project.

## Architecture (control flow)

1. `main()` -> `init_win()` binds kernel32 and pins every function's
   `argtypes`/`restype`, then `CreateProcessW(..., DEBUG_ONLY_THIS_PROCESS)`.
2. `trace_loop()` pumps `WaitForDebugEvent` / `ContinueDebugEvent`:
   - `CREATE_PROCESS` / `LOAD_DLL` -> `module_internal_name()` +
     `want_module()`; if wanted, `hook_module_exports()` parses the module's PE
     export directory out of target memory and calls `add_bp()` per export.
   - `EXCEPTION` with breakpoint code -> `on_breakpoint()`.
   - `EXCEPTION` with single-step code -> `on_single_step()`.
   - `EXIT_PROCESS` -> record exit code, break.
3. `on_breakpoint()` logs the call, then does the **step-over dance** (see
   invariants). `on_single_step()` completes it by re-arming.

Global state lives in module-level `g_*` dicts/flags: `g_hProc`, `g_bp`
(addr -> {orig, name}), `g_pending` (tid -> addr awaiting re-arm),
`g_seen_sysbp`, `g_render_str`, `g_modules`, `g_func_filter`, `g_out`.

## Invariants that must not break

These are the load-bearing details. Each has caused, or would cause, a
hard-to-diagnose failure.

1. **Every kernel32 function needs `argtypes` + `restype`.** Set in
   `init_win()`. Without them ctypes assumes C `int` args and truncates 64-bit
   handles/addresses to 32 bits. Symptom: works on 32-bit, corrupts memory or
   fails `ReadProcessMemory` on 64-bit. When adding a Win32 call, add its
   prototype in `init_win()` first.

2. **Never instantiate `CONTEXT()` directly, use `make_context()`.** On x64,
   `GetThreadContext` requires the buffer 16-byte aligned; plain ctypes objects
   are not guaranteed aligned. `make_context()` carves an aligned view out of a
   larger buffer and returns `(ctx, raw)`.

3. **Keep the backing buffer alive.** `make_context()` returns `raw` for a
   reason: `ctx` is a `from_address` view into it. If `raw` is garbage-collected
   while `ctx` is in use, the context memory is freed under you. Always bind both
   (`ctx, _raw = make_context(...)`) and keep `_raw` in scope through the last
   `Set/GetThreadContext`.

4. **CONTEXT layout must match the Windows ABI exactly.** `CONTEXT64` must be
   1232 bytes, `CONTEXT32` must be 716. Field order/offsets are load-bearing.
   Verify after any change (see Testing in the README).

5. **The step-over protocol is exact.** In `on_breakpoint()`: restore the
   original byte, rewind IP onto the instruction, set the trap flag
   (`EFlags |= 0x100`), record `g_pending[tid] = addr`. In `on_single_step()`:
   re-arm `0xCC`, then clear the trap flag. Skipping the trap-flag clear makes
   the thread single-step forever; skipping the re-arm loses the breakpoint.

6. **Skip forwarder exports.** In `hook_module_exports()`, an export whose RVA
   falls inside the export directory range (`exp_lo <= rva < exp_hi`) is a
   forwarder string (e.g. kernel32 -> ntdll), not code. Planting `0xCC` there
   corrupts the forwarder. Keep this check.

7. **Keep `SKIP_EXPORTS` = {dbgbreakpoint, dbguserbreakpoint}.** Their entry
   point *is* an `int 3`. Overlaying a breakpoint and stepping over it would
   re-trigger a breakpoint instead of a single-step and recurse. The loader also
   uses these for the initial debug break.

8. **Swallow the initial system breakpoint exactly once.** `g_seen_sysbp` gates
   the first unrecognized breakpoint (the loader breakpoint) with `DBG_CONTINUE`;
   later unknown breakpoints belong to the target and must return
   `DBG_EXCEPTION_NOT_HANDLED` so the app sees its own `int 3`.

9. **`read_mem()` returns `None` on partial/failed reads.** Every caller must
   handle `None` (page boundaries and unmapped pages are normal). `read_cstr()`
   already shrinks its read size to cope with straddling a page edge.

10. **Handle values are ints.** ctypes `c_void_p` fields yield Python ints (or
    `None` for null). Pass them straight to functions whose argtype is `HANDLE` /
    `c_void_p`; don't wrap or cast unnecessarily.

## Known limitations (intentional, documented in-file)

The README states these for a reader. If you "fix" one, update the in-file footer
of `apitrace.py`, the README, and this section together.

- **Only exported functions are visible.** Fundamental to breakpoint-on-export
  tracing.
- **Software-breakpoint multithreading race.** Between restoring the original
  byte and re-arming, another thread could execute that address unlogged. See
  the footer of `apitrace.py` for the fix (suspend sibling threads during the
  step) and the alternative (inline trampoline hooks via frida/Detours/MinHook).
- **Slow.** Every hooked call round-trips through this debugger.

## Extension points

- **Typed arguments:** `log_call()` prints raw slots. To print named/typed args
  (like API Monitor), add a signature table keyed by `MODULE!Function` and format
  in `log_call()` / `render_str()`.
- **Follow child processes:** switch `DEBUG_ONLY_THIS_PROCESS` to
  `DEBUG_PROCESS` and handle `CREATE_PROCESS` events for children (each brings
  its own `hProcess`; `g_hProc` currently assumes one target).
- **New Win32 calls:** prototype in `init_win()` (invariant 1) before use.

## Style

Plain stdlib + `ctypes`, no third-party deps, and keep it that way so the file
stays copy-and-run. Match the existing naming: `g_*` for globals, snake_case
functions, Windows type aliases (`DWORD`, `HANDLE`, ...) at the top.

The file is committed byte for byte as written. `ruff.toml` here disables three
stylistic rules for it rather than reformatting working code; the reasoning is in
that file. If you make a real change, keep the diff to the change.
