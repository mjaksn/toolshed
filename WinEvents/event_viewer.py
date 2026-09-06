"""Basic Windows Event Log viewer.

Select a log source from the dropdown, click "Get Last Hour" to load the
most recent hour of events into a time-sorted list, and double-click a row
to open the full event details in a separate window.

Requires: pywin32  (pip install pywin32)
"""

import ctypes
import datetime
import os
import queue
import sys
import threading
import tkinter as tk
import winreg
from tkinter import messagebox, ttk

import win32evtlog
import win32evtlogutil

EVENT_TYPE_NAMES = {
    win32evtlog.EVENTLOG_ERROR_TYPE: "Error",
    win32evtlog.EVENTLOG_WARNING_TYPE: "Warning",
    win32evtlog.EVENTLOG_INFORMATION_TYPE: "Information",
    win32evtlog.EVENTLOG_AUDIT_SUCCESS: "Audit Success",
    win32evtlog.EVENTLOG_AUDIT_FAILURE: "Audit Failure",
}

# Per-theme palettes. Level colors are darker on light backgrounds and
# lightened on dark backgrounds so they stay legible either way.
THEMES = {
    "light": {
        "bg": "#f0f0f0",
        "fg": "#1a1a1a",
        "field_bg": "#ffffff",
        "select_bg": "#0078d7",
        "select_fg": "#ffffff",
        "heading_bg": "#e1e1e1",
        "levels": {"Error": "#c62828", "Warning": "#e65100", "Audit Failure": "#c62828"},
    },
    "dark": {
        "bg": "#2b2b2b",
        "fg": "#e0e0e0",
        "field_bg": "#1e1e1e",
        "select_bg": "#264f78",
        "select_fg": "#ffffff",
        "heading_bg": "#3c3c3c",
        "levels": {"Error": "#f28b82", "Warning": "#fdb95d", "Audit Failure": "#f28b82"},
    },
}


def prefers_dark_theme():
    """Return True if the user's Windows app theme is set to dark."""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        try:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        finally:
            winreg.CloseKey(key)
        # 0 = apps use dark theme, 1 = light.
        return value == 0
    except OSError:
        return False


def apply_titlebar_theme(window, dark):
    """Tint a window's native title bar to match (Windows 10 2004+)."""
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        # DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(value), ctypes.sizeof(value)
        )
    except Exception:
        # Older Windows builds lack the attribute; the client area is still themed.
        pass


def list_log_sources():
    """Enumerate classic event log names from the registry."""
    sources = []
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Services\EventLog",
        )
        try:
            i = 0
            while True:
                sources.append(winreg.EnumKey(key, i))
                i += 1
        except OSError:
            pass
        finally:
            winreg.CloseKey(key)
    except OSError:
        pass
    if not sources:
        sources = ["Application", "System", "Security"]
    # Put the common ones first, the rest alphabetically.
    common = [s for s in ("Application", "System", "Security") if s in sources]
    rest = sorted(s for s in sources if s not in common)
    return common + rest


def read_last_hour(log_name):
    """Read events from `log_name` newer than one hour ago.

    Returns a list of dicts, newest first. Raises on access errors
    (e.g. Security log without admin rights).
    """
    cutoff = datetime.datetime.now() - datetime.timedelta(hours=1)
    handle = win32evtlog.OpenEventLog(None, log_name)
    events = []
    try:
        flags = (
            win32evtlog.EVENTLOG_BACKWARDS_READ
            | win32evtlog.EVENTLOG_SEQUENTIAL_READ
        )
        done = False
        while not done:
            records = win32evtlog.ReadEventLog(handle, flags, 0)
            if not records:
                break
            for rec in records:
                # TimeGenerated is a pywintypes datetime in local time.
                generated = datetime.datetime(
                    rec.TimeGenerated.year,
                    rec.TimeGenerated.month,
                    rec.TimeGenerated.day,
                    rec.TimeGenerated.hour,
                    rec.TimeGenerated.minute,
                    rec.TimeGenerated.second,
                )
                if generated < cutoff:
                    done = True
                    break
                try:
                    message = win32evtlogutil.SafeFormatMessage(rec, log_name)
                except Exception:
                    message = "(unable to format message)"
                events.append(
                    {
                        "time": generated,
                        "level": EVENT_TYPE_NAMES.get(rec.EventType, f"Type {rec.EventType}"),
                        "source": rec.SourceName,
                        "event_id": rec.EventID & 0xFFFF,
                        "category": rec.EventCategory,
                        "record_number": rec.RecordNumber,
                        "computer": rec.ComputerName,
                        "message": message or "",
                    }
                )
    finally:
        win32evtlog.CloseEventLog(handle)
    return events


class EventViewerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Windows Event Log Viewer")
        self.root.geometry("900x500")
        self.events = []
        self.result_queue = queue.Queue()

        self.dark = prefers_dark_theme()
        self.palette = THEMES["dark" if self.dark else "light"]
        self._apply_theme()

        self._build_toolbar()
        self._build_event_list()
        self._build_statusbar()

        apply_titlebar_theme(self.root, self.dark)

    # ----- UI construction -------------------------------------------------

    def _apply_theme(self):
        """Style ttk widgets and the combobox dropdown for the active palette."""
        p = self.palette
        style = ttk.Style()
        # "clam" honors custom colors; the native "vista" theme largely ignores them.
        style.theme_use("clam")
        self.root.configure(bg=p["bg"])

        style.configure(".", background=p["bg"], foreground=p["fg"])
        style.configure("TFrame", background=p["bg"])
        style.configure("TLabel", background=p["bg"], foreground=p["fg"])
        style.configure("TButton", background=p["heading_bg"], foreground=p["fg"])
        style.map(
            "TButton",
            background=[("active", p["select_bg"]), ("disabled", p["bg"])],
            foreground=[("disabled", p["heading_bg"])],
        )
        style.configure(
            "Treeview",
            background=p["field_bg"],
            foreground=p["fg"],
            fieldbackground=p["field_bg"],
        )
        style.map(
            "Treeview",
            background=[("selected", p["select_bg"])],
            foreground=[("selected", p["select_fg"])],
        )
        style.configure(
            "Treeview.Heading", background=p["heading_bg"], foreground=p["fg"]
        )
        style.configure(
            "TCombobox",
            fieldbackground=p["field_bg"],
            background=p["heading_bg"],
            foreground=p["fg"],
            arrowcolor=p["fg"],
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", p["field_bg"])],
            foreground=[("readonly", p["fg"])],
        )
        # The combobox dropdown is a Tk Listbox, styled via the option database.
        self.root.option_add("*TCombobox*Listbox.background", p["field_bg"])
        self.root.option_add("*TCombobox*Listbox.foreground", p["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", p["select_bg"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", p["select_fg"])

    def _build_toolbar(self):
        bar = ttk.Frame(self.root, padding=8)
        bar.pack(fill=tk.X)

        ttk.Label(bar, text="Log source:").pack(side=tk.LEFT)
        self.log_var = tk.StringVar(value="Application")
        self.log_combo = ttk.Combobox(
            bar,
            textvariable=self.log_var,
            values=list_log_sources(),
            state="readonly",
            width=30,
        )
        self.log_combo.pack(side=tk.LEFT, padx=(6, 12))

        self.fetch_button = ttk.Button(
            bar, text="Get Last Hour", command=self.fetch_events
        )
        self.fetch_button.pack(side=tk.LEFT)

    def _build_event_list(self):
        frame = ttk.Frame(self.root, padding=(8, 0, 8, 0))
        frame.pack(fill=tk.BOTH, expand=True)

        columns = ("time", "level", "source", "event_id", "summary")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings")
        self.tree.heading("time", text="Time")
        self.tree.heading("level", text="Level")
        self.tree.heading("source", text="Source")
        self.tree.heading("event_id", text="Event ID")
        self.tree.heading("summary", text="Message")
        self.tree.column("time", width=140, stretch=False)
        self.tree.column("level", width=90, stretch=False)
        self.tree.column("source", width=180, stretch=False)
        self.tree.column("event_id", width=70, stretch=False, anchor=tk.E)
        self.tree.column("summary", width=380)

        for level, color in self.palette["levels"].items():
            self.tree.tag_configure(level, foreground=color)

        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<Double-1>", self.on_double_click)

    def _build_statusbar(self):
        self.status_var = tk.StringVar(value="Select a log and click 'Get Last Hour'.")
        ttk.Label(
            self.root, textvariable=self.status_var, padding=(8, 4), anchor=tk.W
        ).pack(fill=tk.X)

    # ----- Fetching ---------------------------------------------------------

    def fetch_events(self):
        log_name = self.log_var.get()
        self.fetch_button.config(state=tk.DISABLED)
        self.status_var.set(f"Reading '{log_name}' ...")
        thread = threading.Thread(
            target=self._fetch_worker, args=(log_name,), daemon=True
        )
        thread.start()
        self.root.after(100, self._poll_results)

    def _fetch_worker(self, log_name):
        try:
            events = read_last_hour(log_name)
            self.result_queue.put(("ok", log_name, events))
        except Exception as exc:
            self.result_queue.put(("error", log_name, exc))

    def _poll_results(self):
        try:
            status, log_name, payload = self.result_queue.get_nowait()
        except queue.Empty:
            self.root.after(100, self._poll_results)
            return

        self.fetch_button.config(state=tk.NORMAL)
        if status == "error":
            self.status_var.set(f"Failed to read '{log_name}'.")
            messagebox.showerror(
                "Error",
                f"Could not read log '{log_name}':\n{payload}\n\n"
                "Note: the Security log requires administrator rights.",
            )
            return

        self._populate(payload)
        self.status_var.set(
            f"{len(payload)} event(s) from '{log_name}' in the last hour."
        )

    def _populate(self, events):
        self.tree.delete(*self.tree.get_children())
        # Newest first (already time-sorted from the backwards read).
        self.events = sorted(events, key=lambda e: e["time"], reverse=True)
        for idx, ev in enumerate(self.events):
            first_line = ev["message"].strip().splitlines()
            summary = first_line[0] if first_line else "(no message)"
            self.tree.insert(
                "",
                tk.END,
                iid=str(idx),
                values=(
                    ev["time"].strftime("%Y-%m-%d %H:%M:%S"),
                    ev["level"],
                    ev["source"],
                    ev["event_id"],
                    summary[:200],
                ),
                tags=(ev["level"],),
            )

    # ----- Detail window ----------------------------------------------------

    def on_double_click(self, _event):
        item = self.tree.focus()
        if not item:
            return
        ev = self.events[int(item)]

        p = self.palette
        win = tk.Toplevel(self.root)
        win.title(f"Event {ev['event_id']} - {ev['source']}")
        win.geometry("700x450")
        win.configure(bg=p["bg"])

        text = tk.Text(
            win,
            wrap=tk.WORD,
            bg=p["field_bg"],
            fg=p["fg"],
            insertbackground=p["fg"],
            selectbackground=p["select_bg"],
            selectforeground=p["select_fg"],
            relief=tk.FLAT,
        )
        scroll = ttk.Scrollbar(win, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        detail = (
            f"Time:          {ev['time'].strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Level:         {ev['level']}\n"
            f"Source:        {ev['source']}\n"
            f"Event ID:      {ev['event_id']}\n"
            f"Category:      {ev['category']}\n"
            f"Record Number: {ev['record_number']}\n"
            f"Computer:      {ev['computer']}\n"
            f"{'-' * 60}\n\n"
            f"{ev['message'] or '(no message)'}"
        )
        text.insert("1.0", detail)
        text.configure(state=tk.DISABLED)

        apply_titlebar_theme(win, self.dark)


def ensure_admin():
    """Relaunch with administrator rights (UAC prompt) if not already elevated."""
    if ctypes.windll.shell32.IsUserAnAdmin():
        return
    script = os.path.abspath(sys.argv[0])
    params = " ".join(f'"{arg}"' for arg in [script] + sys.argv[1:])
    ret = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, None, 1
    )
    if ret > 32:  # relaunch succeeded; this instance is done
        sys.exit(0)
    # UAC was declined or elevation failed - continue without admin rights.
    ctypes.windll.user32.MessageBoxW(
        None,
        "Elevation was declined. Continuing without administrator rights;\n"
        "the Security log will not be readable.",
        "Windows Event Log Viewer",
        0x30,  # MB_ICONWARNING
    )


def main():
    ensure_admin()
    root = tk.Tk()
    EventViewerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
