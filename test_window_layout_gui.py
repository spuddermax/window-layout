"""Tests for the Window Layouts picker.  `python3 -m unittest -v` here.

The pure helpers are tested as they are.  The picker itself is built for
real, against a throwaway snapshot folder and real (xmessage) windows on
$DISPLAY, and driven the way a person would: pick, untick, Restore.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import window_layout as wl

HAVE_X = bool(os.environ.get("DISPLAY")) and shutil.which("xmessage")
if HAVE_X:
    import window_layout_gui as gui
    from gi.repository import Gtk


def snap(session, taken, n=1):
    return {"session": session, "taken": taken,
            "windows": [wl.Window("0x1", 0, 1, 0, 0, 10, 10, "a.A", "t")] * n,
            "monitors": [], "path": Path(f"{taken}.json")}


@unittest.skipUnless(HAVE_X, "needs an X display (GTK)")
class Helpers(unittest.TestCase):
    def test_sessions_newest_first_and_named_for_a_person(self):
        every = [snap("old", "2026-10-26T09:00:00"),
                 snap("old", "2026-10-26T18:30:00"),
                 snap("lost", "2026-10-27T19:10:00"),
                 snap("lost", "2026-10-27T19:43:00"),
                 snap("now", "2026-10-27T19:47:00")]
        groups = gui.session_groups(every, "now")
        self.assertEqual([g[0] for g in groups],
                         ["This session", "Previous session", "Earlier session"])
        name, span, snaps = groups[1]
        self.assertEqual(span, "Tue Oct 27, 19:10 – 19:43")
        #  Newest snapshot first within a session.
        self.assertEqual([s["taken"][-8:] for s in snaps], ["19:43:00", "19:10:00"])

    def test_a_session_across_midnight_names_both_days(self):
        every = [snap("s", "2026-10-26T23:50:00"), snap("s", "2026-10-27T00:10:00")]
        self.assertEqual(gui.session_groups(every, "x")[0][1],
                         "Oct 26 23:50 – Oct 27 00:10")

    def test_without_a_previous_session(self):
        groups = gui.session_groups([snap("now", "2026-10-27T19:47:00")], "now")
        self.assertEqual([g[0] for g in groups], ["This session"])

    def test_preview_scale_keeps_aspect_and_centres(self):
        to = gui.scaler((0, 0, 4480, 1440), 900, 300, margin=0)
        x, y, w, h = to((0, 0, 4480, 1440))
        self.assertAlmostEqual(w / h, 4480 / 1440, places=3)
        self.assertLessEqual(w, 900)
        self.assertLessEqual(h, 300)
        self.assertAlmostEqual(x, (900 - w) / 2)
        self.assertAlmostEqual(y, (300 - h) / 2)
        #  A window halfway across lands halfway across.
        self.assertAlmostEqual(to((2240, 0, 1, 1))[0], x + w / 2)

    def test_bounds_are_the_monitors_or_else_the_windows(self):
        mons = [wl.Monitor("a", 0, 0, 1920, 1080),
                wl.Monitor("b", 1920, 0, 2560, 1440)]
        self.assertEqual(gui.bounds(mons, []), (0, 0, 4480, 1440))
        wins = [wl.Window("0x1", 0, 1, -10, 20, 100, 50, "a.A", "")]
        self.assertEqual(gui.bounds([], wins), (-10, 20, 100, 50))
        self.assertEqual(gui.bounds([], []), (0, 0, 1920, 1080))

    def test_an_app_is_always_the_same_colour(self):
        self.assertEqual(gui.app_color("brave-browser.Brave-browser"),
                         gui.app_color("Brave-Browser.brave-browser"))
        self.assertNotEqual(gui.app_color("caja.Caja"),
                            gui.app_color("xed.Xed"))

    def test_icons_are_found_for_dotted_res_names(self):
        icon = gui.app_icon("org.inkscape.Inkscape.Inkscape")
        names = icon.get_names() if hasattr(icon, "get_names") else []
        self.assertNotIn("application-x-executable", names)
        fallback = gui.app_icon("no-such-app.No-such-app")
        self.assertIn("application-x-executable", fallback.get_names())

    def test_states_in_words(self):
        self.assertEqual(gui.state_label([]), "")
        self.assertEqual(gui.state_label(["maximized_vert", "maximized_horz"]),
                         "maximized")
        self.assertEqual(gui.state_label(["maximized_horz"]),
                         "maximized horizontally")
        self.assertEqual(gui.state_label(["maximized_vert", "hidden"]),
                         "maximized vertically, minimized")
        self.assertEqual(gui.state_label(["sticky", "above"]),
                         "on every workspace, always on top")

    def test_counts_read_right(self):
        self.assertEqual(gui.windows(1), "1 window")
        self.assertEqual(gui.windows(0), "0 windows")
        a = wl.Window("0x1", 0, 1, 0, 0, 1, 1, "t.Term", "")
        self.assertEqual(gui.summary([wl.Step(a, a, a.rect)]),
                         "1 of 1 window is open now")

    def test_summary(self):
        a = wl.Window("0x1", 0, 1, 0, 0, 1, 1, "t.Term", "")
        b = wl.Window("0x2", 0, 1, 0, 0, 1, 1, "i.Inkscape", "")
        steps = [wl.Step(a, a, a.rect), wl.Step(b, None, b.rect)]
        self.assertEqual(gui.summary(steps),
                         "1 of 2 windows are open now  ·  not open: Inkscape")


@unittest.skipUnless(HAVE_X, "needs an X display")
class PickerLive(unittest.TestCase):
    """The real picker, real windows, a snapshot folder of our own."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder)
        patch = mock.patch.object(wl, "SNAPSHOTS", self.folder)
        patch.start()
        self.addCleanup(patch.stop)
        #  Never wait on the refresh timer in a test.
        timer = mock.patch.object(gui.GLib, "timeout_add_seconds")
        timer.start()
        self.addCleanup(timer.stop)
        self.stamp = str(os.getpid())

    def _open(self, name: str) -> str:
        proc = subprocess.Popen(["xmessage", "-name", name, "-title", name,
                                 "picker test window"])

        def close():
            proc.kill()
            proc.wait()
        self.addCleanup(close)
        for _ in range(50):
            for w in wl.parse_wmctrl(wl._run("wmctrl", "-lGpx")):
                if w.title == name:
                    return w.id
            time.sleep(0.1)
        self.fail(f"{name} never appeared")

    def _ours(self):
        return [w for w in wl.read_windows() if w.title.endswith(self.stamp)]

    _app = None

    def _picker(self):
        #  One application per process -- GApplication exports itself on the
        #  session bus once -- and a fresh picker window on it per test.
        if PickerLive._app is None:
            PickerLive._app = gui.App(unique=False)
            PickerLive._app.register(None)
        window = gui.Picker(PickerLive._app)
        self.addCleanup(window.destroy)
        window.show_all()
        self._pump()
        return window

    @staticmethod
    def _pump(seconds: float = 0.2):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            while Gtk.events_pending():
                Gtk.main_iteration()
            time.sleep(0.02)

    def test_picks_the_previous_session_and_restores_only_whats_ticked(self):
        a = self._open(f"pk-a-{self.stamp}")
        b = self._open(f"pk-b-{self.stamp}")
        wl._run("wmctrl", "-i", "-r", a, "-e", "0,150,120,420,260")
        wl._run("wmctrl", "-i", "-r", b, "-e", "0,800,450,380,220")
        time.sleep(0.4)
        saved = {w.title: w.rect for w in self._ours()}
        t = datetime(2026, 10, 27, 19, 43)
        wl.save(self._ours(), wl.read_monitors(), "lost-session", now=t)
        wl.save(self._ours()[:1], [], "older-session", now=t - timedelta(days=1))
        #  Scramble both.
        for wid in (a, b):
            wl._run("wmctrl", "-i", "-r", wid, "-e", "0,1300,700,300,180")
        time.sleep(0.4)
        scrambled = {w.title: w.rect for w in self._ours()}

        picker = self._picker()
        self.assertEqual(picker.snap["session"], "lost-session")
        ours = [row for row in picker.list_store
                if row[gui.W_TITLE].startswith("pk-")]
        self.assertEqual(len(ours), 2)
        self.assertTrue(all(row[gui.W_TICK] for row in ours))
        #  Untick b: Restore must leave it where it is.
        b_row = next(row for row in picker.list_store
                     if row[gui.W_TITLE].startswith("pk-b-"))
        picker.on_ticked(None, b_row.path)
        self.assertFalse(b_row[gui.W_TICK])
        #  Only our two windows are ticked, so only one is now.
        for row in picker.list_store:
            if not row[gui.W_TITLE].startswith("pk-") and row[gui.W_TICK]:
                picker.on_ticked(None, row.path)
        self.assertEqual(picker.restore_button.get_label(), "Restore 1 window")

        picker.restore_button.clicked()
        for _ in range(100):
            self._pump(0.05)
            if not picker.busy:
                break
        self.assertFalse(picker.busy)
        self.assertIn("Put back 1 window.", picker.status.get_text())
        time.sleep(0.3)
        now = {w.title: w.rect for w in self._ours()}
        self.assertEqual(now[f"pk-a-{self.stamp}"], saved[f"pk-a-{self.stamp}"])
        self.assertEqual(now[f"pk-b-{self.stamp}"],
                         scrambled[f"pk-b-{self.stamp}"])

    def test_the_picker_is_never_part_of_a_layout(self):
        picker = self._picker()
        self._pump(0.3)
        classes = {w.wm_class for w in wl.read_windows()}
        self.assertFalse(any(c.startswith(wl.OWN_CLASS) for c in classes),
                         classes)
        picker.destroy()

    def test_workspace_buttons_filter_the_preview(self):
        a = self._open(f"pk-w-{self.stamp}")
        win = next(w for w in self._ours() if w.id == a)
        other = wl.Window(**{**win.__dict__, "desktop": 2, "id": "0xdead",
                             "title": "elsewhere", "wm_class": "z.Z"})
        wl.save([win, other], wl.read_monitors(), "lost-session")
        picker = self._picker()
        labels = [b.get_label() for b in picker.workspaces.get_children()]
        self.assertEqual(labels, ["All", str(win.desktop + 1), "3"])
        picker.workspaces.get_children()[2].set_active(True)
        self.assertEqual(picker.preview.workspace, 2)
        #  And the preview draws without error at every workspace.
        for button in picker.workspaces.get_children():
            button.set_active(True)
            picker.preview.queue_draw()
            self._pump(0.05)

    def test_no_snapshots_says_so(self):
        picker = self._picker()
        self.assertIn("No snapshots yet", picker.title_label.get_text())
        self.assertFalse(picker.restore_button.get_sensitive())


if __name__ == "__main__":
    unittest.main()
