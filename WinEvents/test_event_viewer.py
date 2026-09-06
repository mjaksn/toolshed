"""Tests for event_viewer.py.

Run with:  python -m pytest --cov=event_viewer
"""

import datetime
import sys
import tkinter as tk
from unittest import mock

import pytest

import event_viewer

# ----- Helpers ---------------------------------------------------------------


class FakeRecord:
    """Mimics the PyEventLogRecord objects returned by ReadEventLog."""

    def __init__(
        self,
        when,
        event_type=None,
        source="TestSource",
        event_id=100,
        category=0,
        record_number=1,
        computer="TESTPC",
    ):
        self.TimeGenerated = when
        self.EventType = (
            event_type
            if event_type is not None
            else event_viewer.win32evtlog.EVENTLOG_INFORMATION_TYPE
        )
        self.SourceName = source
        self.EventID = event_id
        self.EventCategory = category
        self.RecordNumber = record_number
        self.ComputerName = computer


def make_event(minutes_ago=5, level="Information", message="hello\nworld", **over):
    ev = {
        "time": datetime.datetime.now() - datetime.timedelta(minutes=minutes_ago),
        "level": level,
        "source": "TestSource",
        "event_id": 100,
        "category": 0,
        "record_number": 1,
        "computer": "TESTPC",
        "message": message,
    }
    ev.update(over)
    return ev


# A single Tk interpreter for the whole session: creating and destroying a
# fresh tk.Tk() per test intermittently fails Tcl initialization on Windows.
@pytest.fixture(scope="session")
def tk_root():
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def app(tk_root):
    window = tk.Toplevel(tk_root)
    window.withdraw()
    application = event_viewer.EventViewerApp(window)
    yield application
    window.destroy()


# ----- prefers_dark_theme ----------------------------------------------------


class TestPrefersDarkTheme:
    def _patch_registry(self, monkeypatch, value):
        monkeypatch.setattr(event_viewer.winreg, "OpenKey", lambda *a: "key")
        monkeypatch.setattr(
            event_viewer.winreg, "QueryValueEx", lambda k, n: (value, 4)
        )
        monkeypatch.setattr(event_viewer.winreg, "CloseKey", lambda k: None)

    def test_dark_when_apps_use_light_is_zero(self, monkeypatch):
        self._patch_registry(monkeypatch, 0)
        assert event_viewer.prefers_dark_theme() is True

    def test_light_when_apps_use_light_is_one(self, monkeypatch):
        self._patch_registry(monkeypatch, 1)
        assert event_viewer.prefers_dark_theme() is False

    def test_defaults_to_light_when_value_missing(self, monkeypatch):
        def boom(*a):
            raise OSError("value not found")

        monkeypatch.setattr(event_viewer.winreg, "OpenKey", boom)
        assert event_viewer.prefers_dark_theme() is False


# ----- theming ---------------------------------------------------------------


class TestTheme:
    def _make_app(self, tk_root, monkeypatch, dark):
        monkeypatch.setattr(event_viewer, "prefers_dark_theme", lambda: dark)
        window = tk.Toplevel(tk_root)
        window.withdraw()
        return event_viewer.EventViewerApp(window), window

    def test_dark_setting_selects_dark_palette(self, tk_root, monkeypatch):
        app, window = self._make_app(tk_root, monkeypatch, dark=True)
        try:
            assert app.dark is True
            assert app.palette is event_viewer.THEMES["dark"]
        finally:
            window.destroy()

    def test_light_setting_selects_light_palette(self, tk_root, monkeypatch):
        app, window = self._make_app(tk_root, monkeypatch, dark=False)
        try:
            assert app.dark is False
            assert app.palette is event_viewer.THEMES["light"]
        finally:
            window.destroy()

    def test_tree_level_tags_use_palette_colors(self, tk_root, monkeypatch):
        app, window = self._make_app(tk_root, monkeypatch, dark=True)
        try:
            expected = event_viewer.THEMES["dark"]["levels"]["Error"]
            assert str(app.tree.tag_configure("Error", "foreground")) == expected
        finally:
            window.destroy()

    def test_detail_window_uses_theme_colors(self, tk_root, monkeypatch):
        app, window = self._make_app(tk_root, monkeypatch, dark=True)
        try:
            app._populate([make_event()])
            app.tree.focus("0")
            app.on_double_click(None)
            top = next(
                w for w in window.winfo_children() if isinstance(w, tk.Toplevel)
            )
            text = next(
                w for w in top.winfo_children() if isinstance(w, tk.Text)
            )
            assert str(text["bg"]) == event_viewer.THEMES["dark"]["field_bg"]
            assert str(text["fg"]) == event_viewer.THEMES["dark"]["fg"]
        finally:
            window.destroy()

    def test_titlebar_theme_swallows_errors(self, monkeypatch):
        # A fake window whose winfo_id blows up must not propagate.
        fake = mock.Mock()
        fake.winfo_id.side_effect = RuntimeError("no handle")
        event_viewer.apply_titlebar_theme(fake, True)  # must not raise


# ----- list_log_sources ------------------------------------------------------


class TestListLogSources:
    def _patch_registry(self, monkeypatch, names):
        def fake_enum_key(key, i):
            if i < len(names):
                return names[i]
            raise OSError("no more keys")

        monkeypatch.setattr(event_viewer.winreg, "OpenKey", lambda *a: "fake-key")
        monkeypatch.setattr(event_viewer.winreg, "EnumKey", fake_enum_key)
        monkeypatch.setattr(event_viewer.winreg, "CloseKey", lambda k: None)

    def test_common_logs_come_first(self, monkeypatch):
        self._patch_registry(
            monkeypatch, ["Zebra", "System", "Alpha", "Application", "Security"]
        )
        assert event_viewer.list_log_sources() == [
            "Application",
            "System",
            "Security",
            "Alpha",
            "Zebra",
        ]

    def test_rest_is_alphabetical(self, monkeypatch):
        self._patch_registry(monkeypatch, ["Charlie", "Alpha", "Bravo"])
        assert event_viewer.list_log_sources() == ["Alpha", "Bravo", "Charlie"]

    def test_registry_failure_falls_back_to_defaults(self, monkeypatch):
        def raise_oserror(*a):
            raise OSError("access denied")

        monkeypatch.setattr(event_viewer.winreg, "OpenKey", raise_oserror)
        assert event_viewer.list_log_sources() == [
            "Application",
            "System",
            "Security",
        ]

    def test_real_registry_contains_application(self):
        # Integration check against the actual machine.
        assert "Application" in event_viewer.list_log_sources()


# ----- read_last_hour --------------------------------------------------------


class TestReadLastHour:
    def _patch_evtlog(self, monkeypatch, batches, message="formatted message"):
        """Patch win32evtlog so ReadEventLog yields `batches` in order."""
        batches = list(batches)
        closed = []

        def fake_read(handle, flags, offset):
            return batches.pop(0) if batches else []

        monkeypatch.setattr(
            event_viewer.win32evtlog, "OpenEventLog", lambda srv, name: "handle"
        )
        monkeypatch.setattr(event_viewer.win32evtlog, "ReadEventLog", fake_read)
        monkeypatch.setattr(
            event_viewer.win32evtlog, "CloseEventLog", lambda h: closed.append(h)
        )
        monkeypatch.setattr(
            event_viewer.win32evtlogutil,
            "SafeFormatMessage",
            lambda rec, log: message,
        )
        return closed

    def test_returns_only_events_within_last_hour(self, monkeypatch):
        now = datetime.datetime.now()
        recent = FakeRecord(now - datetime.timedelta(minutes=10))
        older = FakeRecord(now - datetime.timedelta(minutes=30))
        ancient = FakeRecord(now - datetime.timedelta(hours=2))
        self._patch_evtlog(monkeypatch, [[recent, older, ancient]])

        events = event_viewer.read_last_hour("Application")
        assert len(events) == 2

    def test_stops_reading_once_past_cutoff(self, monkeypatch):
        now = datetime.datetime.now()
        first = [FakeRecord(now - datetime.timedelta(hours=3))]
        second = [FakeRecord(now - datetime.timedelta(minutes=1))]
        self._patch_evtlog(monkeypatch, [first, second])

        # The first batch is already past the cutoff, so the in-range
        # record in the second batch must never be reached.
        events = event_viewer.read_last_hour("Application")
        assert events == []

    def test_stops_on_empty_batch(self, monkeypatch):
        now = datetime.datetime.now()
        self._patch_evtlog(monkeypatch, [[FakeRecord(now)], []])
        events = event_viewer.read_last_hour("Application")
        assert len(events) == 1

    def test_event_fields_are_mapped(self, monkeypatch):
        now = datetime.datetime.now()
        rec = FakeRecord(
            now,
            event_type=event_viewer.win32evtlog.EVENTLOG_ERROR_TYPE,
            source="MyApp",
            event_id=0xC0000064,  # high bits must be masked to 100
            category=3,
            record_number=42,
            computer="BOX1",
        )
        self._patch_evtlog(monkeypatch, [[rec]], message="boom")

        (ev,) = event_viewer.read_last_hour("Application")
        assert ev["level"] == "Error"
        assert ev["source"] == "MyApp"
        assert ev["event_id"] == 100
        assert ev["category"] == 3
        assert ev["record_number"] == 42
        assert ev["computer"] == "BOX1"
        assert ev["message"] == "boom"

    def test_unknown_event_type_gets_generic_label(self, monkeypatch):
        rec = FakeRecord(datetime.datetime.now(), event_type=99)
        self._patch_evtlog(monkeypatch, [[rec]])
        (ev,) = event_viewer.read_last_hour("Application")
        assert ev["level"] == "Type 99"

    def test_message_format_failure_is_handled(self, monkeypatch):
        rec = FakeRecord(datetime.datetime.now())
        self._patch_evtlog(monkeypatch, [[rec]])

        def boom(rec, log):
            raise RuntimeError("no message DLL")

        monkeypatch.setattr(event_viewer.win32evtlogutil, "SafeFormatMessage", boom)
        (ev,) = event_viewer.read_last_hour("Application")
        assert ev["message"] == "(unable to format message)"

    def test_handle_closed_even_when_read_raises(self, monkeypatch):
        closed = self._patch_evtlog(monkeypatch, [])

        def raise_error(handle, flags, offset):
            raise OSError("access denied")

        monkeypatch.setattr(event_viewer.win32evtlog, "ReadEventLog", raise_error)
        with pytest.raises(OSError):
            event_viewer.read_last_hour("Security")
        assert closed == ["handle"]

    def test_open_failure_propagates(self, monkeypatch):
        def raise_error(srv, name):
            raise OSError("no such log")

        monkeypatch.setattr(event_viewer.win32evtlog, "OpenEventLog", raise_error)
        with pytest.raises(OSError):
            event_viewer.read_last_hour("Nonexistent")


# ----- ensure_admin ----------------------------------------------------------


class TestEnsureAdmin:
    def _patch_windll(self, monkeypatch, is_admin, shell_ret=42):
        fake = mock.Mock()
        fake.shell32.IsUserAnAdmin.return_value = is_admin
        fake.shell32.ShellExecuteW.return_value = shell_ret
        monkeypatch.setattr(event_viewer.ctypes, "windll", fake)
        return fake

    def test_already_admin_returns_without_relaunch(self, monkeypatch):
        fake = self._patch_windll(monkeypatch, is_admin=1)
        event_viewer.ensure_admin()
        fake.shell32.ShellExecuteW.assert_not_called()

    def test_relaunches_elevated_and_exits(self, monkeypatch):
        fake = self._patch_windll(monkeypatch, is_admin=0, shell_ret=42)
        with pytest.raises(SystemExit):
            event_viewer.ensure_admin()

        args = fake.shell32.ShellExecuteW.call_args.args
        assert args[1] == "runas"
        assert args[2] == sys.executable
        # Script path must be absolute and quoted.
        import os

        assert f'"{os.path.abspath(sys.argv[0])}"' in args[3]

    def test_declined_uac_warns_and_continues(self, monkeypatch):
        fake = self._patch_windll(monkeypatch, is_admin=0, shell_ret=5)
        event_viewer.ensure_admin()  # must NOT raise SystemExit
        fake.user32.MessageBoxW.assert_called_once()


# ----- GUI: EventViewerApp ---------------------------------------------------


class TestGui:
    def test_initial_widgets(self, app):
        assert app.log_var.get() == "Application"
        assert str(app.fetch_button["state"]) == "normal"
        assert app.tree.get_children() == ()

    def test_populate_sorts_newest_first(self, app):
        events = [
            make_event(minutes_ago=30, message="old"),
            make_event(minutes_ago=1, message="new"),
            make_event(minutes_ago=15, message="middle"),
        ]
        app._populate(events)

        summaries = [
            app.tree.item(iid)["values"][4] for iid in app.tree.get_children()
        ]
        assert summaries == ["new", "middle", "old"]

    def test_populate_uses_first_message_line(self, app):
        app._populate([make_event(message="first line\nsecond line")])
        (iid,) = app.tree.get_children()
        assert app.tree.item(iid)["values"][4] == "first line"

    def test_populate_handles_empty_message(self, app):
        app._populate([make_event(message="")])
        (iid,) = app.tree.get_children()
        assert app.tree.item(iid)["values"][4] == "(no message)"

    def test_populate_tags_rows_by_level(self, app):
        app._populate([make_event(level="Error")])
        (iid,) = app.tree.get_children()
        assert "Error" in app.tree.item(iid)["tags"]

    def test_populate_clears_previous_rows(self, app):
        app._populate([make_event(), make_event()])
        app._populate([make_event()])
        assert len(app.tree.get_children()) == 1

    def test_fetch_events_disables_button_and_spawns_worker(self, app, monkeypatch):
        started = []
        monkeypatch.setattr(
            event_viewer.threading,
            "Thread",
            lambda **kw: mock.Mock(start=lambda: started.append(kw)),
        )
        app.log_var.set("System")
        app.fetch_events()

        assert str(app.fetch_button["state"]) == "disabled"
        assert "System" in app.status_var.get()
        assert started[0]["target"] == app._fetch_worker
        assert started[0]["args"] == ("System",)
        assert started[0]["daemon"] is True

    def test_fetch_worker_puts_events_on_queue(self, app, monkeypatch):
        events = [make_event()]
        monkeypatch.setattr(event_viewer, "read_last_hour", lambda name: events)
        app._fetch_worker("Application")
        status, log_name, payload = app.result_queue.get_nowait()
        assert (status, log_name, payload) == ("ok", "Application", events)

    def test_fetch_worker_puts_error_on_queue(self, app, monkeypatch):
        def boom(name):
            raise OSError("access denied")

        monkeypatch.setattr(event_viewer, "read_last_hour", boom)
        app._fetch_worker("Security")
        status, log_name, payload = app.result_queue.get_nowait()
        assert status == "error"
        assert isinstance(payload, OSError)

    def test_poll_results_populates_on_success(self, app):
        app.fetch_button.config(state=tk.DISABLED)
        app.result_queue.put(("ok", "Application", [make_event()]))
        app._poll_results()

        assert len(app.tree.get_children()) == 1
        assert str(app.fetch_button["state"]) == "normal"
        assert "1 event(s)" in app.status_var.get()

    def test_poll_results_shows_error_dialog(self, app, monkeypatch):
        shown = []
        monkeypatch.setattr(
            event_viewer.messagebox,
            "showerror",
            lambda title, msg: shown.append(msg),
        )
        app.fetch_button.config(state=tk.DISABLED)
        app.result_queue.put(("error", "Security", OSError("access denied")))
        app._poll_results()

        assert len(shown) == 1
        assert "Security" in shown[0]
        assert str(app.fetch_button["state"]) == "normal"
        assert app.tree.get_children() == ()

    def test_poll_results_reschedules_when_queue_empty(self, app, monkeypatch):
        scheduled = []
        monkeypatch.setattr(
            app.root, "after", lambda ms, cb: scheduled.append((ms, cb))
        )
        app._poll_results()
        assert scheduled == [(100, app._poll_results)]

    def test_double_click_opens_detail_window(self, app):
        app._populate(
            [make_event(level="Warning", message="line one\nline two", event_id=777)]
        )
        app.tree.focus("0")
        app.on_double_click(None)

        toplevels = [
            w for w in app.root.winfo_children() if isinstance(w, tk.Toplevel)
        ]
        assert len(toplevels) == 1
        (text_widget,) = [
            w for w in toplevels[0].winfo_children() if isinstance(w, tk.Text)
        ]
        content = text_widget.get("1.0", tk.END)
        assert "777" in content
        assert "Warning" in content
        assert "line one\nline two" in content
        # Detail text must be read-only.
        assert str(text_widget["state"]) == "disabled"

    def test_double_click_with_no_selection_is_noop(self, app):
        app.on_double_click(None)  # nothing selected; must not raise
        assert [
            w for w in app.root.winfo_children() if isinstance(w, tk.Toplevel)
        ] == []
