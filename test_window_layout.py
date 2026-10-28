"""Tests for window_layout.  `python3 -m unittest -v` in this folder.

The live tests at the bottom open real (xmessage) windows on $DISPLAY,
move them, and restore them; they skip where there is no X display.
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
from types import SimpleNamespace
from unittest import mock

import window_layout as wl

WMCTRL = """\
0x00c00003 -1 2140   0    2106 1920 27   mate-panel.Mate-panel  matthew-laptop Bottom Panel
0x04000006  0 4140   972  70   958  495  mate-terminal.Mate-terminal  matthew-laptop ◑ Desktop crash  to text console
0x05a00004  2 5120   -8   -30  1936 1110 brave-browser.Brave-browser  matthew-laptop Inbox (3) - Gmail - Brave
0x06000001  1 6001   10   20   300  200  xmessage.Xmessage  matthew-laptop
garbage line
"""


def win(cls, title, x=0, y=0, w=100, h=100, desk=0, states=()):
    return wl.Window("0x1", desk, 1, x, y, w, h, cls, title, list(states))


class Parsing(unittest.TestCase):
    def test_wmctrl_lines_keep_titles_whole(self):
        got = wl.parse_wmctrl(WMCTRL)
        self.assertEqual(len(got), 4)
        term = got[1]
        self.assertEqual((term.id, term.desktop, term.pid), ("0x04000006", 0, 4140))
        self.assertEqual(term.rect, (972, 70, 958, 495))
        self.assertEqual(term.wm_class, "mate-terminal.Mate-terminal")
        #  Inner double spaces survive; only the separator is consumed.
        self.assertEqual(term.title, "◑ Desktop crash  to text console")

    def test_negative_geometry_and_sticky_desktop(self):
        panel, _, brave, _ = wl.parse_wmctrl(WMCTRL)
        self.assertEqual(panel.desktop, -1)
        self.assertEqual(brave.rect, (-8, -30, 1936, 1110))

    def test_a_window_with_no_title(self):
        self.assertEqual(wl.parse_wmctrl(WMCTRL)[3].title, "")

    def test_xprop_states_and_types(self):
        states, types = wl.parse_xprop(
            "_NET_WM_STATE(ATOM) = _NET_WM_STATE_MAXIMIZED_HORZ, "
            "_NET_WM_STATE_FOCUSED, _NET_WM_STATE_MAXIMIZED_VERT\n"
            "_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NORMAL\n")
        self.assertEqual(states, ["maximized_horz", "maximized_vert"])
        self.assertEqual(types, {"_NET_WM_WINDOW_TYPE_NORMAL"})

    def test_xprop_with_nothing_set(self):
        states, types = wl.parse_xprop(
            "_NET_WM_STATE:  not found.\n_NET_WM_WINDOW_TYPE:  not found.\n")
        self.assertEqual((states, types), ([], set()))

    def test_monitors(self):
        got = wl.parse_monitors(
            "Monitors: 2\n"
            " 0: +*eDP-1 1920/382x1080/215+0+0  eDP-1\n"
            " 1: +DVI-I-1-1 2560/597x1440/336+1920+0  DVI-I-1-1\n")
        self.assertEqual([(m.name, m.x, m.y, m.w, m.h) for m in got],
                         [("eDP-1", 0, 0, 1920, 1080),
                          ("DVI-I-1-1", 1920, 0, 2560, 1440)])


class Matching(unittest.TestCase):
    def test_same_title_wins_over_order(self):
        saved = [win("t.T", "build"), win("t.T", "logs")]
        now = [win("t.T", "logs"), win("t.T", "build")]
        pairs = wl.match(saved, now)
        self.assertEqual([(s.title, c.title) for s, c in pairs],
                         [("build", "build"), ("logs", "logs")])

    def test_a_browser_whose_tab_changed_still_matches(self):
        saved = [win("b.B", "Inbox (3) - Gmail - Brave"),
                 win("b.B", "Pull request #114 - GitHub - Brave")]
        now = [win("b.B", "Pull request #114 · example/garden-planner - Brave"),
               win("b.B", "Inbox (7) - Gmail - Brave")]
        pairs = {s.title: c.title for s, c in wl.match(saved, now)}
        self.assertEqual(pairs["Inbox (3) - Gmail - Brave"],
                         "Inbox (7) - Gmail - Brave")

    def test_never_across_applications(self):
        self.assertEqual(wl.match([win("a.A", "x")], [win("b.B", "x")]), [])

    def test_each_window_used_once(self):
        saved = [win("t.T", "a"), win("t.T", "a"), win("t.T", "a")]
        now = [win("t.T", "a"), win("t.T", "a")]
        pairs = wl.match(saved, now)
        self.assertEqual(len(pairs), 2)
        self.assertEqual(len({id(c) for _, c in pairs}), 2)
        #  The first two saved, in saved order.
        self.assertIs(pairs[0][0], saved[0])
        self.assertIs(pairs[1][0], saved[1])


class Screens(unittest.TestCase):
    laptop = [wl.Monitor("eDP-1", 0, 0, 1920, 1080)]

    def test_on_and_off_screen(self):
        self.assertTrue(wl.on_screen((100, 100, 500, 400), self.laptop))
        #  Was on a 2560-wide monitor to the right, unplugged since.
        self.assertFalse(wl.on_screen((2200, 100, 800, 600), self.laptop))
        #  A sliver at the edge is not "on screen".
        self.assertFalse(wl.on_screen((1900, 100, 800, 600), self.laptop))
        self.assertTrue(wl.on_screen((5000, 5000, 10, 10), []))

    def test_fit_moves_and_shrinks_onto_the_screen(self):
        self.assertEqual(wl.fit((2200, 100, 800, 600), self.laptop),
                         (1120, 100, 800, 600))
        self.assertEqual(wl.fit((3000, -50, 2560, 1440), self.laptop),
                         (0, 0, 1920, 1080))


class Storage(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.t0 = datetime(2026, 10, 27, 19, 0, 0)

    def _save(self, windows, session="s1", minutes=0):
        return wl.save(windows, [], session, folder=self.dir,
                       now=self.t0 + timedelta(minutes=minutes))

    def test_round_trip(self):
        w = win("t.T", "a", 1, 2, 3, 4, desk=2, states=["sticky"])
        w.command = "mate-terminal --tab"
        path = self._save([w])
        back = wl.load(path)
        self.assertEqual(back["windows"], [w])
        self.assertEqual(back["session"], "s1")

    def test_an_unchanged_layout_is_not_saved_again(self):
        self.assertIsNotNone(self._save([win("t.T", "a")]))
        #  A new window id and pid is the same layout.
        again = win("t.T", "a")
        again.id, again.pid = "0x9", 99
        self.assertIsNone(self._save([again], minutes=2))
        self.assertIsNotNone(self._save([win("t.T", "a", x=5)], minutes=4))
        self.assertEqual(len(wl.snapshots(self.dir)), 2)

    def test_a_new_session_saves_even_the_same_layout(self):
        self._save([win("t.T", "a")])
        self.assertIsNotNone(self._save([win("t.T", "a")], "s2", minutes=2))

    def test_an_empty_desktop_is_not_saved(self):
        self.assertIsNone(self._save([]))

    def test_previous_is_the_last_snapshot_of_another_session(self):
        self._save([win("t.T", "old")], "s1", 0)
        self._save([win("t.T", "lost")], "s1", 2)        # the crash took this
        self._save([win("t.T", "fresh")], "s2", 4)       # after logging back in
        self._save([win("t.T", "fresher")], "s2", 6)
        every = wl.snapshots(self.dir)
        self.assertEqual(wl.pick(every, "s2", "previous")["windows"][0].title,
                         "lost")
        self.assertEqual(wl.pick(every, "s2", "latest")["windows"][0].title,
                         "fresher")
        self.assertEqual(wl.pick(every, "s2", "20261027-190000")["windows"][0]
                         .title, "old")
        self.assertIsNone(wl.pick(every, "s9", "nope"))
        self.assertIsNone(wl.pick(wl.snapshots(self.dir)[2:], "s2", "previous"))

    def test_prune_keeps_the_last_of_each_recent_session(self):
        self._save([win("t.T", "a")], "s1", 0)
        self._save([win("t.T", "b")], "s1", 1)           # last of s1
        self._save([win("t.T", "c")], "s2", 2)
        removed = wl.prune(self.dir, now=self.t0 + timedelta(days=30))
        left = [s["windows"][0].title for s in wl.snapshots(self.dir)]
        self.assertEqual(left, ["b", "c"])
        self.assertEqual(len(removed), 1)

    def test_prune_keeps_everything_recent(self):
        for n in range(5):
            self._save([win("t.T", str(n))], "s1", n)
        self.assertEqual(wl.prune(self.dir, now=self.t0 + timedelta(days=1)), [])

    def test_a_corrupt_snapshot_is_skipped(self):
        self._save([win("t.T", "a")])
        (self.dir / "20261027-180000.json").write_text("{not json")
        self.assertEqual(len(wl.snapshots(self.dir)), 1)


class Restore(unittest.TestCase):
    """cmd_restore's report, with the desktop faked."""

    def test_report_names_whats_missing_and_how_it_ran(self):
        saved = [win("t.T", "build", 10, 20, 300, 200),
                 win("ink.Inkscape", "map.svg - Inkscape", 0, 0, 900, 700)]
        saved[1].command = "inkscape /home/matthew/map.svg"
        snap = {"path": Path("20261027-194300.json"), "taken": "x",
                "session": "s1", "windows": saved, "monitors": []}
        now = [win("t.T", "build")]
        with mock.patch.object(wl, "snapshots", return_value=[snap]), \
             mock.patch.object(wl, "session_key", return_value="s2"), \
             mock.patch.object(wl, "read_windows", return_value=now), \
             mock.patch.object(wl, "read_monitors", return_value=[]), \
             mock.patch.object(wl, "desktop_count", return_value=4), \
             mock.patch.object(wl, "place") as place, \
             mock.patch("sys.stdout.write") as out:
            wl.cmd_restore(SimpleNamespace(which="previous", dry_run=False, notify=False))
        place.assert_called_once()
        text = "".join(c.args[0] for c in out.call_args_list)
        self.assertIn("1 of 2 windows are open", text)
        self.assertIn("not open now (1)", text)
        self.assertIn("$ inkscape /home/matthew/map.svg", text)

    def test_a_snapshot_with_no_x_display_is_skipped_quietly(self):
        gone = subprocess.CalledProcessError(1, ["wmctrl"], stderr="Cannot open display")
        with mock.patch.object(wl, "read_windows", side_effect=gone), \
             mock.patch.object(wl, "save") as save:
            self.assertEqual(wl.cmd_snapshot(SimpleNamespace(verbose=False)), 0)
        save.assert_not_called()

    def test_nothing_to_restore(self):
        with mock.patch.object(wl, "snapshots", return_value=[]), \
             mock.patch.object(wl, "session_key", return_value="s"), \
             mock.patch("sys.stderr.write"):
            self.assertEqual(
                wl.cmd_restore(SimpleNamespace(which="previous", dry_run=True, notify=False)),
                1)


# ---------------------------------------------------------------------------
# Live: real windows on the real display.
# ---------------------------------------------------------------------------


@unittest.skipUnless(os.environ.get("DISPLAY") and shutil.which("xmessage")
                     and shutil.which("wmctrl"), "needs an X display")
class Live(unittest.TestCase):
    def _open(self, name: str) -> str:
        proc = subprocess.Popen(["xmessage", "-name", name, "-title", name,
                                 "window-layout test window"])

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

    def _here(self, wid):
        return next(w for w in wl.read_windows() if w.id == wid)

    def test_scrambled_windows_go_back_exactly(self):
        stamp = str(os.getpid())
        a, b = self._open(f"wl-a-{stamp}"), self._open(f"wl-b-{stamp}")
        wl._run("wmctrl", "-i", "-r", a, "-e", "0,200,150,420,260")
        wl._run("wmctrl", "-i", "-r", b, "-e", "0,900,500,380,220")
        time.sleep(0.4)
        before = {w.title: w for w in wl.read_windows()
                  if w.title.endswith(stamp)}
        self.assertEqual(len(before), 2)
        #  Scramble: both elsewhere, one maximized.
        wl._run("wmctrl", "-i", "-r", a, "-e", "0,1300,700,300,200")
        wl._run("wmctrl", "-i", "-r", b, "-e", "0,50,50,300,200")
        wl._run("wmctrl", "-i", "-r", b, "-b", "add,maximized_vert,maximized_horz")
        time.sleep(0.4)
        current = [w for w in wl.read_windows() if w.title.endswith(stamp)]
        for saved, now in wl.match(list(before.values()), current):
            wl.place(now.id, saved, saved.rect, wl.desktop_count())
        time.sleep(0.4)
        for wid in (a, b):
            got = self._here(wid)
            want = before[got.title]
            self.assertEqual(got.rect, want.rect, got.title)
            self.assertNotIn("maximized_vert", got.states)

    def test_a_maximized_window_comes_back_maximized(self):
        a = self._open(f"wl-m-{os.getpid()}")
        wl._run("wmctrl", "-i", "-r", a, "-b", "add,maximized_vert,maximized_horz")
        time.sleep(0.4)
        saved = self._here(a)
        self.assertIn("maximized_horz", saved.states)
        wl._run("wmctrl", "-i", "-r", a, "-b",
                "remove,maximized_vert,maximized_horz")
        time.sleep(0.4)
        wl.place(a, saved, saved.rect, wl.desktop_count())
        time.sleep(0.4)
        got = self._here(a)
        self.assertIn("maximized_vert", got.states)
        self.assertIn("maximized_horz", got.states)

    def test_a_minimized_window_is_minimized_again_in_its_place(self):
        a = self._open(f"wl-h-{os.getpid()}")
        wl._run("wmctrl", "-i", "-r", a, "-e", "0,500,400,300,200")
        time.sleep(0.3)
        subprocess.run(["xdotool", "windowminimize", str(int(a, 16))])
        time.sleep(0.4)
        saved = self._here(a)
        self.assertIn("hidden", saved.states)
        subprocess.run(["xdotool", "windowmap", str(int(a, 16))])
        wl._run("wmctrl", "-i", "-r", a, "-e", "0,100,100,300,200")
        time.sleep(0.4)
        self.assertNotIn("hidden", self._here(a).states)
        wl.place(a, saved, saved.rect, wl.desktop_count())
        time.sleep(0.4)
        got = self._here(a)
        self.assertIn("hidden", got.states)
        self.assertEqual(got.rect, saved.rect)

    def test_a_window_minimized_since_comes_back_into_view(self):
        a = self._open(f"wl-u-{os.getpid()}")
        wl._run("wmctrl", "-i", "-r", a, "-e", "0,600,300,300,200")
        time.sleep(0.4)
        saved = self._here(a)
        self.assertNotIn("hidden", saved.states)
        subprocess.run(["xdotool", "windowminimize", str(int(a, 16))])
        time.sleep(0.4)
        wl.place(a, saved, saved.rect, wl.desktop_count())
        time.sleep(0.4)
        got = self._here(a)
        self.assertNotIn("hidden", got.states)
        self.assertEqual(got.rect, saved.rect)

    def test_a_window_goes_back_to_its_workspace(self):
        if wl.desktop_count() < 2:
            self.skipTest("one workspace")
        a = self._open(f"wl-d-{os.getpid()}")
        saved = self._here(a)
        saved.desktop = 1
        wl.place(a, saved, saved.rect, wl.desktop_count())
        time.sleep(0.3)
        self.assertEqual(self._here(a).desktop, 1)


if __name__ == "__main__":
    unittest.main()
