#!/usr/bin/python3
"""Snapshot the desktop's window layout, and put it back after a crash.

    window-layout snapshot          record where every window is (the timer
                                    runs this every two minutes)
    window-layout restore           put open windows back where they were at
                                    the end of the previous X session
    window-layout restore --dry-run say what it would do, move nothing
    window-layout list              the sessions and snapshots on file

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
matched are listed with the command that was running them, to reopen.

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
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME")
                 or Path.home() / ".local" / "state") / "window-layout"
SNAPSHOTS = STATE_DIR / "snapshots"
KEEP_DAYS = 14
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
#: How long to let the window manager act on a request before reading back.
SETTLE_S = 0.15


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


def _command(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return " ".join(part.decode(errors="replace")
                    for part in raw.split(b"\0") if part)


def read_windows() -> list[Window]:
    windows = []
    for win in parse_wmctrl(_run("wmctrl", "-lGpx")):
        try:
            states, types = parse_xprop(_run(
                "xprop", "-id", win.id, "_NET_WM_STATE", "_NET_WM_WINDOW_TYPE"))
        except subprocess.CalledProcessError:
            continue                              # gone since wmctrl saw it
        if types & SKIP_TYPES:
            continue
        win.states = states
        win.command = _command(win.pid)
        windows.append(win)
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
    """What makes two snapshots the same layout: everything but window ids
    and pids, in a stable order."""
    return sorted(({k: v for k, v in asdict(w).items()
                    if k not in ("id", "pid")} for w in windows),
                  key=lambda d: json.dumps(d, sort_keys=True))


def load(path: Path) -> dict:
    data = json.loads(path.read_text())
    data["windows"] = [Window(**w) for w in data["windows"]]
    data["monitors"] = [Monitor(**m) for m in data.get("monitors", [])]
    data["path"] = path
    return data


def snapshots(folder: Path = SNAPSHOTS) -> list[dict]:
    """Every snapshot on file, oldest first; unreadable ones skipped."""
    out = []
    for path in sorted(folder.glob("*.json")):
        try:
            out.append(load(path))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return out


def save(windows: list[Window], monitors: list[Monitor], session: str,
         folder: Path = SNAPSHOTS, now: datetime | None = None) -> Path | None:
    """Write a snapshot if the layout differs from the last one of this
    session; return its path, or None when nothing changed (or there is
    nothing to save)."""
    if not windows:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    same_session = [s for s in snapshots(folder) if s["session"] == session]
    if same_session and _layout(same_session[-1]["windows"]) == _layout(windows):
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


def prune(folder: Path = SNAPSHOTS, now: datetime | None = None) -> list[Path]:
    """Drop snapshots older than KEEP_DAYS, except the last of each of the
    newest KEEP_SESSIONS sessions.  Returns what was removed."""
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


# ---------------------------------------------------------------------------
# Moving windows
# ---------------------------------------------------------------------------


def _geometry(wid: str) -> tuple[int, int, int, int] | None:
    for win in parse_wmctrl(_run("wmctrl", "-lGpx")):
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
        windows = read_windows()
    except (OSError, subprocess.CalledProcessError):
        if args.verbose:
            print("no X display to read; skipped")
        return 0
    path = save(windows, read_monitors(), session_key())
    prune()
    if args.verbose:
        print(f"saved {path}" if path else "unchanged")
    return 0


def _short(text: str, n: int = 60) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


def cmd_restore(args) -> int:
    every = snapshots()
    snap = pick(every, session_key(), args.which)
    if snap is None:
        print(f"no snapshot to restore ({args.which!r}); "
              f"`window-layout list` shows what there is", file=sys.stderr)
        return 1
    current = read_windows()
    monitors = read_monitors()
    desktops = desktop_count()
    pairs = match(snap["windows"], current)
    print(f"restoring {snap['path'].name} (taken {snap['taken']}): "
          f"{len(pairs)} of {len(snap['windows'])} windows are open"
          + (" -- dry run" if args.dry_run else ""))
    for saved, now in pairs:
        rect = saved.rect
        note = ""
        if not on_screen(rect, monitors):
            rect = fit(rect, monitors)
            note = "  (its monitor is not connected; moved onto this one)"
        state = f" [{', '.join(saved.states)}]" if saved.states else ""
        desk = "all" if saved.desktop < 0 else saved.desktop + 1
        print(f"  {saved.wm_class.split('.')[-1]:<16} {_short(now.title, 50):<50} "
              f"-> ws {desk} {rect[0]},{rect[1]} {rect[2]}x{rect[3]}{state}{note}")
        if not args.dry_run:
            try:
                place(now.id, saved, rect, desktops)
            except subprocess.CalledProcessError as exc:
                print(f"    could not move it: {exc.stderr.strip() or exc}")
    matched = {id(s) for s, _ in pairs}
    missing = [s for s in snap["windows"] if id(s) not in matched]
    if args.notify:
        apps = sorted({s.wm_class.split(".")[-1] for s in missing})
        body = f"{len(pairs)} of {len(snap['windows'])} windows put back " \
               f"(layout of {snap['taken'].replace('T', ' ')})."
        if apps:
            body += f"\nNot open: {', '.join(apps)}. Open them and restore again."
        subprocess.run(["notify-send", "-i", "preferences-system-windows",
                        "Window layout restored", body], capture_output=True)
    if missing:
        print(f"\nnot open now ({len(missing)}) -- open these and run "
              f"`window-layout restore` again to place them:")
        for s in missing:
            print(f"  {s.wm_class.split('.')[-1]:<16} {_short(s.title, 50)}")
            if s.command:
                print(f"  {'':<16} $ {_short(s.command, 100)}")
    return 0


def cmd_list(args) -> int:
    every = snapshots()
    if not every:
        print(f"no snapshots in {SNAPSHOTS}")
        return 0
    here = session_key()
    by_session: dict[str, list[dict]] = {}
    for snap in every:
        by_session.setdefault(snap["session"], []).append(snap)
    for session, snaps in by_session.items():
        mark = "  (this session)" if session == here else ""
        print(f"session {session.split(':')[0][:8]}… pid {session.split(':')[1]}"
              f"{mark}: {len(snaps)} snapshot(s), "
              f"{snaps[0]['taken']} .. {snaps[-1]['taken']}")
        for snap in snaps[-args.last:]:
            apps = sorted({w.wm_class.split('.')[-1] for w in snap["windows"]})
            print(f"  {snap['path'].stem}  {len(snap['windows']):>2} windows  "
                  f"{_short(', '.join(apps), 70)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="window-layout", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("snapshot", help="record the layout if it changed")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_snapshot)
    p = sub.add_parser("restore", help="put windows back")
    p.add_argument("which", nargs="?", default="previous",
                   help="previous (default: the last layout of the session "
                        "before this one), latest, or a snapshot name")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="say what would move, move nothing")
    p.add_argument("--notify", action="store_true",
                   help="also say how it went in a desktop notification "
                        "(for the menu entry)")
    p.set_defaults(func=cmd_restore)
    p = sub.add_parser("list", help="sessions and snapshots on file")
    p.add_argument("--last", type=int, default=3,
                   help="snapshots shown per session (default 3)")
    p.set_defaults(func=cmd_list)
    args = parser.parse_args(argv)
    for tool in ("wmctrl", "xprop"):
        if not shutil.which(tool):
            print(f"window-layout needs {tool}", file=sys.stderr)
            return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
