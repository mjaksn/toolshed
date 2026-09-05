/*
 * apitrace.c  -  A self-contained Win32 API call tracer.
 *
 * Launches a target executable as a debuggee and logs calls made to the
 * *exported* functions of the modules you select (default: kernel32.dll and
 * ntdll.dll). It works like a tiny debugger: it plants software breakpoints
 * (INT3 / 0xCC) on every function entry point of interest, and on each hit it
 * records the thread, the module!function name, the return address, and the
 * first few argument slots, then transparently steps over the breakpoint and
 * re-arms it.
 *
 * Build (native, matching bitness of the targets you want to trace):
 *     cl /W4 apitrace.c            (MSVC)
 *     x86_64-w64-mingw32-gcc -O2 -o apitrace64.exe apitrace.c   (64-bit MinGW)
 *     i686-w64-mingw32-gcc  -O2 -o apitrace32.exe apitrace.c    (32-bit MinGW)
 *
 * Usage:
 *     apitrace [options] -- <target.exe> [target args...]
 *
 *   -m <module>   Module (DLL) to hook. Repeatable. Matched case-insensitively
 *                 against the DLL's internal export name (e.g. KERNEL32.dll).
 *                 Defaults to kernel32.dll + ntdll.dll if none given.
 *   -f <substr>   Only hook exported functions whose name contains <substr>
 *                 (case-insensitive). Handy to cut noise, e.g. -f File.
 *   -o <file>     Write the trace to <file> instead of stdout.
 *   -s            Best-effort: try to render pointer arguments that point at
 *                 readable ASCII / UTF-16 text as quoted strings.
 *   -h            Help.
 *
 * IMPORTANT LIMITATIONS (read these):
 *   - Bitness must match. A 64-bit apitrace can only trace 64-bit targets and
 *     vice-versa. The program checks the loaded module's PE magic and skips
 *     mismatched modules.
 *   - Only *exported* functions are seen. Internal helper calls, inlined code,
 *     and functions resolved past the export table are not visible. This is the
 *     fundamental limit of breakpoint-on-export tracing.
 *   - Software breakpoints have a small multithreading race: for the brief
 *     window in which a breakpoint is removed to be stepped over, another
 *     thread could execute that address without being logged. For a robust
 *     production tracer you would suspend the other threads during the step, or
 *     use an inline-hooking engine (Microsoft Detours, MinHook). See notes at
 *     the bottom of this file.
 *   - It is slow. Every hooked call traps into this debugger. Narrow the set
 *     with -m / -f when you can.
 *
 * This is a debugging / reverse-engineering / software-analysis tool, the
 * Windows analogue of strace. It only observes and logs; it does not modify the
 * traced program's behavior.
 */

#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <stdint.h>

/* ----- portability shims between the 32- and 64-bit builds --------------- */
#ifdef _WIN64
  #define IP_REG(ctx)  ((ctx).Rip)
  #define SP_REG(ctx)  ((ctx).Rsp)
  typedef unsigned long long uword;
  #define UWORD_FMT "0x%016llx"
#else
  #define IP_REG(ctx)  ((ctx).Eip)
  #define SP_REG(ctx)  ((ctx).Esp)
  typedef unsigned long uword;
  #define UWORD_FMT "0x%08lx"
#endif

/* ----- global state ------------------------------------------------------ */

typedef struct {
    uintptr_t addr;        /* absolute VA of the function entry in the target */
    BYTE      orig;        /* original byte we replaced with 0xCC             */
    char      name[192];   /* "MODULE!Function"                               */
} Breakpoint;

typedef struct {
    DWORD     tid;         /* thread that is mid-single-step                  */
    uintptr_t addr;        /* breakpoint address to re-arm after the step     */
} PendingStep;

static Breakpoint  *g_bp        = NULL;
static size_t       g_bp_count  = 0;
static size_t       g_bp_cap    = 0;

static PendingStep *g_pend      = NULL;
static size_t       g_pend_count= 0;
static size_t       g_pend_cap  = 0;

static HANDLE       g_hProc     = NULL;   /* handle to the traced process     */
static FILE        *g_out       = NULL;   /* where the trace goes             */
static int          g_seen_sysbp= 0;      /* consumed the initial loader BP?  */
static int          g_render_str= 0;      /* -s : try to render string args   */

/* module / function filters ------------------------------------------------ */
#define MAX_MODULES 32
static const char *g_modules[MAX_MODULES];
static int         g_module_count = 0;
static const char *g_func_filter  = NULL;

/* ----- small helpers ----------------------------------------------------- */

static void die(const char *msg) {
    fprintf(stderr, "apitrace: %s (err %lu)\n", msg, GetLastError());
    exit(1);
}

static int ieq_sub(const char *hay, const char *needle) {
    /* case-insensitive substring test */
    size_t nl = strlen(needle);
    if (nl == 0) return 1;
    for (; *hay; hay++) {
        size_t i = 0;
        while (i < nl && hay[i] &&
               tolower((unsigned char)hay[i]) == tolower((unsigned char)needle[i]))
            i++;
        if (i == nl) return 1;
    }
    return 0;
}

static int read_mem(uintptr_t addr, void *buf, SIZE_T len) {
    SIZE_T got = 0;
    return ReadProcessMemory(g_hProc, (LPCVOID)addr, buf, len, &got) && got == len;
}

static int write_byte(uintptr_t addr, BYTE b) {
    SIZE_T put = 0;
    if (!WriteProcessMemory(g_hProc, (LPVOID)addr, &b, 1, &put) || put != 1)
        return 0;
    FlushInstructionCache(g_hProc, (LPCVOID)addr, 1);
    return 1;
}

static Breakpoint *find_bp(uintptr_t addr) {
    for (size_t i = 0; i < g_bp_count; i++)
        if (g_bp[i].addr == addr) return &g_bp[i];
    return NULL;
}

static void add_bp(uintptr_t addr, const char *name) {
    BYTE orig;
    if (find_bp(addr)) return;                 /* aliased export, already set */
    if (!read_mem(addr, &orig, 1)) return;     /* unreadable, skip           */
    if (!write_byte(addr, 0xCC)) return;

    if (g_bp_count == g_bp_cap) {
        g_bp_cap = g_bp_cap ? g_bp_cap * 2 : 1024;
        g_bp = (Breakpoint *)realloc(g_bp, g_bp_cap * sizeof(*g_bp));
        if (!g_bp) die("out of memory (breakpoints)");
    }
    g_bp[g_bp_count].addr = addr;
    g_bp[g_bp_count].orig = orig;
    strncpy(g_bp[g_bp_count].name, name, sizeof(g_bp[0].name) - 1);
    g_bp[g_bp_count].name[sizeof(g_bp[0].name) - 1] = '\0';
    g_bp_count++;
}

/* pending single-step bookkeeping (keyed by thread id) -------------------- */

static void pend_push(DWORD tid, uintptr_t addr) {
    if (g_pend_count == g_pend_cap) {
        g_pend_cap = g_pend_cap ? g_pend_cap * 2 : 16;
        g_pend = (PendingStep *)realloc(g_pend, g_pend_cap * sizeof(*g_pend));
        if (!g_pend) die("out of memory (pending)");
    }
    g_pend[g_pend_count].tid  = tid;
    g_pend[g_pend_count].addr = addr;
    g_pend_count++;
}

static int pend_pop(DWORD tid, uintptr_t *addr_out) {
    for (size_t i = 0; i < g_pend_count; i++) {
        if (g_pend[i].tid == tid) {
            *addr_out = g_pend[i].addr;
            g_pend[i] = g_pend[--g_pend_count];   /* swap-remove */
            return 1;
        }
    }
    return 0;
}

/* ----- PE export enumeration -------------------------------------------- */
/*
 * Read the export directory of a module that is already mapped in the target,
 * and plant a breakpoint on every named, non-forwarded export that passes the
 * module/function filters.
 */
static void hook_module_exports(uintptr_t base, const char *modname) {
    IMAGE_DOS_HEADER dos;
    if (!read_mem(base, &dos, sizeof(dos)) || dos.e_magic != IMAGE_DOS_SIGNATURE)
        return;

    /* Read the NT headers. We read a generous fixed chunk that covers both the
     * 32- and 64-bit optional headers, then branch on the magic field. */
    BYTE nthdr[sizeof(IMAGE_NT_HEADERS64)];
    uintptr_t nt_va = base + dos.e_lfanew;
    if (!read_mem(nt_va, nthdr, sizeof(nthdr)))
        return;
    if (*(DWORD *)nthdr != IMAGE_NT_SIGNATURE)
        return;

    WORD magic = *(WORD *)(nthdr + 4 + sizeof(IMAGE_FILE_HEADER)); /* Optional.Magic */

    IMAGE_DATA_DIRECTORY expdir;
#ifdef _WIN64
    if (magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC) return;  /* bitness mismatch */
    expdir = ((IMAGE_NT_HEADERS64 *)nthdr)->OptionalHeader
                 .DataDirectory[IMAGE_DIRECTORY_ENTRY_EXPORT];
#else
    if (magic != IMAGE_NT_OPTIONAL_HDR32_MAGIC) return;  /* bitness mismatch */
    expdir = ((IMAGE_NT_HEADERS32 *)nthdr)->OptionalHeader
                 .DataDirectory[IMAGE_DIRECTORY_ENTRY_EXPORT];
#endif
    if (expdir.VirtualAddress == 0 || expdir.Size == 0)
        return;                                          /* no exports        */

    IMAGE_EXPORT_DIRECTORY ed;
    if (!read_mem(base + expdir.VirtualAddress, &ed, sizeof(ed)))
        return;
    if (ed.NumberOfNames == 0 || ed.AddressOfFunctions == 0)
        return;

    /* Pull the three parallel arrays out of the target's memory. */
    DWORD  n_names  = ed.NumberOfNames;
    DWORD *rva_funcs = (DWORD *)malloc((size_t)ed.NumberOfFunctions * sizeof(DWORD));
    DWORD *rva_names = (DWORD *)malloc((size_t)n_names * sizeof(DWORD));
    WORD  *ordinals  = (WORD  *)malloc((size_t)n_names * sizeof(WORD));
    if (!rva_funcs || !rva_names || !ordinals) { free(rva_funcs); free(rva_names); free(ordinals); return; }

    int ok =
        read_mem(base + ed.AddressOfFunctions,    rva_funcs, (size_t)ed.NumberOfFunctions * sizeof(DWORD)) &&
        read_mem(base + ed.AddressOfNames,        rva_names, (size_t)n_names * sizeof(DWORD)) &&
        read_mem(base + ed.AddressOfNameOrdinals, ordinals,  (size_t)n_names * sizeof(WORD));

    DWORD exp_lo = expdir.VirtualAddress;
    DWORD exp_hi = expdir.VirtualAddress + expdir.Size;   /* forwarder range  */
    size_t before = g_bp_count;

    for (DWORD i = 0; ok && i < n_names; i++) {
        char fname[256];
        if (!read_mem(base + rva_names[i], fname, sizeof(fname))) continue;
        fname[sizeof(fname) - 1] = '\0';

        if (g_func_filter && !ieq_sub(fname, g_func_filter)) continue;

        WORD  ord     = ordinals[i];
        if (ord >= ed.NumberOfFunctions) continue;
        DWORD func_rva = rva_funcs[ord];
        if (func_rva == 0) continue;
        if (func_rva >= exp_lo && func_rva < exp_hi) continue; /* forwarder, not code */

        char label[192];
        snprintf(label, sizeof(label), "%s!%s", modname, fname);
        add_bp(base + func_rva, label);
    }

    free(rva_funcs); free(rva_names); free(ordinals);
    fprintf(g_out, "[apitrace] hooked %zu exports in %s\n",
            g_bp_count - before, modname);
    fflush(g_out);
}

/* Read the module's own internal name from its export directory Name field.
 * This is more reliable than the debug event's lpImageName. */
static int module_internal_name(uintptr_t base, char *out, size_t out_sz) {
    IMAGE_DOS_HEADER dos;
    if (!read_mem(base, &dos, sizeof(dos)) || dos.e_magic != IMAGE_DOS_SIGNATURE)
        return 0;
    BYTE nthdr[sizeof(IMAGE_NT_HEADERS64)];
    if (!read_mem(base + dos.e_lfanew, nthdr, sizeof(nthdr))) return 0;
    if (*(DWORD *)nthdr != IMAGE_NT_SIGNATURE) return 0;
    WORD magic = *(WORD *)(nthdr + 4 + sizeof(IMAGE_FILE_HEADER));
    IMAGE_DATA_DIRECTORY expdir;
#ifdef _WIN64
    if (magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC) return 0;
    expdir = ((IMAGE_NT_HEADERS64 *)nthdr)->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_EXPORT];
#else
    if (magic != IMAGE_NT_OPTIONAL_HDR32_MAGIC) return 0;
    expdir = ((IMAGE_NT_HEADERS32 *)nthdr)->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_EXPORT];
#endif
    if (!expdir.VirtualAddress) return 0;
    IMAGE_EXPORT_DIRECTORY ed;
    if (!read_mem(base + expdir.VirtualAddress, &ed, sizeof(ed))) return 0;
    if (!ed.Name) return 0;
    if (!read_mem(base + ed.Name, out, out_sz)) return 0;
    out[out_sz - 1] = '\0';
    return 1;
}

static int want_module(const char *modname) {
    for (int i = 0; i < g_module_count; i++)
        if (ieq_sub(modname, g_modules[i])) return 1;
    return 0;
}

/* ----- argument rendering ------------------------------------------------ */

/* Best-effort: if a value looks like a pointer to readable text, print it. */
static void render_maybe_string(uword val) {
    if (!g_render_str || val == 0) return;

    char a[64];
    if (read_mem((uintptr_t)val, a, sizeof(a))) {
        int printable = 1, len = 0;
        for (; len < (int)sizeof(a) && a[len]; len++)
            if (a[len] < 0x20 || (unsigned char)a[len] > 0x7e) { printable = 0; break; }
        if (printable && len >= 3) { fprintf(g_out, " \"%.*s\"", len, a); return; }
    }
    wchar_t w[32];
    if (read_mem((uintptr_t)val, w, sizeof(w))) {
        int printable = 1, len = 0;
        for (; len < 31 && w[len]; len++)
            if (w[len] < 0x20 || w[len] > 0x7e) { printable = 0; break; }
        if (printable && len >= 3) { fprintf(g_out, " L\"%.*ls\"", len, w); return; }
    }
}

static void log_call(const Breakpoint *bp, DWORD tid, CONTEXT *ctx) {
    uintptr_t sp = (uintptr_t)SP_REG(*ctx);
    uword ret = 0;
    read_mem(sp, &ret, sizeof(ret));            /* [SP] = return address */

    fprintf(g_out, "[tid %5lu] %-40s ret=" UWORD_FMT " args:",
            tid, bp->name, (uword)ret);

#ifdef _WIN64
    /* x64 calling convention: first four integer args in RCX,RDX,R8,R9,
     * remaining args on the stack past the 32-byte shadow space. */
    uword regs[4] = { (uword)ctx->Rcx, (uword)ctx->Rdx, (uword)ctx->R8, (uword)ctx->R9 };
    for (int i = 0; i < 4; i++) {
        fprintf(g_out, " " UWORD_FMT, regs[i]);
        render_maybe_string(regs[i]);
    }
    for (int i = 0; i < 2; i++) {               /* a couple of stack args    */
        uword v = 0;
        if (read_mem(sp + 0x28 + (uintptr_t)i * sizeof(uword), &v, sizeof(v))) {
            fprintf(g_out, " " UWORD_FMT, v);
            render_maybe_string(v);
        }
    }
#else
    /* x86 __stdcall/__cdecl: all args on the stack after the return address. */
    for (int i = 0; i < 6; i++) {
        uword v = 0;
        if (read_mem(sp + sizeof(uword) + (uintptr_t)i * sizeof(uword), &v, sizeof(v))) {
            fprintf(g_out, " " UWORD_FMT, v);
            render_maybe_string(v);
        }
    }
#endif
    fputc('\n', g_out);
    fflush(g_out);
}

/* ----- breakpoint hit handling ------------------------------------------ */

static DWORD on_breakpoint(const DEBUG_EVENT *ev) {
    uintptr_t addr = (uintptr_t)ev->u.Exception.ExceptionRecord.ExceptionAddress;
    Breakpoint *bp = find_bp(addr);

    if (!bp) {
        /* First breakpoint is the system loader breakpoint: swallow it once.
         * Anything else unknown is the target's own breakpoint: pass it on. */
        if (!g_seen_sysbp) { g_seen_sysbp = 1; return DBG_CONTINUE; }
        return DBG_EXCEPTION_NOT_HANDLED;
    }

    HANDLE hThread = OpenThread(THREAD_ALL_ACCESS, FALSE, ev->dwThreadId);
    if (!hThread) return DBG_CONTINUE;

    CONTEXT ctx;
    ctx.ContextFlags = CONTEXT_ALL;
    if (GetThreadContext(hThread, &ctx)) {
        log_call(bp, ev->dwThreadId, &ctx);

        /* Step over: restore the original byte, rewind IP onto it, set the
         * trap flag so we get a single-step exception after it executes, and
         * remember to re-arm this breakpoint on that step. */
        write_byte(addr, bp->orig);
        IP_REG(ctx) = (uword)addr;
        ctx.EFlags |= 0x100;               /* TF */
        SetThreadContext(hThread, &ctx);
        pend_push(ev->dwThreadId, addr);
    }
    CloseHandle(hThread);
    return DBG_CONTINUE;
}

static DWORD on_single_step(const DEBUG_EVENT *ev) {
    uintptr_t addr;
    if (pend_pop(ev->dwThreadId, &addr)) {
        write_byte(addr, 0xCC);            /* re-arm the breakpoint          */

        /* Clear the trap flag so the thread does not keep single-stepping.  */
        HANDLE hThread = OpenThread(THREAD_ALL_ACCESS, FALSE, ev->dwThreadId);
        if (hThread) {
            CONTEXT ctx; ctx.ContextFlags = CONTEXT_CONTROL;
            if (GetThreadContext(hThread, &ctx)) {
                ctx.EFlags &= ~0x100u;
                SetThreadContext(hThread, &ctx);
            }
            CloseHandle(hThread);
        }
        return DBG_CONTINUE;
    }
    return DBG_EXCEPTION_NOT_HANDLED;       /* not ours */
}

/* ----- the debug loop ---------------------------------------------------- */

static int trace(void) {
    DEBUG_EVENT ev;
    int running = 1, exit_code = 0;

    while (running && WaitForDebugEvent(&ev, INFINITE)) {
        DWORD status = DBG_CONTINUE;

        switch (ev.dwDebugEventCode) {

        case CREATE_PROCESS_DEBUG_EVENT: {
            g_hProc = ev.u.CreateProcessInfo.hProcess;
            uintptr_t base = (uintptr_t)ev.u.CreateProcessInfo.lpBaseOfImage;
            char name[64];
            if (module_internal_name(base, name, sizeof(name)) && want_module(name))
                hook_module_exports(base, name);
            if (ev.u.CreateProcessInfo.hFile) CloseHandle(ev.u.CreateProcessInfo.hFile);
            break;
        }

        case LOAD_DLL_DEBUG_EVENT: {
            uintptr_t base = (uintptr_t)ev.u.LoadDll.lpBaseOfDll;
            char name[64];
            if (module_internal_name(base, name, sizeof(name)) && want_module(name))
                hook_module_exports(base, name);
            if (ev.u.LoadDll.hFile) CloseHandle(ev.u.LoadDll.hFile);
            break;
        }

        case EXCEPTION_DEBUG_EVENT: {
            DWORD code = ev.u.Exception.ExceptionRecord.ExceptionCode;
            if (code == EXCEPTION_BREAKPOINT || code == 0x4000001F /*WX86 BP*/)
                status = on_breakpoint(&ev);
            else if (code == EXCEPTION_SINGLE_STEP)
                status = on_single_step(&ev);
            else
                status = DBG_EXCEPTION_NOT_HANDLED;  /* real fault, let app see it */
            break;
        }

        case EXIT_PROCESS_DEBUG_EVENT:
            exit_code = (int)ev.u.ExitProcess.dwExitCode;
            fprintf(g_out, "[apitrace] target exited, code %d\n", exit_code);
            running = 0;
            break;

        case OUTPUT_DEBUG_STRING_EVENT:
        default:
            break;
        }

        if (!ContinueDebugEvent(ev.dwProcessId, ev.dwThreadId, status))
            die("ContinueDebugEvent failed");
    }
    return exit_code;
}

/* ----- command line ------------------------------------------------------ */

static void usage(const char *prog) {
    fprintf(stderr,
        "Usage: %s [options] -- <target.exe> [args...]\n"
        "  -m <module>  hook this DLL (repeatable; default kernel32.dll ntdll.dll)\n"
        "  -f <substr>  only hook functions whose name contains <substr>\n"
        "  -o <file>    write trace to <file> (default stdout)\n"
        "  -s           try to render pointer args as strings\n"
        "  -h           this help\n", prog);
}

int main(int argc, char **argv) {
    const char *out_path = NULL;
    int i = 1;
    for (; i < argc; i++) {
        if (strcmp(argv[i], "--") == 0) { i++; break; }
        else if (strcmp(argv[i], "-m") == 0 && i + 1 < argc) {
            if (g_module_count < MAX_MODULES) g_modules[g_module_count++] = argv[++i];
        }
        else if (strcmp(argv[i], "-f") == 0 && i + 1 < argc) g_func_filter = argv[++i];
        else if (strcmp(argv[i], "-o") == 0 && i + 1 < argc) out_path = argv[++i];
        else if (strcmp(argv[i], "-s") == 0) g_render_str = 1;
        else if (strcmp(argv[i], "-h") == 0) { usage(argv[0]); return 0; }
        else { fprintf(stderr, "unknown option: %s\n", argv[i]); usage(argv[0]); return 2; }
    }
    if (i >= argc) { usage(argv[0]); return 2; }

    if (g_module_count == 0) {                 /* sensible defaults */
        g_modules[g_module_count++] = "kernel32.dll";
        g_modules[g_module_count++] = "ntdll.dll";
    }

    g_out = stdout;
    if (out_path) {
        g_out = fopen(out_path, "w");
        if (!g_out) { perror("fopen"); return 1; }
    }

    /* Rebuild the target command line from the remaining argv, quoting args
     * that contain spaces. CreateProcess wants a single mutable string. */
    char cmdline[8192]; cmdline[0] = '\0';
    size_t used = 0;
    for (; i < argc; i++) {
        int need_q = strchr(argv[i], ' ') != NULL;
        int n = snprintf(cmdline + used, sizeof(cmdline) - used, "%s%s%s%s",
                         used ? " " : "", need_q ? "\"" : "", argv[i], need_q ? "\"" : "");
        if (n < 0 || (size_t)n >= sizeof(cmdline) - used) { fprintf(stderr, "command line too long\n"); return 1; }
        used += (size_t)n;
    }

    STARTUPINFOA si; ZeroMemory(&si, sizeof(si)); si.cb = sizeof(si);
    PROCESS_INFORMATION pi; ZeroMemory(&pi, sizeof(pi));

    if (!CreateProcessA(NULL, cmdline, NULL, NULL, FALSE,
                        DEBUG_ONLY_THIS_PROCESS, NULL, NULL, &si, &pi))
        die("CreateProcess failed (check the target path)");

    fprintf(g_out, "[apitrace] launched pid %lu: %s\n", pi.dwProcessId, cmdline);
    fflush(g_out);

    int code = trace();

    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    if (g_out && g_out != stdout) fclose(g_out);
    return code;
}

/*
 * Making this robust / production-grade:
 *   - Multithreading correctness: before stepping over a breakpoint, suspend
 *     every other thread of the target (enumerate via CreateToolhelp32Snapshot
 *     with TH32CS_SNAPTHREAD), then resume them after re-arming. That closes
 *     the race where another thread slips over the temporarily-removed 0xCC.
 *   - Argument decoding: this tracer logs raw slots. To print typed, named
 *     arguments (like API Monitor) you need per-function prototypes; drive it
 *     from a signature table keyed by MODULE!Function.
 *   - Fewer traps / lower overhead: for hot code, replace software breakpoints
 *     with an inline-hooking engine. Microsoft Detours and MinHook both install
 *     a trampoline so the logging runs in-process without a debugger round-trip.
 *   - Following child processes: pass DEBUG_PROCESS instead of
 *     DEBUG_ONLY_THIS_PROCESS and handle CREATE_PROCESS events for children.
 */
