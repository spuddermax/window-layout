#!/usr/bin/python3
"""Snapshot the desktop's window layout, and put it back after a crash.

    window-layout snapshot          record where every window is (the timer
                                    runs this every two minutes)
    window-layout restore           put open windows back where they were at
                                    the end of the previous X session
    window-layout restore --dry-run say what it would do, move nothing
    window-layout restore --open    first start the apps that aren't open
    window-layout list              the sessions and snapshots on file
    window-layout delete NAME...    delete snapshots (-s: their whole
                                    sessions); undelete brings them back

Why: on 2026-10-27 the X session died under memory pressure and the layout
of a dozen windows went with it -- MATE's session autosave was off and
nothing else had written it down.

How it reads the desktop: `wmctrl -lGpx` for every window's id, workspace,
pid, geometry, WM_CLASS and title, and `xprop` for its state (maximized,
minimized, sticky...) and type, so panels and the desktop are left out.

How it matches: a window after a crash is a different X window, with a
different id and often a different title (a browser's title is its tab).
So a saved window is matched to an open one of the same WM_CLASS, the
closest title first (`difflib`), each used once.  Saved windows nothing
matched are listed with the command that was running them, to reopen --
or, with `--open` (and in the picker), started again from that command,
in the folder it ran in, and placed once their windows appear.

How it moves: Marco does not hand back the coordinates it was given.  A
window set to (300, 200) can report (312, 270) -- an offset that depends on
the window -- and moving a window to where it says it is walks it down the
screen.  Snapshots are in the coordinates `wmctrl` reports, so a restore
moves the window, reads where it landed, and moves it once more by the
difference.

Snapshots are only written when the layout changed, kept 14 days, and the
last one of each of the most recent sessions is kept regardless -- the
layout you lost is the last snapshot of the session that ended, and a
fresh login must not age it out.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME")
                 or Path.home() / ".local" / "state") / "window-layout"
SNAPSHOTS = STATE_DIR / "snapshots"
KEEP_DAYS = 14
#: Touched on every snapshot run, saved or not: a layout that hasn't
#: changed since last night is current, not stale, and this says so.
CHECKED = ".checked"
#: Deleted snapshots wait here, inside the snapshot folder, to be undone.
DELETED = ".deleted"
#: How long a deleted snapshot can be brought back before prune drops it.
UNDO_DAYS = 1
#: How often the timer runs; a stamp much older than this means it stopped.
EVERY_S = 120
#: The last snapshot of each of this many sessions is kept past KEEP_DAYS.
KEEP_SESSIONS = 5
#: Window types that are part of the desktop, not windows to put back.
SKIP_TYPES = {"_NET_WM_WINDOW_TYPE_DOCK", "_NET_WM_WINDOW_TYPE_DESKTOP",
              "_NET_WM_WINDOW_TYPE_SPLASH", "_NET_WM_WINDOW_TYPE_NOTIFICATION",
              "_NET_WM_WINDOW_TYPE_TOOLTIP", "_NET_WM_WINDOW_TYPE_MENU",
              "_NET_WM_WINDOW_TYPE_DROPDOWN_MENU",
              "_NET_WM_WINDOW_TYPE_POPUP_MENU"}
#: _NET_WM_STATE atoms worth carrying across, by their wmctrl -b names.
STATES = {"_NET_WM_STATE_MAXIMIZED_VERT": "maximized_vert",
          "_NET_WM_STATE_MAXIMIZED_HORZ": "maximized_horz",
          "_NET_WM_STATE_FULLSCREEN": "fullscreen",
          "_NET_WM_STATE_STICKY": "sticky",
          "_NET_WM_STATE_ABOVE": "above",
          "_NET_WM_STATE_SHADED": "shaded",
          "_NET_WM_STATE_HIDDEN": "hidden"}
#: The picker's own WM_CLASS: never snapshotted, never moved.
OWN_CLASS = "window-layout"
#: The tests' windows' WM_CLASS starts with this: the timer, running while
#: they do, leaves them out of the real snapshots.
TEST_CLASS = "wltest-"
#: How long to let the window manager act on a request before reading back.
SETTLE_S = 0.15
#: Starting apps: the longest to wait for their windows, per round...
OPEN_WAIT_S = 20
#: ...and how long no new window must appear before a round is done.
OPEN_QUIET_S = 3
#: Terminals that take --working-directory: one reopens in the folder its
#: shell was in (see `tabs`), else the one in its `user@host: ~/x` title.
TERMINALS = {"mate-terminal", "gnome-terminal", "xfce4-terminal", "tilix",
             "terminator"}
#: ...and those that also take `--window --tab --tab`, each tab with its
#: own --working-directory: a window of three tabs reopens as three tabs.
TABBED = {"mate-terminal", "gnome-terminal", "xfce4-terminal"}


@dataclass
class Window:
    id: str
    desktop: int
    pid: int
    x: int
    y: int
    w: int
    h: int
    wm_class: str
    title: str
    states: list[str] = field(default_factory=list)
    command: str = ""
    #: The process's argv as it was, and the folder it ran in: what starts
    #: it again.  (Older snapshots have only `command`, argv joined.)
    argv: list[str] = field(default_factory=list)
    cwd: str = ""
    #: A terminal's tabs: the folder each one's shell was in (its `pwd`),
    #: oldest tab first.  Empty for anything that isn't a terminal.
    tabs: list[str] = field(default_factory=list)

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.w, self.h


@dataclass
class Monitor:
    name: str
    x: int
    y: int
    w: int
    h: int


# ---------------------------------------------------------------------------
# Reading the desktop
# ---------------------------------------------------------------------------


def _run(*argv: str) -> str:
    return subprocess.run(argv, capture_output=True, text=True,
                          check=True).stdout


_WMCTRL = re.compile(r"^(0x[0-9a-f]+)\s+(-?\d+)\s+(\d+)\s+(-?\d+)\s+(-?\d+)"
                     r"\s+(\d+)\s+(\d+)\s+(\S+)\s+\S+\s?(.*)$")


def parse_wmctrl(text: str) -> list[Window]:
    """`wmctrl -lGpx` lines -> windows (state and command left empty)."""
    windows = []
    for line in text.splitlines():
        m = _WMCTRL.match(line)
        if not m:
            continue
        wid, desk, pid, x, y, w, h, cls, title = m.groups()
        windows.append(Window(wid, int(desk), int(pid), int(x), int(y),
                              int(w), int(h), cls, title))
    return windows


def _list_windows() -> list[Window]:
    """`wmctrl -lGpx`, read again if it fails: it does when a window closes
    while it's being listed.  With no X display it fails every time."""
    for attempt in range(3):
        try:
            return parse_wmctrl(_run("wmctrl", "-lGpx"))
        except subprocess.CalledProcessError:
            if attempt == 2:
                raise
            time.sleep(0.1)
    return []


def parse_xprop(text: str) -> tuple[list[str], set[str]]:
    """`xprop -id W _NET_WM_STATE _NET_WM_WINDOW_TYPE` -> (states, types)."""
    states, types = [], set()
    for line in text.splitlines():
        name, _, value = line.partition("=")
        atoms = [a.strip() for a in value.split(",") if a.strip()]
        if name.startswith("_NET_WM_STATE"):
            states = [STATES[a] for a in atoms if a in STATES]
        elif name.startswith("_NET_WM_WINDOW_TYPE"):
            types = set(atoms)
    return states, types


def parse_monitors(text: str) -> list[Monitor]:
    """`xrandr --listmonitors` -> monitors."""
    out = []
    for line in text.splitlines():
        m = re.search(r"(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)\s+(\S+)\s*$",
                      line)
        if m:
            w, h, x, y, name = m.groups()
            out.append(Monitor(name, int(x), int(y), int(w), int(h)))
    return out


def _argv(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def _cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def _mine(pid: int) -> bool:
    try:
        return pid > 0 and os.stat(f"/proc/{pid}").st_uid == os.getuid()
    except OSError:
        return False


def owner(pid: int, wm_class: str) -> int:
    """The process behind a window.  Its _NET_WM_PID, when that is one of
    ours with a command line; else -- xmessage sets none (0), and Deskflow
    reports 2, a kernel thread -- the newest process of ours named after
    the window's WM_CLASS (`deskflow.Deskflow` -> deskflow)."""
    if _mine(pid) and _argv(pid):
        return pid
    name, _, cls = wm_class.rpartition(".")
    for candidate in dict.fromkeys((name, name.lower(), cls.lower())):
        if not candidate or len(candidate) > 15:   # past comm's length
            continue
        try:
            found = _run("pgrep", "-n", "-x", "-u", str(os.getuid()),
                         candidate).split()
        except (OSError, subprocess.CalledProcessError):
            continue
        if found:
            return int(found[0])
    return pid


def _children(proc: Path = Path("/proc")) -> dict[int, list[int]]:
    """Our processes by parent pid."""
    kids: dict[int, list[int]] = {}
    uid = os.getuid()
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            if d.stat().st_uid != uid:
                continue
            #  The command name can hold spaces and parentheses: the
            #  fields after the last ")" are fixed.
            ppid = int((d / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(d.name))
    return kids


def _windowid(pid: int, proc: Path = Path("/proc")) -> int | None:
    try:
        env = (proc / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    for var in env.split(b"\0"):
        if var.startswith(b"WINDOWID="):
            try:
                return int(var[9:])
            except ValueError:
                return None
    return None


def terminal_tabs(windows: list[Window],
                  proc: Path = Path("/proc")) -> dict[str, list[str]]:
    """Each terminal window's tabs, as the folder each tab's shell is in:
    {window id: [folder, ...]}.  A terminal starts each tab's shell with
    WINDOWID set to its window, in decimal; the shells are the terminal
    process's own children (their children inherit WINDOWID too, so only
    the direct children count) -- and only those reading from a tab's
    pseudo-terminal, once per terminal: a helper the terminal spawned
    isn't a tab."""
    kids = _children(proc)
    ours = {int(w.id, 16): w.id for w in windows}
    tabs: dict[str, list[str]] = {}
    seen: set[str] = set()
    for pid in sorted({w.pid for w in windows}):
        for child in sorted(kids.get(pid, [])):
            wid = _windowid(child, proc)
            if wid not in ours:
                continue
            try:
                tty = os.readlink(proc / str(child) / "fd" / "0")
                folder = os.readlink(proc / str(child) / "cwd")
            except OSError:
                continue
            if not tty.startswith("/dev/pts/") or tty in seen:
                continue
            seen.add(tty)
            tabs.setdefault(ours[wid], []).append(folder)
    return tabs


def read_windows() -> list[Window]:
    windows = []
    for win in _list_windows():
        try:
            states, types = parse_xprop(_run(
                "xprop", "-id", win.id, "_NET_WM_STATE", "_NET_WM_WINDOW_TYPE"))
        except subprocess.CalledProcessError:
            continue                              # gone since wmctrl saw it
        if types & SKIP_TYPES or win.wm_class.startswith(OWN_CLASS):
            continue
        win.states = states
        win.pid = owner(win.pid, win.wm_class)
        win.argv = _argv(win.pid)
        win.command = " ".join(win.argv)
        win.cwd = _cwd(win.pid)
        windows.append(win)
    try:
        tabs = terminal_tabs(windows)
    except OSError:
        tabs = {}
    for win in windows:
        win.tabs = tabs.get(win.id, [])
    return windows


def read_monitors() -> list[Monitor]:
    try:
        return parse_monitors(_run("xrandr", "--listmonitors"))
    except (OSError, subprocess.CalledProcessError):
        return []


def desktop_count() -> int:
    return max(1, len(_run("wmctrl", "-d").splitlines()))


def session_key() -> str:
    """This X session, as boot id + the X server's pid and start time: a
    new login (or a crashed one restarted) is a new key."""
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    try:
        pid = _run("pgrep", "-o", "-x", "Xorg").split()[0]
        start = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1] \
            .split()[19]
    except (OSError, IndexError, subprocess.CalledProcessError):
        pid, start = "?", os.environ.get("DISPLAY", "?")
    return f"{boot}:{pid}:{start}"


# ---------------------------------------------------------------------------
# Snapshots on disk
# ---------------------------------------------------------------------------


def _layout(windows: list[Window]) -> list[dict]:
    """What makes two snapshots the same layout: everything but window ids,
    pids and titles, in a stable order.  Titles help match a window but are
    not where it is -- and a busy terminal's title has a spinner in it, a
    browser's is its tab, so counting them would save a "new" layout every
    two minutes.  A terminal's tabs' folders do count: a `cd` is saved, so
    the last snapshot reopens each terminal where its shell last was."""
    return sorted(({k: v for k, v in asdict(w).items()
                    if k not in ("id", "pid", "title", "argv", "cwd")}
                   for w in windows),
                  key=lambda d: json.dumps(d, sort_keys=True))


def load(path: Path) -> dict:
    data = json.loads(path.read_text())
    data["windows"] = [Window(**w) for w in data["windows"]]
    data["monitors"] = [Monitor(**m) for m in data.get("monitors", [])]
    data["path"] = path
    return data


def snapshots(folder: Path | None = None) -> list[dict]:
    """Every snapshot on file, oldest first; unreadable ones skipped."""
    folder = folder or SNAPSHOTS
    out = []
    for path in sorted(folder.glob("*.json")):
        try:
            out.append(load(path))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return out


def save(windows: list[Window], monitors: list[Monitor], session: str,
         folder: Path | None = None,
         now: datetime | None = None, force: bool = False) -> Path | None:
    """Write a snapshot if the layout differs from the last one of this
    session (or `force`); return its path, or None when nothing changed
    (or there is nothing to save)."""
    if not windows:
        return None
    folder = folder or SNAPSHOTS
    folder.mkdir(parents=True, exist_ok=True)
    same_session = [s for s in snapshots(folder) if s["session"] == session]
    if not force and same_session and \
            _layout(same_session[-1]["windows"]) == _layout(windows):
        return None
    now = now or datetime.now()
    path = folder / f"{now:%Y%m%d-%H%M%S}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "taken": now.isoformat(timespec="seconds"),
        "session": session,
        "monitors": [asdict(m) for m in monitors],
        "windows": [asdict(w) for w in windows],
    }, indent=1))
    tmp.replace(path)
    return path


def mark_checked(folder: Path | None = None, session: str = "",
                 windows: list[Window] | None = None) -> None:
    """Stamp a check, with the layout it saw: a layout already seen isn't
    saved again, even if its snapshot has since been deleted."""
    folder = folder or SNAPSHOTS
    folder.mkdir(parents=True, exist_ok=True)
    (folder / CHECKED).write_text(json.dumps(
        {"session": session, "layout": _layout(windows or [])}))


def seen_last(session: str, windows: list[Window],
              folder: Path | None = None) -> bool:
    """Whether the last check saw this same layout in this session."""
    try:
        last = json.loads(((folder or SNAPSHOTS) / CHECKED).read_text())
    except (OSError, ValueError):
        return False
    return isinstance(last, dict) and last.get("session") == session and \
        last.get("layout") == json.loads(json.dumps(_layout(windows)))


def delete(paths: list[Path], folder: Path | None = None) -> list[Path]:
    """Move snapshots into the undo folder; returns where each one went,
    for `undelete`.  After UNDO_DAYS, prune deletes them for good."""
    trash = (folder or SNAPSHOTS) / DELETED
    trash.mkdir(parents=True, exist_ok=True)
    moved = []
    for path in paths:
        to = trash / path.name
        try:
            path.replace(to)
        except FileNotFoundError:
            continue
        os.utime(to)                   # the undo clock starts now
        moved.append(to)
    return moved


def undelete(moved: list[Path] | None = None,
             folder: Path | None = None) -> list[Path]:
    """Put deleted snapshots back: `moved` (from `delete`), else every one
    still in the undo folder.  Returns the restored paths."""
    folder = folder or SNAPSHOTS
    if moved is None:
        moved = sorted((folder / DELETED).glob("*.json"))
    back = []
    for path in moved:
        to = folder / path.name
        try:
            path.replace(to)
        except FileNotFoundError:
            continue
        back.append(to)
    return back


def last_checked(folder: Path | None = None) -> datetime | None:
    """When the desktop was last read for a snapshot, or None if never."""
    try:
        return datetime.fromtimestamp(((folder or SNAPSHOTS) / CHECKED)
                                      .stat().st_mtime)
    except OSError:
        return None


def _when(t: datetime, now: datetime) -> str:
    """09:10, yesterday 23:03, or Oct 25 23:03."""
    days = (now.date() - t.date()).days
    day = "" if days == 0 else "yesterday " if days == 1 else f"{t:%b %-d} "
    return f"{day}{t:%H:%M}"


def _ago(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    return f"{int(seconds // 3600)} h ago"


def checked_note(checked: datetime | None, newest: datetime | None,
                 now: datetime | None = None) -> str:
    """One line on whether snapshots are current.  `newest` is the newest
    snapshot of this session.  A snapshot is only written when a window
    moves, so an old newest snapshot with a fresh check is a layout that
    hasn't changed -- not a timer that stopped."""
    now = now or datetime.now()
    if checked is None:
        return ("Never checked: is window-layout-snapshot.timer running? "
                "(systemctl --user status window-layout-snapshot.timer)")
    age = (now - checked).total_seconds()
    if age > 3 * EVERY_S:
        return (f"Last checked {_when(checked, now)}, {_ago(age)}: the "
                f"snapshot timer seems to have stopped "
                f"(systemctl --user status window-layout-snapshot.timer)")
    note = f"Checked {_ago(age)}"
    if newest is None:
        return note + "; nothing saved this session yet"
    if (checked - newest).total_seconds() < 60:
        return note + f"; saved then ({_when(newest, now)})"
    return (note + f"; no window has moved since {_when(newest, now)}, "
            f"the newest snapshot")


def prune(folder: Path | None = None,
          now: datetime | None = None) -> list[Path]:
    """Drop snapshots older than KEEP_DAYS, except the last of each of the
    newest KEEP_SESSIONS sessions.  Returns what was removed."""
    folder = folder or SNAPSHOTS
    now = now or datetime.now()
    every = snapshots(folder)
    last_of = {}
    for snap in every:
        last_of[snap["session"]] = snap["path"]
    order = sorted(last_of.items(),
                   key=lambda kv: kv[1].name, reverse=True)[:KEEP_SESSIONS]
    keep = {path for _, path in order}
    removed = []
    for snap in every:
        taken = datetime.fromisoformat(snap["taken"])
        if snap["path"] not in keep and now - taken > timedelta(days=KEEP_DAYS):
            snap["path"].unlink(missing_ok=True)
            removed.append(snap["path"])
    for path in (folder / DELETED).glob("*.json"):
        if now - datetime.fromtimestamp(path.stat().st_mtime) > \
                timedelta(days=UNDO_DAYS):
            path.unlink(missing_ok=True)
            removed.append(path)
    return removed


def pick(every: list[dict], session: str, which: str) -> dict | None:
    """The snapshot to restore: `previous` is the last one taken in any
    other session than this one (the layout a crash or logout lost),
    `latest` the newest of all; else a file name or path."""
    if not every:
        return None
    if which == "latest":
        return every[-1]
    if which == "previous":
        others = [s for s in every if s["session"] != session]
        return others[-1] if others else None
    for snap in every:
        if which in (str(snap["path"]), snap["path"].name, snap["path"].stem):
            return snap
    path = Path(which)
    return load(path) if path.is_file() else None


# ---------------------------------------------------------------------------
# Matching saved windows to open ones
# ---------------------------------------------------------------------------


def match(saved: list[Window], current: list[Window]) -> list[tuple[Window, Window]]:
    """(saved, current) pairs: same WM_CLASS, closest titles first, each
    window used once.  Ties go to saved order, so two identical terminals
    land in the order they were saved."""
    scored = []
    for i, s in enumerate(saved):
        for j, c in enumerate(current):
            if s.wm_class != c.wm_class:
                continue
            ratio = 1.0 if s.title == c.title else \
                difflib.SequenceMatcher(None, s.title, c.title).ratio()
            scored.append((-ratio, i, j))
    scored.sort()
    used_s, used_c, pairs = set(), set(), []
    for _, i, j in scored:
        if i in used_s or j in used_c:
            continue
        used_s.add(i)
        used_c.add(j)
        pairs.append((saved[i], current[j]))
    pairs.sort(key=lambda p: saved.index(p[0]))
    return pairs


def on_screen(rect: tuple[int, int, int, int], monitors: list[Monitor]) -> bool:
    """Does at least a 50x50 corner of the window fall on some monitor?"""
    if not monitors:
        return True
    x, y, w, h = rect
    for m in monitors:
        ix = min(x + w, m.x + m.w) - max(x, m.x)
        iy = min(y + h, m.y + m.h) - max(y, m.y)
        if ix >= 50 and iy >= 50:
            return True
    return False


def fit(rect: tuple[int, int, int, int],
        monitors: list[Monitor]) -> tuple[int, int, int, int]:
    """A window from a monitor that is not here now, moved onto the first
    one there is, shrunk to fit if it must be."""
    m = monitors[0]
    x, y, w, h = rect
    w, h = min(w, m.w), min(h, m.h)
    x = min(max(x, m.x), m.x + m.w - w)
    y = min(max(y, m.y), m.y + m.h - h)
    return x, y, w, h


@dataclass
class Step:
    """One saved window and what a restore does about it: the open window
    it goes to (None when that app is not open), and the geometry to give
    it, moved onto a monitor that is here now when its own is not."""
    saved: Window
    now: Window | None
    rect: tuple[int, int, int, int]
    offscreen: bool = False


def plan(saved: list[Window], current: list[Window],
         monitors: list[Monitor]) -> list[Step]:
    """Every saved window, in saved order, with its match and target."""
    pairs = {id(s): c for s, c in match(saved, current)}
    steps = []
    for s in saved:
        rect, offscreen = s.rect, False
        if monitors and not on_screen(rect, monitors):
            rect, offscreen = fit(rect, monitors), True
        steps.append(Step(s, pairs.get(id(s)), rect, offscreen))
    return steps


# ---------------------------------------------------------------------------
# Starting apps that aren't open
# ---------------------------------------------------------------------------


_PROMPT_TITLE = re.compile(r"^[^\s@]+@[^\s:]+:\s*(\S.*?)\s*$")


def terminal_dir(title: str) -> str | None:
    """The folder a terminal was in, from bash's default title
    (`matthew@laptop: ~/projects/x`), if that folder is still there."""
    m = _PROMPT_TITLE.match(title)
    if not m:
        return None
    folder = os.path.expanduser(m.group(1))
    return folder if os.path.isabs(folder) and os.path.isdir(folder) else None


def launch_argv(w: Window) -> list[str] | None:
    """The command that opens `w` again, or None when there is none to run:
    no command was recorded (another user's process, or one gone before
    the snapshot read it), or its program isn't there now."""
    argv = list(w.argv) or w.command.split()
    if not argv:
        return None
    exe = argv[0]
    if "/" not in exe:
        found = shutil.which(exe)
    elif os.path.isabs(exe):
        found = exe
    else:                                  # `.venv/bin/python`: from its cwd
        found = os.path.join(w.cwd, exe) if w.cwd else None
    if not found or not os.path.isfile(found) or not os.access(found, os.X_OK):
        return None
    #  LibreOffice's launcher hands soffice.bin a pipe to its splash screen;
    #  run straight, that descriptor is not there.
    argv = [a for a in argv if not a.startswith("--splash-pipe=")]
    name = os.path.basename(exe)
    if name not in TERMINALS or \
            any(a.startswith("--working-directory") for a in argv):
        return argv
    #  Folders that are gone open in home instead.
    tabs = [t if os.path.isdir(t) else str(Path.home()) for t in w.tabs]
    if name in TABBED and len(argv) == 1 and len(tabs) > 1:
        #  A plain terminal of several tabs: the same tabs, each in its
        #  folder.  (One started with a command of its own keeps it.)
        out = [exe, "--window", f"--working-directory={tabs[0]}"]
        for t in tabs[1:]:
            out += ["--tab", f"--working-directory={t}"]
        return out
    folder = tabs[0] if tabs else terminal_dir(w.title)
    if folder:
        #  Right after the program: anything after `--` is the shell's.
        argv.insert(1, f"--working-directory={folder}")
    return argv


def launch(w: Window) -> bool:
    """Start the app of saved window `w`, detached, in the folder it ran
    in (else home).  Returns whether it started."""
    argv = launch_argv(w)
    if argv is None:
        return False
    cwd = w.cwd if w.cwd and os.path.isdir(w.cwd) else str(Path.home())
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError:
        return False
    #  Reap it when it exits (a terminal's client exits at once, handing
    #  the window to its server), so a long-lived picker gathers no zombies.
    threading.Thread(target=proc.wait, daemon=True).start()
    return True


def _await_windows(want: dict[str, int], before: set[str], read,
                   timeout: float, quiet: float, sleep, clock) -> list[Window]:
    """Wait for new windows (ids not in `before`): until each class in
    `want` has that many, or none has appeared for `quiet` seconds after
    the first, or `timeout`.  Returns the new windows."""
    start = clock()
    last_change, seen = None, 0
    new: list[Window] = []
    while True:
        try:
            new = [w for w in read() if w.id not in before]
        except (OSError, subprocess.CalledProcessError):
            new = []
        have = {}
        for w in new:
            have[w.wm_class] = have.get(w.wm_class, 0) + 1
        if all(have.get(c, 0) >= n for c, n in want.items()):
            return new
        now = clock()
        if len(new) != seen:
            seen, last_change = len(new), now
        if now - start >= timeout or \
                (last_change is not None and now - last_change >= quiet):
            return new
        sleep(0.3)


def reopen(missing: list[Window], read=None, timeout: float = OPEN_WAIT_S,
           quiet: float = OPEN_QUIET_S, sleep=time.sleep,
           clock=time.monotonic, start=None) -> list[Window]:
    """Start the apps of saved windows that aren't open, and wait for their
    windows.  Returns the saved windows that could not be started.

    Two rounds.  The saved windows of one process (a browser's windows,
    every window of a terminal server) are started once first: a browser
    restores its own session, and starting it once per window would open
    each of them twice.  Then whatever that didn't bring back is started
    once per window -- the terminals, and browser windows its session
    didn't have.  An app none of whose windows came up in the first
    round isn't started again."""
    read = read or read_windows
    start = start or launch
    try:
        before = {w.id for w in read()}
    except (OSError, subprocess.CalledProcessError):
        before = set()
    runnable = [w for w in missing if launch_argv(w) is not None]
    failed = [w for w in missing if launch_argv(w) is None]
    want: dict[str, int] = {}
    for w in runnable:
        want[w.wm_class] = want.get(w.wm_class, 0) + 1

    def wait():
        return _await_windows(want, before, read, timeout, quiet, sleep, clock)

    firsts, seen = [], set()
    for w in runnable:
        key = (w.wm_class, w.pid)
        if key not in seen:
            seen.add(key)
            firsts.append(w)
    #  By identity: two saved terminals can be equal field for field.
    dud = {id(w) for w in firsts if not start(w)}
    failed += [w for w in firsts if id(w) in dud]
    if len(dud) == len(firsts):
        return failed
    new = wait()
    #  Which saved windows did that bring back?  Matched like a restore.
    #  An app with no window yet is slow or opens some other class of
    #  window: starting it again would only make two.
    covered = {id(s) for s, _ in match(runnable, new)}
    arrived = {w.wm_class for w in new}
    again = [w for w in runnable if id(w) not in covered and
             id(w) not in dud and w.wm_class in arrived]
    if again:
        failed += [w for w in again if not start(w)]
        wait()
    return failed


# ---------------------------------------------------------------------------
# Moving windows
# ---------------------------------------------------------------------------


def _geometry(wid: str) -> tuple[int, int, int, int] | None:
    for win in _list_windows():
        if win.id == wid:
            return win.rect
    return None


def _states(wid: str, action: str, names: list[str]) -> None:
    #  wmctrl -b takes at most two properties at a time.
    for k in range(0, len(names), 2):
        _run("wmctrl", "-i", "-r", wid, "-b", ",".join([action, *names[k:k + 2]]))


def place(wid: str, saved: Window, rect: tuple[int, int, int, int],
          desktops: int) -> tuple[int, int, int, int] | None:
    """Put one open window where `saved` was, `rect` being the geometry to
    give it.  Returns where it ended up (before re-maximizing)."""
    x, y, w, h = rect
    _run("wmctrl", "-i", "-r", wid, "-t", str(min(saved.desktop, desktops - 1))
         if saved.desktop >= 0 else "-1")
    #  Geometry only takes on a window that is not maximized or minimized.
    _states(wid, "remove", ["maximized_vert", "maximized_horz", "fullscreen",
                            "shaded"])
    _run("wmctrl", "-i", "-r", wid, "-e", f"0,{x},{y},{w},{h}")
    time.sleep(SETTLE_S)
    got = _geometry(wid)
    if got and (got[0], got[1]) != (x, y):
        dx, dy = got[0] - x, got[1] - y
        _run("wmctrl", "-i", "-r", wid, "-e", f"0,{x - dx},{y - dy},{w},{h}")
        time.sleep(SETTLE_S)
        got = _geometry(wid)
    keep = [s for s in saved.states if s != "hidden"]
    if keep:
        _states(wid, "add", keep)
    #  Minimized or not.  Marco ignores `wmctrl -b remove,hidden`; mapping
    #  the window un-minimizes it without focusing it or switching
    #  workspace (`wmctrl -R` would do both).  Geometry takes either way.
    xid = str(int(wid, 16))
    action = "windowminimize" if "hidden" in saved.states else "windowmap"
    subprocess.run(["xdotool", action, xid], capture_output=True)
    return got


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_snapshot(args) -> int:
    #  The timer fires through logins, logouts and crashes; an X server
    #  that is not there (yet, or any more) is a skipped snapshot, not an
    #  error in the journal every two minutes.
    try:
        windows = [w for w in read_windows()
                   if not w.wm_class.startswith(TEST_CLASS)]
    except (OSError, subprocess.CalledProcessError):
        if args.verbose:
            print("no X display to read; skipped")
        return 0
    session = session_key()
    force = getattr(args, "force", False)
    #  Seen on the last check: unchanged, even if that snapshot was deleted
    #  since -- a deleted layout stays deleted until a window moves.
    path = None if not force and seen_last(session, windows) else \
        save(windows, read_monitors(), session, force=force)
    mark_checked(session=session, windows=windows)
    prune()
    if args.verbose:
        print(f"saved {path}" if path else "unchanged")
    return 0


def app_name(w: Window) -> str:
    """`brave-browser.Brave-browser` -> `Brave-browser`."""
    return w.wm_class.split(".")[-1] or w.wm_class


def workspace_label(w: Window) -> str:
    return "all" if w.desktop < 0 else str(w.desktop + 1)


def _short(text: str, n: int = 60) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


def cmd_restore(args) -> int:
    every = snapshots()
    snap = pick(every, session_key(), args.which)
    if snap is None:
        print(f"no snapshot to restore ({args.which!r}); "
              f"`window-layout list` shows what there is", file=sys.stderr)
        return 1
    steps = plan(snap["windows"], read_windows(), read_monitors())
    opening = getattr(args, "open", False)
    print(f"restoring {snap['path'].name} (taken {snap['taken']}): "
          f"{sum(1 for st in steps if st.now)} of {len(steps)} windows "
          f"are open" + (" -- dry run" if args.dry_run else ""))
    if opening:
        closed = [st.saved for st in steps if not st.now]
        runnable = [s for s in closed if launch_argv(s) is not None]
        if runnable:
            print(f"{'would start' if args.dry_run else 'starting'} "
                  f"{len(runnable)} that are not open:")
            for s in runnable:
                print(f"  {app_name(s):<16} {_short(s.title, 50):<50} "
                      f"$ {_short(' '.join(launch_argv(s)), 60)}")
            if not args.dry_run:
                reopen(closed)
                steps = plan(snap["windows"], read_windows(), read_monitors())
    desktops = desktop_count()
    placed = [st for st in steps if st.now]
    missing = [st.saved for st in steps if not st.now]
    if opening and not args.dry_run:
        print(f"now {len(placed)} of {len(steps)} windows are open")
    elif opening:                          # listed above, as would start
        missing = [s for s in missing if launch_argv(s) is None]
    for st in placed:
        x, y, w, h = st.rect
        state = f" [{', '.join(st.saved.states)}]" if st.saved.states else ""
        note = ("  (its monitor is not connected; moved onto this one)"
                if st.offscreen else "")
        print(f"  {app_name(st.saved):<16} {_short(st.now.title, 50):<50} "
              f"-> ws {workspace_label(st.saved)} {x},{y} {w}x{h}"
              f"{state}{note}")
        if not args.dry_run:
            try:
                place(st.now.id, st.saved, st.rect, desktops)
            except subprocess.CalledProcessError as exc:
                print(f"    could not move it: {exc.stderr.strip() or exc}")
    if args.notify:
        apps = sorted({app_name(s) for s in missing})
        body = f"{len(placed)} of {len(steps)} windows put back " \
               f"(layout of {snap['taken'].replace('T', ' ')})."
        if apps:
            body += f"\nNot open: {', '.join(apps)}. Open them and restore again."
        subprocess.run(["notify-send", "-i", "window-layout",
                        "Window layout restored", body], capture_output=True)
    if missing:
        print(f"\nnot open now ({len(missing)}) -- open these and run "
              f"`window-layout restore` again to place them"
              + ("" if opening else
                 ", or `window-layout restore --open` to start them") + ":")
        for s in missing:
            print(f"  {app_name(s):<16} {_short(s.title, 50)}")
            if s.command:
                print(f"  {'':<16} $ {_short(s.command, 100)}")
    return 0


def cmd_list(args) -> int:
    every = snapshots()
    if not every:
        print(f"no snapshots in {SNAPSHOTS}")
        return 0
    here = session_key()
    mine = [s for s in every if s["session"] == here]
    print(checked_note(last_checked(), datetime.fromisoformat(
        mine[-1]["taken"]) if mine else None) + ".")
    by_session: dict[str, list[dict]] = {}
    for snap in every:
        by_session.setdefault(snap["session"], []).append(snap)
    for session, snaps in by_session.items():
        mark = "  (this session)" if session == here else ""
        print(f"session {session.split(':')[0][:8]}… pid {session.split(':')[1]}"
              f"{mark}: {len(snaps)} snapshot(s), "
              f"{snaps[0]['taken']} .. {snaps[-1]['taken']}")
        for snap in snaps[-args.last:]:
            apps = sorted({app_name(w) for w in snap["windows"]})
            n = len(snap["windows"])
            print(f"  {snap['path'].stem}  {n:>2} window{'s' * (n != 1):<1}  "
                  f"{_short(', '.join(apps), 70)}")
    return 0


def cmd_delete(args) -> int:
    every = snapshots()
    doomed: list[Path] = []
    for which in args.which:
        snap = pick(every, session_key(), which)
        if snap is None or snap["path"].parent != SNAPSHOTS:
            print(f"window-layout: no snapshot {which!r}", file=sys.stderr)
            return 1
        doomed += [s["path"] for s in every
                   if (s["session"] == snap["session"] if args.session
                       else s["path"] == snap["path"])]
    doomed = sorted(set(doomed))
    moved = delete(doomed)
    print(f"deleted {len(moved)} snapshot{'s' * (len(moved) != 1)}: "
          f"{_short(', '.join(p.stem for p in moved), 70)}")
    print(f"`window-layout undelete` brings {'them' if len(moved) > 1 else 'it'}"
          f" back for the next {UNDO_DAYS * 24} hours.")
    return 0


def cmd_undelete(args) -> int:
    back = undelete()
    print(f"brought back {len(back)} snapshot{'s' * (len(back) != 1)}"
          + (f": {_short(', '.join(p.stem for p in back), 70)}" if back else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="window-layout", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("snapshot", help="record the layout if it changed")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-f", "--force", action="store_true",
                   help="save even if nothing changed")
    p.set_defaults(func=cmd_snapshot)
    p = sub.add_parser("restore", help="put windows back")
    p.add_argument("which", nargs="?", default="previous",
                   help="previous (default: the last layout of the session "
                        "before this one), latest, or a snapshot name")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="say what would move, move nothing")
    p.add_argument("-o", "--open", action="store_true",
                   help="first start the apps that aren't open, from the "
                        "command each was running, then place them")
    p.add_argument("--notify", action="store_true",
                   help="also say how it went in a desktop notification "
                        "(for the menu entry)")
    p.set_defaults(func=cmd_restore)
    p = sub.add_parser("list", help="sessions and snapshots on file")
    p.add_argument("--last", type=int, default=3,
                   help="snapshots shown per session (default 3)")
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("delete", help="delete snapshots (undoable for a day)")
    p.add_argument("which", nargs="+",
                   help="snapshot names (20261027-194300), previous or latest")
    p.add_argument("-s", "--session", action="store_true",
                   help="delete every snapshot of the named snapshots' sessions")
    p.set_defaults(func=cmd_delete)
    p = sub.add_parser("undelete", help="bring back deleted snapshots")
    p.set_defaults(func=cmd_undelete)
    args = parser.parse_args(argv)
    for tool in ("wmctrl", "xprop"):
        if not shutil.which(tool):
            print(f"window-layout needs {tool}", file=sys.stderr)
            return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
