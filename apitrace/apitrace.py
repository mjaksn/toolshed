#!/usr/bin/env python3
r"""
apitrace.py  -  A self-contained Win32 API call tracer (pure ctypes, no deps).

Python port of apitrace.c. It launches a target executable as a debuggee and
logs calls made to the *exported* functions of the modules you select
(default: kernel32.dll and ntdll.dll). It works like a tiny debugger: it plants
software breakpoints (INT3 / 0xCC) on every function entry point of interest,
and on each hit it records the thread, MODULE!Function, the return address, and
the first few argument slots, then steps over the breakpoint and re-arms it.

Run on Windows with a Python whose bitness matches the target:
    py -3        apitrace.py -- notepad.exe            (64-bit Python -> 64-bit exe)
    py -3-32     apitrace.py -- legacy32.exe           (32-bit Python -> 32-bit exe)

Usage:
    apitrace.py [options] -- <target.exe> [target args...]

  -m <module>   Hook this DLL's exports. Repeatable. Matched case-insensitively
                (e.g. kernel32.dll, user32.dll). Default: kernel32.dll ntdll.dll
  -f <substr>   Only hook functions whose name contains <substr> (case-insens.).
  -o <file>     Write the trace to <file> instead of stdout.
  -s            Best-effort: render pointer args that point at readable ASCII /
                UTF-16 text as quoted strings.
  -h / --help / /?   This help.

Everything after -- is the target and its arguments.

LIMITATIONS (same as the C version):
  * Bitness of the Python interpreter must match the target.
  * Only *exported* functions are traced (not inlined/internal calls).
  * Software breakpoints have a small multithreading race in the step-over
    window; for robustness suspend sibling threads during the step or use an
    inline-hooking engine. See notes at the bottom of this file.
  * It is slow: every hooked call traps into this debugger. Narrow with -m / -f.

This is a debugging / reverse-engineering tool (the Windows analogue of strace).
It only observes; it does not alter the traced program's behavior.
"""

import sys
import ctypes

# ---- fixed-width Windows typedefs --------------------------------------------
DWORD     = ctypes.c_uint32
WORD      = ctypes.c_uint16
LPVOID    = ctypes.c_void_p
HANDLE    = ctypes.c_void_p
ULONG_PTR = ctypes.c_size_t
DWORD64   = ctypes.c_uint64

IS64 = ctypes.sizeof(ctypes.c_void_p) == 8
PTR  = 8 if IS64 else 4

# ---- constants ---------------------------------------------------------------
INFINITE                    = 0xFFFFFFFF
DEBUG_ONLY_THIS_PROCESS     = 0x00000002

EXCEPTION_DEBUG_EVENT       = 1
CREATE_THREAD_DEBUG_EVENT   = 2
CREATE_PROCESS_DEBUG_EVENT  = 3
EXIT_THREAD_DEBUG_EVENT     = 4
EXIT_PROCESS_DEBUG_EVENT    = 5
LOAD_DLL_DEBUG_EVENT        = 6
UNLOAD_DLL_DEBUG_EVENT      = 7
OUTPUT_DEBUG_STRING_EVENT   = 8
RIP_EVENT                   = 9

DBG_CONTINUE                = 0x00010002
DBG_EXCEPTION_NOT_HANDLED   = 0x80010001

EXCEPTION_BREAKPOINT        = 0x80000003
EXCEPTION_SINGLE_STEP       = 0x80000004
STATUS_WX86_BREAKPOINT      = 0x4000001F
STATUS_WX86_SINGLE_STEP     = 0x4000001E

TRAP_FLAG                   = 0x100
THREAD_RIGHTS               = 0x0008 | 0x0010 | 0x0040  # GET|SET|QUERY context

IMAGE_DOS_SIGNATURE = 0x5A4D       # 'MZ'
IMAGE_NT_SIGNATURE  = 0x00004550   # 'PE\0\0'

# CONTEXT flag values differ per architecture.
if IS64:
    CTX_GET     = 0x00100001 | 0x00100002   # CONTROL | INTEGER
    CTX_CONTROL = 0x00100001
else:
    CTX_GET     = 0x00010001 | 0x00010002
    CTX_CONTROL = 0x00010001

# Exported functions we must NOT breakpoint: their entry point *is* an int3,
# so overlaying 0xCC and stepping would recurse. The loader also uses these for
# the initial debug break.
SKIP_EXPORTS = {"dbgbreakpoint", "dbguserbreakpoint"}

kernel32 = None  # set in init_win()

# ---- CONTEXT structures ------------------------------------------------------
class M128A(ctypes.Structure):
    _fields_ = [("Low", ctypes.c_uint64), ("High", ctypes.c_int64)]

class CONTEXT64(ctypes.Structure):
    _fields_ = [
        ("P1Home", DWORD64), ("P2Home", DWORD64), ("P3Home", DWORD64),
        ("P4Home", DWORD64), ("P5Home", DWORD64), ("P6Home", DWORD64),
        ("ContextFlags", DWORD), ("MxCsr", DWORD),
        ("SegCs", WORD), ("SegDs", WORD), ("SegEs", WORD),
        ("SegFs", WORD), ("SegGs", WORD), ("SegSs", WORD),
        ("EFlags", DWORD),
        ("Dr0", DWORD64), ("Dr1", DWORD64), ("Dr2", DWORD64),
        ("Dr3", DWORD64), ("Dr6", DWORD64), ("Dr7", DWORD64),
        ("Rax", DWORD64), ("Rcx", DWORD64), ("Rdx", DWORD64), ("Rbx", DWORD64),
        ("Rsp", DWORD64), ("Rbp", DWORD64), ("Rsi", DWORD64), ("Rdi", DWORD64),
        ("R8", DWORD64), ("R9", DWORD64), ("R10", DWORD64), ("R11", DWORD64),
        ("R12", DWORD64), ("R13", DWORD64), ("R14", DWORD64), ("R15", DWORD64),
        ("Rip", DWORD64),
        ("FltSave", ctypes.c_byte * 512),
        ("VectorRegister", M128A * 26),
        ("VectorControl", DWORD64),
        ("DebugControl", DWORD64), ("LastBranchToRip", DWORD64),
        ("LastBranchFromRip", DWORD64), ("LastExceptionToRip", DWORD64),
        ("LastExceptionFromRip", DWORD64),
    ]

class FLOATING_SAVE_AREA(ctypes.Structure):
    _fields_ = [
        ("ControlWord", DWORD), ("StatusWord", DWORD), ("TagWord", DWORD),
        ("ErrorOffset", DWORD), ("ErrorSelector", DWORD),
        ("DataOffset", DWORD), ("DataSelector", DWORD),
        ("RegisterArea", ctypes.c_byte * 80), ("Cr0NpxState", DWORD),
    ]

class CONTEXT32(ctypes.Structure):
    _fields_ = [
        ("ContextFlags", DWORD),
        ("Dr0", DWORD), ("Dr1", DWORD), ("Dr2", DWORD),
        ("Dr3", DWORD), ("Dr6", DWORD), ("Dr7", DWORD),
        ("FloatSave", FLOATING_SAVE_AREA),
        ("SegGs", DWORD), ("SegFs", DWORD), ("SegEs", DWORD), ("SegDs", DWORD),
        ("Edi", DWORD), ("Esi", DWORD), ("Ebx", DWORD), ("Edx", DWORD),
        ("Ecx", DWORD), ("Eax", DWORD),
        ("Ebp", DWORD), ("Eip", DWORD), ("SegCs", DWORD), ("EFlags", DWORD),
        ("Esp", DWORD), ("SegSs", DWORD),
        ("ExtendedRegisters", ctypes.c_byte * 512),
    ]

CONTEXT = CONTEXT64 if IS64 else CONTEXT32

# ---- DEBUG_EVENT structures --------------------------------------------------
class EXCEPTION_RECORD(ctypes.Structure):
    _fields_ = [
        ("ExceptionCode", DWORD), ("ExceptionFlags", DWORD),
        ("ExceptionRecord", LPVOID), ("ExceptionAddress", LPVOID),
        ("NumberParameters", DWORD), ("ExceptionInformation", ULONG_PTR * 15),
    ]

class EXCEPTION_DEBUG_INFO(ctypes.Structure):
    _fields_ = [("ExceptionRecord", EXCEPTION_RECORD), ("dwFirstChance", DWORD)]

class CREATE_THREAD_DEBUG_INFO(ctypes.Structure):
    _fields_ = [("hThread", HANDLE), ("lpThreadLocalBase", LPVOID),
                ("lpStartAddress", LPVOID)]

class CREATE_PROCESS_DEBUG_INFO(ctypes.Structure):
    _fields_ = [
        ("hFile", HANDLE), ("hProcess", HANDLE), ("hThread", HANDLE),
        ("lpBaseOfImage", LPVOID),
        ("dwDebugInfoFileOffset", DWORD), ("nDebugInfoSize", DWORD),
        ("lpThreadLocalBase", LPVOID), ("lpStartAddress", LPVOID),
        ("lpImageName", LPVOID), ("fUnicode", WORD),
    ]

class EXIT_THREAD_DEBUG_INFO(ctypes.Structure):
    _fields_ = [("dwExitCode", DWORD)]

class EXIT_PROCESS_DEBUG_INFO(ctypes.Structure):
    _fields_ = [("dwExitCode", DWORD)]

class LOAD_DLL_DEBUG_INFO(ctypes.Structure):
    _fields_ = [
        ("hFile", HANDLE), ("lpBaseOfDll", LPVOID),
        ("dwDebugInfoFileOffset", DWORD), ("nDebugInfoSize", DWORD),
        ("lpImageName", LPVOID), ("fUnicode", WORD),
    ]

class UNLOAD_DLL_DEBUG_INFO(ctypes.Structure):
    _fields_ = [("lpBaseOfDll", LPVOID)]

class OUTPUT_DEBUG_STRING_INFO(ctypes.Structure):
    _fields_ = [("lpDebugStringData", LPVOID),
                ("fUnicode", WORD), ("nDebugStringLength", WORD)]

class RIP_INFO(ctypes.Structure):
    _fields_ = [("dwError", DWORD), ("dwType", DWORD)]

class DEBUG_EVENT_UNION(ctypes.Union):
    _fields_ = [
        ("Exception", EXCEPTION_DEBUG_INFO),
        ("CreateThread", CREATE_THREAD_DEBUG_INFO),
        ("CreateProcessInfo", CREATE_PROCESS_DEBUG_INFO),
        ("ExitThread", EXIT_THREAD_DEBUG_INFO),
        ("ExitProcess", EXIT_PROCESS_DEBUG_INFO),
        ("LoadDll", LOAD_DLL_DEBUG_INFO),
        ("UnloadDll", UNLOAD_DLL_DEBUG_INFO),
        ("DebugString", OUTPUT_DEBUG_STRING_INFO),
        ("RipInfo", RIP_INFO),
    ]

class DEBUG_EVENT(ctypes.Structure):
    _fields_ = [
        ("dwDebugEventCode", DWORD), ("dwProcessId", DWORD),
        ("dwThreadId", DWORD), ("u", DEBUG_EVENT_UNION),
    ]

class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", DWORD), ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p), ("lpTitle", ctypes.c_wchar_p),
        ("dwX", DWORD), ("dwY", DWORD), ("dwXSize", DWORD), ("dwYSize", DWORD),
        ("dwXCountChars", DWORD), ("dwYCountChars", DWORD),
        ("dwFillAttribute", DWORD), ("dwFlags", DWORD),
        ("wShowWindow", WORD), ("cbReserved2", WORD),
        ("lpReserved2", LPVOID),
        ("hStdInput", HANDLE), ("hStdOutput", HANDLE), ("hStdError", HANDLE),
    ]

class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE),
                ("dwProcessId", DWORD), ("dwThreadId", DWORD)]

# ---- global tracer state -----------------------------------------------------
g_hProc      = None            # HANDLE (int) to the traced process
g_bp         = {}              # addr -> {"orig": int, "name": str}
g_pending    = {}              # tid  -> addr awaiting single-step re-arm
g_seen_sysbp = False
g_render_str = False
g_modules    = []              # module-name filters
g_func_filter = None
g_out        = None            # output stream

def out(line):
    g_out.write(line + "\n")
    g_out.flush()

# ---- Win32 binding -----------------------------------------------------------
def init_win():
    """Load kernel32 and pin argtypes/restypes. Setting these is mandatory:
    without them ctypes assumes int-sized args and silently truncates 64-bit
    handles and addresses."""
    global kernel32
    if sys.platform != "win32":
        sys.exit("apitrace: this tool only runs on Windows.")
    k = ctypes.WinDLL("kernel32", use_last_error=True)

    k.CreateProcessW.argtypes = [
        ctypes.c_wchar_p, LPVOID, LPVOID, LPVOID, ctypes.c_int, DWORD,
        LPVOID, ctypes.c_wchar_p,
        ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION)]
    k.CreateProcessW.restype = ctypes.c_int

    k.WaitForDebugEvent.argtypes = [ctypes.POINTER(DEBUG_EVENT), DWORD]
    k.WaitForDebugEvent.restype  = ctypes.c_int
    k.ContinueDebugEvent.argtypes = [DWORD, DWORD, DWORD]
    k.ContinueDebugEvent.restype  = ctypes.c_int

    k.ReadProcessMemory.argtypes  = [HANDLE, LPVOID, LPVOID, ctypes.c_size_t,
                                     ctypes.POINTER(ctypes.c_size_t)]
    k.ReadProcessMemory.restype   = ctypes.c_int
    k.WriteProcessMemory.argtypes = [HANDLE, LPVOID, LPVOID, ctypes.c_size_t,
                                     ctypes.POINTER(ctypes.c_size_t)]
    k.WriteProcessMemory.restype  = ctypes.c_int
    k.FlushInstructionCache.argtypes = [HANDLE, LPVOID, ctypes.c_size_t]
    k.FlushInstructionCache.restype  = ctypes.c_int

    k.OpenThread.argtypes = [DWORD, ctypes.c_int, DWORD]
    k.OpenThread.restype  = HANDLE
    k.GetThreadContext.argtypes = [HANDLE, ctypes.POINTER(CONTEXT)]
    k.GetThreadContext.restype  = ctypes.c_int
    k.SetThreadContext.argtypes = [HANDLE, ctypes.POINTER(CONTEXT)]
    k.SetThreadContext.restype  = ctypes.c_int
    k.CloseHandle.argtypes = [HANDLE]
    k.CloseHandle.restype  = ctypes.c_int
    kernel32 = k

# ---- process memory helpers --------------------------------------------------
def read_mem(addr, size):
    if not addr:
        return None
    buf = (ctypes.c_char * size)()
    n = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(g_hProc, addr, buf, size, ctypes.byref(n))
    if not ok or n.value != size:
        return None
    return buf.raw

def write_mem(addr, data):
    buf = ctypes.create_string_buffer(data, len(data))
    n = ctypes.c_size_t(0)
    ok = kernel32.WriteProcessMemory(g_hProc, addr, buf, len(data), ctypes.byref(n))
    if ok:
        kernel32.FlushInstructionCache(g_hProc, addr, len(data))
    return bool(ok) and n.value == len(data)

def read_word(addr):
    """Read one pointer-sized value from the target."""
    data = read_mem(addr, PTR)
    return None if data is None else int.from_bytes(data, "little")

def read_cstr(addr, maxlen=256):
    data = None
    for n in (maxlen, 128, 64, 32, 16, 8):   # shrink if we straddle a page edge
        data = read_mem(addr, n)
        if data is not None:
            break
    if data is None:
        return None
    z = data.find(b"\x00")
    if z >= 0:
        data = data[:z]
    return data.decode("ascii", "replace")

# ---- aligned CONTEXT allocation ---------------------------------------------
def make_context(flags):
    """CONTEXT must be 16-byte aligned for x64 GetThreadContext; ctypes objects
    are not guaranteed aligned, so carve an aligned view out of a bigger buffer.
    The backing buffer is returned and must be kept alive by the caller."""
    size = ctypes.sizeof(CONTEXT)
    raw = (ctypes.c_byte * (size + 16))()
    aligned = (ctypes.addressof(raw) + 15) & ~15
    ctx = CONTEXT.from_address(aligned)
    ctx.ContextFlags = flags
    return ctx, raw

def get_ip(ctx):
    return ctx.Rip if IS64 else ctx.Eip

def set_ip(ctx, val):
    if IS64:
        ctx.Rip = val
    else:
        ctx.Eip = val

def get_sp(ctx):
    return ctx.Rsp if IS64 else ctx.Esp

# ---- breakpoints -------------------------------------------------------------
def add_bp(addr, name):
    if addr in g_bp:
        return
    orig = read_mem(addr, 1)
    if orig is None:
        return
    if not write_mem(addr, b"\xCC"):
        return
    g_bp[addr] = {"orig": orig[0], "name": name}

# ---- PE export enumeration ---------------------------------------------------
def _export_dir(base):
    """Return (export_rva, export_size, name_rva) for a module mapped in the
    target, or None. Reads straight from the target's memory."""
    import struct
    dos = read_mem(base, 0x40)
    if not dos or struct.unpack_from("<H", dos, 0)[0] != IMAGE_DOS_SIGNATURE:
        return None
    e_lfanew = struct.unpack_from("<I", dos, 0x3C)[0]
    nt = read_mem(base + e_lfanew, 0x18)     # signature + IMAGE_FILE_HEADER
    if not nt or struct.unpack_from("<I", nt, 0)[0] != IMAGE_NT_SIGNATURE:
        return None
    size_opt = struct.unpack_from("<H", nt, 4 + 16)[0]   # SizeOfOptionalHeader
    opt = read_mem(base + e_lfanew + 0x18, max(size_opt, 120))
    if not opt:
        return None
    magic = struct.unpack_from("<H", opt, 0)[0]
    if magic == 0x20B:       # PE32+
        dd_off = 112
    elif magic == 0x10B:     # PE32
        dd_off = 96
    else:
        return None
    if (magic == 0x20B) != IS64:             # bitness mismatch -> skip
        return None
    exp_rva, exp_size = struct.unpack_from("<II", opt, dd_off)
    if not exp_rva:
        return None
    ed = read_mem(base + exp_rva, 40)
    if not ed:
        return None
    name_rva = struct.unpack_from("<I", ed, 12)[0]
    return exp_rva, exp_size, name_rva, ed

def module_internal_name(base):
    info = _export_dir(base)
    if not info:
        return None
    _, _, name_rva, _ = info
    if not name_rva:
        return None
    return read_cstr(base + name_rva, 64)

def hook_module_exports(base, modname):
    import struct
    info = _export_dir(base)
    if not info:
        return
    exp_rva, exp_size, _name_rva, ed = info
    # IMAGE_EXPORT_DIRECTORY: NumberOfFunctions/NumberOfNames/AddressOfFunctions/
    # AddressOfNames/AddressOfNameOrdinals are five DWORDs starting at offset 20.
    num_funcs, num_names, addr_funcs, addr_names, addr_ords = \
        struct.unpack_from("<IIIII", ed, 20)
    if not num_names or not addr_funcs:
        return

    funcs = read_mem(base + addr_funcs, num_funcs * 4)
    names = read_mem(base + addr_names, num_names * 4)
    ords_ = read_mem(base + addr_ords,  num_names * 2)
    if not (funcs and names and ords_):
        return

    exp_lo, exp_hi = exp_rva, exp_rva + exp_size
    before = len(g_bp)
    for i in range(num_names):
        name_ptr_rva = struct.unpack_from("<I", names, i * 4)[0]
        fname = read_cstr(base + name_ptr_rva)
        if not fname:
            continue
        if g_func_filter and g_func_filter.lower() not in fname.lower():
            continue
        if fname.lower() in SKIP_EXPORTS:
            continue
        ordinal = struct.unpack_from("<H", ords_, i * 2)[0]
        if ordinal >= num_funcs:
            continue
        func_rva = struct.unpack_from("<I", funcs, ordinal * 4)[0]
        if func_rva == 0:
            continue
        if exp_lo <= func_rva < exp_hi:      # forwarder string, not code
            continue
        add_bp(base + func_rva, "%s!%s" % (modname, fname))

    out("[apitrace] hooked %d exports in %s" % (len(g_bp) - before, modname))

def want_module(modname):
    if not modname:
        return False
    low = modname.lower()
    return any(m.lower() in low for m in g_modules)

# ---- argument rendering ------------------------------------------------------
def fmt_word(v):
    if v is None:
        return "?"
    return "0x%0*x" % (PTR * 2, v)

def render_str(v):
    if not g_render_str or not v:
        return ""
    data = read_mem(v, 64)
    if data:
        z = data.find(b"\x00")
        s = data[:z] if z >= 0 else data
        if len(s) >= 3 and all(0x20 <= b <= 0x7E for b in s):
            return ' "%s"' % s.decode("ascii", "replace")
    wdata = read_mem(v, 64)
    if wdata:
        chars = []
        for i in range(0, len(wdata) - 1, 2):
            cp = wdata[i] | (wdata[i + 1] << 8)
            if cp == 0:
                break
            chars.append(cp)
        if len(chars) >= 3 and all(0x20 <= c <= 0x7E for c in chars):
            return ' L"%s"' % "".join(chr(c) for c in chars)
    return ""

def log_call(bp, tid, ctx):
    sp = get_sp(ctx)
    ret = read_word(sp)
    if IS64:
        args = [ctx.Rcx, ctx.Rdx, ctx.R8, ctx.R9]
        args += [read_word(sp + 0x28 + i * 8) for i in range(2)]
    else:
        args = [read_word(sp + 4 + i * 4) for i in range(6)]
    rendered = " ".join(fmt_word(a) + render_str(a) for a in args)
    out("[tid %5d] %-40s ret=%s args: %s" % (tid, bp["name"], fmt_word(ret), rendered))

# ---- exception handlers ------------------------------------------------------
def on_breakpoint(ev):
    global g_seen_sysbp
    addr = ev.u.Exception.ExceptionRecord.ExceptionAddress
    bp = g_bp.get(addr)
    if bp is None:
        if not g_seen_sysbp:                 # initial loader breakpoint
            g_seen_sysbp = True
            return DBG_CONTINUE
        return DBG_EXCEPTION_NOT_HANDLED     # the target's own breakpoint

    tid = ev.dwThreadId
    hThread = kernel32.OpenThread(THREAD_RIGHTS, False, tid)
    if not hThread:
        return DBG_CONTINUE
    try:
        ctx, _raw = make_context(CTX_GET)
        if kernel32.GetThreadContext(hThread, ctypes.byref(ctx)):
            log_call(bp, tid, ctx)
            # Step over: restore original byte, rewind IP onto it, set trap flag,
            # remember to re-arm on the single-step that follows.
            write_mem(addr, bytes([bp["orig"]]))
            set_ip(ctx, addr)
            ctx.EFlags |= TRAP_FLAG
            kernel32.SetThreadContext(hThread, ctypes.byref(ctx))
            g_pending[tid] = addr
    finally:
        kernel32.CloseHandle(hThread)
    return DBG_CONTINUE

def on_single_step(ev):
    tid = ev.dwThreadId
    addr = g_pending.pop(tid, None)
    if addr is None:
        return DBG_EXCEPTION_NOT_HANDLED
    write_mem(addr, b"\xCC")                  # re-arm
    hThread = kernel32.OpenThread(THREAD_RIGHTS, False, tid)
    if hThread:
        ctx, _raw = make_context(CTX_CONTROL)
        if kernel32.GetThreadContext(hThread, ctypes.byref(ctx)):
            ctx.EFlags &= ~TRAP_FLAG          # stop single-stepping
            kernel32.SetThreadContext(hThread, ctypes.byref(ctx))
        kernel32.CloseHandle(hThread)
    return DBG_CONTINUE

# ---- debug loop --------------------------------------------------------------
def trace_loop():
    global g_hProc
    ev = DEBUG_EVENT()
    exit_code = 0
    while kernel32.WaitForDebugEvent(ctypes.byref(ev), INFINITE):
        status = DBG_CONTINUE
        code = ev.dwDebugEventCode

        if code == CREATE_PROCESS_DEBUG_EVENT:
            info = ev.u.CreateProcessInfo
            g_hProc = info.hProcess
            base = info.lpBaseOfImage
            name = module_internal_name(base)
            if name and want_module(name):
                hook_module_exports(base, name)
            if info.hFile:
                kernel32.CloseHandle(info.hFile)

        elif code == LOAD_DLL_DEBUG_EVENT:
            base = ev.u.LoadDll.lpBaseOfDll
            name = module_internal_name(base)
            if name and want_module(name):
                hook_module_exports(base, name)
            if ev.u.LoadDll.hFile:
                kernel32.CloseHandle(ev.u.LoadDll.hFile)

        elif code == EXCEPTION_DEBUG_EVENT:
            xc = ev.u.Exception.ExceptionRecord.ExceptionCode
            if xc in (EXCEPTION_BREAKPOINT, STATUS_WX86_BREAKPOINT):
                status = on_breakpoint(ev)
            elif xc in (EXCEPTION_SINGLE_STEP, STATUS_WX86_SINGLE_STEP):
                status = on_single_step(ev)
            else:
                status = DBG_EXCEPTION_NOT_HANDLED   # real fault -> let app see

        elif code == EXIT_PROCESS_DEBUG_EVENT:
            exit_code = ev.u.ExitProcess.dwExitCode
            out("[apitrace] target exited, code %d" % exit_code)
            kernel32.ContinueDebugEvent(ev.dwProcessId, ev.dwThreadId, status)
            break

        if not kernel32.ContinueDebugEvent(ev.dwProcessId, ev.dwThreadId, status):
            sys.exit("apitrace: ContinueDebugEvent failed (%d)" %
                     ctypes.get_last_error())
    return exit_code

# ---- command line ------------------------------------------------------------
HELP = __doc__

def parse_args(argv):
    global g_func_filter, g_render_str
    out_path = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            i += 1
            break
        elif a == "-m" and i + 1 < len(argv):
            g_modules.append(argv[i + 1]); i += 2
        elif a == "-f" and i + 1 < len(argv):
            g_func_filter = argv[i + 1]; i += 2
        elif a == "-o" and i + 1 < len(argv):
            out_path = argv[i + 1]; i += 2
        elif a == "-s":
            g_render_str = True; i += 1
        elif a in ("-h", "--help", "/?", "/h"):
            print(HELP); sys.exit(0)
        else:
            sys.exit("apitrace: unknown option %r (use -h)" % a)
    target = argv[i:]
    if not target:
        print(HELP); sys.exit(2)
    return out_path, target

def build_cmdline(target):
    parts = []
    for t in target:
        parts.append('"%s"' % t if " " in t else t)
    return " ".join(parts)

def main():
    global g_out
    init_win()
    out_path, target = parse_args(sys.argv[1:])

    if not g_modules:
        g_modules.extend(["kernel32.dll", "ntdll.dll"])

    g_out = open(out_path, "w") if out_path else sys.stdout

    cmdline = build_cmdline(target)
    buf = ctypes.create_unicode_buffer(cmdline, len(cmdline) + 1)
    si = STARTUPINFOW(); si.cb = ctypes.sizeof(si)
    pi = PROCESS_INFORMATION()

    ok = kernel32.CreateProcessW(None, buf, None, None, False,
                                 DEBUG_ONLY_THIS_PROCESS, None, None,
                                 ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        sys.exit("apitrace: CreateProcess failed (%d) - check the target path"
                 % ctypes.get_last_error())

    out("[apitrace] launched pid %d: %s" % (pi.dwProcessId, cmdline))
    try:
        code = trace_loop()
    finally:
        kernel32.CloseHandle(pi.hThread)
        kernel32.CloseHandle(pi.hProcess)
        if g_out is not sys.stdout:
            g_out.close()
    sys.exit(code)

if __name__ == "__main__":
    main()

# -----------------------------------------------------------------------------
# Making this robust / production-grade:
#   * Multithreading correctness: before stepping over a breakpoint, suspend the
#     target's other threads (CreateToolhelp32Snapshot + SuspendThread), then
#     resume them after re-arming. Closes the race where a second thread runs
#     over the temporarily-removed 0xCC.
#   * Typed arguments: this logs raw slots. To print named/typed args like API
#     Monitor, drive rendering from a per-function signature table keyed by
#     MODULE!Function.
#   * Lower overhead: for hot code, replace software breakpoints with an inline
#     trampoline hook (frida, or a Detours/MinHook DLL injected into the target).
#   * Child processes: pass DEBUG_PROCESS instead of DEBUG_ONLY_THIS_PROCESS and
#     handle CREATE_PROCESS events for spawned children.
# -----------------------------------------------------------------------------
