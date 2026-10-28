# window-layout

Records where every window on the desktop is, every two minutes, and puts
open windows back after a crash, a logout or a reboot.

Built 2026-10-27, after an out-of-memory thrash took the X session down and
a dozen windows' layout went with it. MATE's session autosave was off and
nothing else had written the layout down.

## Using it

After a crash, log back in and open your apps again. Then do either of
these:

- In the menu: **Restore Window Layout**. A notification says how many
  windows went back and which apps weren't open yet.
- In a terminal: `window-layout restore`.

Windows that weren't open yet can be placed later: open them and restore
again. Running it twice is harmless.

```
window-layout restore            # the last layout of the session before this one
window-layout restore --dry-run  # say what would move, move nothing
window-layout restore latest     # the newest snapshot, this session included
window-layout restore 20261027-194300   # a particular snapshot
window-layout list               # sessions and their snapshots
window-layout snapshot -v        # take one now
```

## What it puts back

- Position and size
- Workspace (a window on all workspaces stays on all of them)
- Maximized, fullscreen, minimized, sticky, always-on-top and shaded states
- If a window was on a monitor that isn't connected now, it is moved onto
  the first monitor there is, and shrunk if it must be.

A window after a crash is a new X window, with a new id and often a new
title (a browser's title is its current tab). So saved windows are matched
to open ones of the same application (WM_CLASS), the closest title first,
and each window is used once. Saved windows that nothing matched are
listed with the command line that was running them, so you know what to
reopen.

## How it works

- **The snapshot.** `window-layout-snapshot.timer` (a systemd user timer)
  runs `window-layout snapshot` every two minutes. That reads `wmctrl -lGpx`
  and `xprop`, skips panels and the desktop, and writes a JSON file to
  `~/.local/state/window-layout/snapshots/` only when the layout changed. It
  exits quietly when there is no X display (during login, logout or a
  crash).
- **Sessions.** Each snapshot records its X session: the boot id plus the X
  server's pid and start time. `restore` defaults to the last snapshot of a
  session other than this one, which is the layout that was lost. Snapshots
  taken after you log back in can't hide it.
- **Pruning.** Snapshots are kept 14 days. The last snapshot of each of the
  five most recent sessions is kept regardless of age.
- **Marco's offset.** Marco does not hand back the coordinates it was given:
  a window set to (300, 200) can report (312, 270), by an offset that varies
  from window to window. So `restore` moves each window, reads where it
  landed, and corrects once. The live tests check positions to the pixel.
- **Minimized windows.** Marco ignores `wmctrl -b remove,hidden`, so
  un-minimizing is done with `xdotool windowmap`. That doesn't focus the
  window or switch workspace.

One limit: the newest snapshot can be up to two minutes older than the
crash. If the last snapshot of the old session caught a half-closed desktop
(for example, during a slow logout), `window-layout list` shows the earlier
ones, and `restore <name>` picks one.

## Files

| What | Where |
|---|---|
| The tool (stdlib Python, needs `wmctrl`, `xprop`, `xdotool`) | `window_layout.py`, symlinked as `~/.local/bin/window-layout` |
| Tests | `test_window_layout.py`, run with `python3 -m unittest -v` here. The live tests open xmessage windows on `$DISPLAY`. |
| Timer and service | `~/.config/systemd/user/window-layout-snapshot.{timer,service}` |
| Menu entry | `~/.local/share/applications/window-layout-restore.desktop` |
| Snapshots | `~/.local/state/window-layout/snapshots/` |

To check that it's running:

```
systemctl --user list-timers window-layout-snapshot.timer
journalctl --user -u window-layout-snapshot
```

To remove it:

```
systemctl --user disable --now window-layout-snapshot.timer
```

Then delete the files in the table above.
