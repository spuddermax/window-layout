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


class Deleting(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        patch = mock.patch.object(wl, "SNAPSHOTS", self.dir)
        patch.start()
        self.addCleanup(patch.stop)
        #  Recent: cmd_snapshot prunes against the real clock, and
        #  snapshots past KEEP_DAYS would go out from under the test.
        self.t0 = datetime.now().replace(microsecond=0) - timedelta(hours=1)
        self.a = wl.save([win("t.T", "a", x=1)], [], "s1", now=self.t0)
        self.b = wl.save([win("t.T", "a", x=2)], [], "s1",
                         now=self.t0 + timedelta(minutes=2))
        self.c = wl.save([win("t.T", "a", x=3)], [], "s2",
                         now=self.t0 + timedelta(minutes=4))

    def names(self):
        return [s["path"].name for s in wl.snapshots()]

    def test_delete_and_undo(self):
        moved = wl.delete([self.b])
        self.assertEqual(self.names(), [self.a.name, self.c.name])
        self.assertEqual(wl.undelete(moved), [self.b])
        self.assertEqual(self.names(), [self.a.name, self.b.name, self.c.name])

    def test_deleting_what_is_already_gone_is_harmless(self):
        wl.delete([self.b])
        self.assertEqual(wl.delete([self.b]), [])

    def test_deleted_snapshots_are_dropped_for_good_after_a_day(self):
        moved = wl.delete([self.a])
        self.assertEqual(wl.prune(now=datetime.now() + timedelta(hours=1)), [])
        self.assertEqual(wl.prune(now=datetime.now() + timedelta(days=2)), moved)
        self.assertEqual(wl.undelete(moved), [])

    def test_a_deleted_layout_is_not_saved_again_until_a_window_moves(self):
        here = [win("t.T", "a", x=3)]
        with mock.patch.object(wl, "read_windows", side_effect=lambda: list(here)), \
             mock.patch.object(wl, "read_monitors", return_value=[]), \
             mock.patch.object(wl, "session_key", return_value="s2"):
            run = lambda **kw: wl.cmd_snapshot(SimpleNamespace(verbose=False, **kw))
            run()                                   # sees c's layout
            wl.delete([self.c])
            run()
            self.assertEqual(self.names(), [self.a.name, self.b.name])
            here[0] = win("t.T", "a", x=9)          # a window moves
            run()
            self.assertEqual(len(self.names()), 3)
            #  The camera button: saved even though nothing moved.
            time.sleep(1.1)                         # names are by the second
            run(force=True)
            self.assertEqual(len(self.names()), 4)

    def test_cli_deletes_by_name_or_by_session(self):
        with mock.patch.object(wl, "session_key", return_value="s2"), \
             mock.patch("sys.stdout.write") as out:
            self.assertEqual(wl.main(["delete", self.a.stem]), 0)
            self.assertEqual(self.names(), [self.b.name, self.c.name])
            text = "".join(c.args[0] for c in out.call_args_list)
            self.assertIn("deleted 1 snapshot:", text)
            self.assertIn("undelete", text)
            wl.main(["undelete"])
            self.assertEqual(len(self.names()), 3)
            wl.main(["delete", "--session", self.b.stem])
            self.assertEqual(self.names(), [self.c.name])

    def test_cli_refuses_an_unknown_name(self):
        with mock.patch("sys.stderr.write") as err:
            self.assertEqual(wl.main(["delete", "20991231-000000"]), 1)
        self.assertIn("no snapshot", "".join(c.args[0] for c in err.call_args_list))
        self.assertEqual(len(self.names()), 3)


class CheckedNote(unittest.TestCase):
    now = datetime(2026, 10, 28, 9, 11, 0)

    def note(self, checked_ago, newest=None):
        return wl.checked_note(
            None if checked_ago is None else self.now - timedelta(seconds=checked_ago),
            newest, self.now)

    def test_an_old_snapshot_and_a_fresh_check_is_an_unchanged_layout(self):
        self.assertEqual(
            self.note(35, datetime(2026, 10, 27, 23, 3, 37)),
            "Checked just now; no window has moved since yesterday 23:03, "
            "the newest snapshot")

    def test_saved_on_the_last_check(self):
        checked = self.now - timedelta(seconds=150)
        self.assertEqual(wl.checked_note(checked, checked, self.now),
                         "Checked 2 min ago; saved then (09:08)")

    def test_a_stale_check_is_a_stopped_timer(self):
        text = self.note(3 * 3600, datetime(2026, 10, 25, 6, 0))
        self.assertTrue(text.startswith("Last checked 06:11, 3 h ago: the "
                                        "snapshot timer seems to have stopped"),
                        text)
        self.assertIn("systemctl --user status", text)

    def test_never_checked(self):
        self.assertIn("Never checked", self.note(None))

    def test_nothing_saved_this_session(self):
        self.assertEqual(self.note(10), "Checked just now; nothing saved this "
                                        "session yet")

    def test_older_days_name_the_date(self):
        self.assertEqual(wl._when(datetime(2026, 10, 25, 6, 0), self.now),
                         "Oct 25 06:00")


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
        #  Nor is a title that changed (a spinner, a browser tab).
        self.assertIsNone(self._save([win("t.T", "◐ a")], minutes=3))
        self.assertIsNotNone(self._save([win("t.T", "a", x=5)], minutes=4))
        self.assertEqual(len(wl.snapshots(self.dir)), 2)

    def test_a_new_session_saves_even_the_same_layout(self):
        self._save([win("t.T", "a")])
        self.assertIsNotNone(self._save([win("t.T", "a")], "s2", minutes=2))

    def test_an_empty_desktop_is_not_saved(self):
        self.assertIsNone(self._save([]))

    def test_every_run_is_stamped_saved_or_not(self):
        self.assertIsNone(wl.last_checked(self.dir))
        with mock.patch.object(wl, "SNAPSHOTS", self.dir), \
             mock.patch.object(wl, "read_windows", return_value=[win("t.T", "a")]), \
             mock.patch.object(wl, "read_monitors", return_value=[]), \
             mock.patch.object(wl, "session_key", return_value="s1"):
            wl.cmd_snapshot(SimpleNamespace(verbose=False))
            first = wl.last_checked(self.dir)
            os.utime(self.dir / wl.CHECKED, (0, 0))
            #  Unchanged: nothing new saved, but the check is recorded.
            wl.cmd_snapshot(SimpleNamespace(verbose=False))
        self.assertIsNotNone(first)
        self.assertEqual(len(wl.snapshots(self.dir)), 1)
        self.assertGreater(wl.last_checked(self.dir), datetime(2000, 1, 1))
        #  The stamp is not a snapshot.
        self.assertEqual([p.suffix for p in self.dir.iterdir()
                          if p.name != wl.CHECKED], [".json"])

    def test_previous_is_the_last_snapshot_of_another_session(self):
        self._save([win("t.T", "old", x=1)], "s1", 0)
        self._save([win("t.T", "lost", x=2)], "s1", 2)   # the crash took this
        self._save([win("t.T", "fresh", x=3)], "s2", 4)  # after logging back in
        self._save([win("t.T", "fresher", x=4)], "s2", 6)
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
        self._save([win("t.T", "a", x=1)], "s1", 0)
        self._save([win("t.T", "b", x=2)], "s1", 1)      # last of s1
        self._save([win("t.T", "c", x=3)], "s2", 2)
        removed = wl.prune(self.dir, now=self.t0 + timedelta(days=30))
        left = [s["windows"][0].title for s in wl.snapshots(self.dir)]
        self.assertEqual(left, ["b", "c"])
        self.assertEqual(len(removed), 1)

    def test_prune_keeps_everything_recent(self):
        for n in range(5):
            self._save([win("t.T", str(n), x=n)], "s1", n)
        self.assertEqual(wl.prune(self.dir, now=self.t0 + timedelta(days=1)), [])

    def test_a_corrupt_snapshot_is_skipped(self):
        self._save([win("t.T", "a")])
        (self.dir / "20261027-180000.json").write_text("{not json")
        self.assertEqual(len(wl.snapshots(self.dir)), 1)


class Reopening(unittest.TestCase):
    """Starting apps that aren't open: what to run, and how often."""

    def saved(self, cls, title, pid, argv, cwd=""):
        w = win(cls, title)
        w.pid, w.argv, w.command, w.cwd = pid, argv, " ".join(argv), cwd
        return w

    def test_a_terminal_reopens_in_its_folder(self):
        home = os.path.expanduser("~")
        self.assertEqual(wl.terminal_dir("matthew@laptop: ~"), home)
        self.assertEqual(wl.terminal_dir("me@box: /tmp"), "/tmp")
        self.assertIsNone(wl.terminal_dir("✳ Claude Code"))
        self.assertIsNone(wl.terminal_dir("me@box: ~/no/such/folder/here"))
        t = self.saved("mate-terminal.Mate-terminal", "me@box: /tmp", 1,
                       ["/bin/sh", "--", "x"])
        #  Only a terminal gets the option.
        self.assertEqual(wl.launch_argv(t), ["/bin/sh", "--", "x"])
        with mock.patch.object(wl, "TERMINALS", {"sh"}):
            self.assertEqual(wl.launch_argv(t),
                             ["/bin/sh", "--working-directory=/tmp", "--", "x"])

    def test_what_cannot_be_started(self):
        self.assertIsNone(wl.launch_argv(self.saved("a.A", "", 1, [])))
        self.assertIsNone(wl.launch_argv(
            self.saved("a.A", "", 1, ["/no/such/program"])))
        self.assertIsNone(wl.launch_argv(
            self.saved("a.A", "", 1, ["no-such-program-anywhere"])))
        #  A relative program needs the folder it ran in.
        self.assertIsNone(wl.launch_argv(
            self.saved("a.A", "", 1, ["bin/sh"])))
        self.assertEqual(wl.launch_argv(
            self.saved("a.A", "", 1, ["bin/sh"], cwd="/")), ["bin/sh"])

    def test_old_snapshots_and_libreoffice(self):
        old = win("a.A", "")
        old.command = "sh -c true"                  # no argv recorded
        self.assertEqual(wl.launch_argv(old), ["sh", "-c", "true"])
        calc = self.saved("l.L", "", 1, ["/bin/sh", "--calc", "--splash-pipe=5"])
        self.assertEqual(wl.launch_argv(calc), ["/bin/sh", "--calc"])

    def test_a_window_with_no_usable_pid_is_found_by_its_class(self):
        me = os.getpid()
        self.assertEqual(wl.owner(me, "x.X"), me)
        with mock.patch.object(wl, "_run", return_value="4242\n") as run:
            self.assertEqual(wl.owner(2, "deskflow.Deskflow"), 4242)
        self.assertEqual(run.call_args.args[-1], "deskflow")
        gone = subprocess.CalledProcessError(1, ["pgrep"])
        with mock.patch.object(wl, "_run", side_effect=gone):
            self.assertEqual(wl.owner(0, "nothing.Nothing"), 0)

    def test_where_it_ran_is_not_part_of_the_layout(self):
        a, b = win("t.T", "x"), win("t.T", "x")
        b.argv, b.cwd = ["t"], "/tmp"
        self.assertEqual(wl._layout([a]), wl._layout([b]))

    def run_reopen(self, missing, appears):
        """reopen() against a fake desktop: `appears(w)` is the windows
        starting saved window `w` opens.  Returns (failed, started)."""
        desktop, started = [], []
        clock = [0.0]

        def start(w):
            started.append(w)
            for n, cls in enumerate(appears(w)):
                desktop.append(win(cls, w.title))
                desktop[-1].id = f"0x{len(desktop)}"
            return True

        def sleep(t):
            clock[0] += t
        failed = wl.reopen(missing, read=lambda: list(desktop), start=start,
                           sleep=sleep, clock=lambda: clock[0])
        return failed, started, clock[0]

    def test_a_browser_is_started_once_for_its_windows(self):
        b1 = self.saved("b.B", "Mail", 7, ["/bin/sh"])
        b2 = self.saved("b.B", "News", 7, ["/bin/sh"])
        #  The browser restores its session: both windows at once.
        failed, started, _ = self.run_reopen(
            [b1, b2], lambda w: ["b.B", "b.B"] if w is b1 else ["b.B"])
        self.assertEqual((failed, [id(w) for w in started]), ([], [id(b1)]))

    def test_terminals_are_started_once_each(self):
        ts = [self.saved("t.T", f"me@box: /tmp", 9, ["/bin/sh"])
              for _ in range(3)]                    # equal field for field
        failed, started, waited = self.run_reopen(ts, lambda w: ["t.T"])
        self.assertEqual(failed, [])
        self.assertEqual(len(started), 3)
        #  One quiet spell after the first, none after the rest.
        self.assertLess(waited, wl.OPEN_QUIET_S + 1)

    def test_an_app_that_shows_nothing_is_not_started_twice(self):
        a = self.saved("a.A", "x", 1, ["/bin/sh"])
        b = self.saved("a.A", "y", 1, ["/bin/sh"])
        failed, started, waited = self.run_reopen([a, b], lambda w: [])
        self.assertEqual(len(started), 1)
        self.assertGreaterEqual(waited, wl.OPEN_WAIT_S)

    def test_what_cannot_start_is_handed_back(self):
        a = self.saved("a.A", "x", 1, [])
        failed, started, _ = self.run_reopen([a], lambda w: ["a.A"])
        self.assertEqual((failed, started), ([a], []))


class Restore(unittest.TestCase):
    """cmd_restore's report, with the desktop faked."""

    def test_report_names_whats_missing_and_how_it_ran(self):
        saved = [win("t.T", "build", 10, 20, 300, 200),
                 win("ink.Inkscape", "map.svg - Inkscape", 0, 0, 900, 700)]
        saved[1].command = "inkscape /home/user/map.svg"
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
        self.assertIn("$ inkscape /home/user/map.svg", text)
        self.assertIn("restore --open", text)

    def test_open_starts_whats_missing_then_places_it(self):
        saved = [win("t.T", "build"), win("sh.Sh", "shell")]
        saved[1].argv = ["/bin/sh"]
        snap = {"path": Path("20261027-194300.json"), "taken": "x",
                "session": "s1", "windows": saved, "monitors": []}
        desktop = [win("t.T", "build")]

        def reopen(missing):
            self.assertEqual([w.title for w in missing], ["shell"])
            desktop.append(win("sh.Sh", "shell"))
            return []
        for dry in (True, False):
            with mock.patch.object(wl, "snapshots", return_value=[snap]), \
                 mock.patch.object(wl, "session_key", return_value="s2"), \
                 mock.patch.object(wl, "read_windows",
                                   side_effect=lambda: list(desktop)), \
                 mock.patch.object(wl, "read_monitors", return_value=[]), \
                 mock.patch.object(wl, "desktop_count", return_value=4), \
                 mock.patch.object(wl, "reopen", side_effect=reopen) as ro, \
                 mock.patch.object(wl, "place") as place, \
                 mock.patch("sys.stdout.write") as out:
                wl.main(["restore", "--open"] + (["--dry-run"] if dry else []))
            text = "".join(c.args[0] for c in out.call_args_list)
            if dry:
                ro.assert_not_called()
                place.assert_not_called()
                self.assertIn("would start 1", text)
                self.assertIn("$ /bin/sh", text)
                self.assertNotIn("not open now", text)
            else:
                self.assertEqual(place.call_count, 2)
                self.assertIn("now 2 of 2 windows are open", text)
                self.assertNotIn("not open now", text)

    def test_the_tests_own_windows_are_never_snapshotted(self):
        here = [win("t.T", "a"), win(wl.TEST_CLASS + "x.Xmessage", "x")]
        with mock.patch.object(wl, "read_windows", return_value=here), \
             mock.patch.object(wl, "read_monitors", return_value=[]), \
             mock.patch.object(wl, "session_key", return_value="s"), \
             mock.patch.object(wl, "seen_last", return_value=False), \
             mock.patch.object(wl, "mark_checked"), \
             mock.patch.object(wl, "prune"), \
             mock.patch.object(wl, "save") as save:
            wl.cmd_snapshot(SimpleNamespace(verbose=False))
        self.assertEqual([w.title for w in save.call_args.args[0]], ["a"])

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
        proc = subprocess.Popen(["xmessage", "-name", wl.TEST_CLASS + name, "-title", name,
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

    def test_what_ran_a_window_is_recorded(self):
        a = self._open(f"wl-r-{os.getpid()}")
        got = self._here(a)
        self.assertEqual(got.argv[0], "xmessage")
        self.assertEqual(got.cwd, os.getcwd())

    def test_a_closed_window_is_started_again_and_put_back(self):
        name = f"wl-o-{os.getpid()}"
        proc = subprocess.Popen(["xmessage", "-name", wl.TEST_CLASS + name, "-title", name,
                                 "window-layout reopen test"])
        for _ in range(50):
            here = [w for w in wl.read_windows() if w.title == name]
            if here:
                break
            time.sleep(0.1)
        wl._run("wmctrl", "-i", "-r", here[0].id, "-e", "0,333,222,300,200")
        time.sleep(0.4)
        saved = [w for w in wl.read_windows() if w.title == name]
        proc.kill()
        proc.wait()
        time.sleep(0.3)
        self.assertFalse([w for w in wl.read_windows() if w.title == name])

        self.assertEqual(wl.reopen(saved), [])
        back = [w for w in wl.read_windows() if w.title == name]

        def close():
            for w in back:
                try:
                    os.kill(w.pid, 15)
                except OSError:
                    pass
        self.addCleanup(close)
        self.assertEqual(len(back), 1)
        step, = wl.plan(saved, back, wl.read_monitors())
        wl.place(step.now.id, step.saved, step.rect, wl.desktop_count())
        time.sleep(0.4)
        self.assertEqual(self._here(back[0].id).rect, saved[0].rect)


if __name__ == "__main__":
    unittest.main()
