# window-layout

Records where every window on the desktop is, every two minutes, and puts
open windows back after a crash, a logout or a reboot.

Built 2026-10-27, after an out-of-memory thrash took the X session down and
a dozen windows' layout went with it. MATE's session autosave was off and
nothing else had written the layout down.

## Using it

After a crash, log back in and open your apps again. Then do one of these:

- **Double-click Window Layouts on the desktop** (it's also in the menu,
  under Accessories). That opens the picker.
- In the menu: **Restore Window Layout**. This puts windows back in one
  click, with no picker, and a notification says how it went.
- In a terminal: `window-layout restore`, or `window-layouts` for the
  picker.

### The picker

![The picker](docs/picker.png)

- **Left:** every snapshot on file, grouped by session: *This session*,
  *Previous session* (the one a crash or logout ended), then *Earlier
  session*. It opens with the previous session's last snapshot selected,
  which is the layout you lost.
- **Preview:** the chosen snapshot drawn to scale on the monitors it was
  taken on, each app in its own colour. Use **Workspace** to see one
  workspace at a time. Solid boxes are windows that are open now; dashed
  boxes are apps that aren't open. Select a row to outline that window.
- **List:** every window in the snapshot. Ticked windows are open now and
  will be moved; untick one to leave it where it is. Rows for apps that
  aren't open are greyed out.
- **Restore N windows** moves the ticked windows. The picker stays open, and
  the list updates every few seconds while it's open. Open a missing app
  and it becomes tickable, then press Restore again.
- The **camera button** saves the layout as it is right now, even if
  nothing has moved since the last snapshot.
- **Deleting:** select a snapshot and press the **trash button** or
  **Delete**, or right-click it. Select a session's heading to delete the
  whole session. An **Undo** bar appears at the bottom. Deleted snapshots
  can be brought back for a day (`window-layout undelete`), then they're
  gone for good. A deleted layout isn't saved again just because it is
  still on screen; the next snapshot comes when a window moves.
- **Under the title** it says when the desktop was last checked, e.g.
  *Checked just now; no window has moved since yesterday 23:03*. A
  snapshot is only saved when a window moves, so a newest snapshot from
  last night just means nothing has moved since. If the timer stops, this
  line says so. New snapshots appear while the picker is open.

Opening the picker a second time brings the open one to the front.

Windows that weren't open yet can be placed later: open them and restore
again. Running it twice is harmless.

```
window-layout restore            # the last layout of the session before this one
window-layout restore --dry-run  # say what would move, move nothing
window-layout restore latest     # the newest snapshot, this session included
window-layout restore 20261027-194300   # a particular snapshot
window-layout list               # sessions and their snapshots
window-layout snapshot -v        # take one now (if anything changed)
window-layout snapshot --force   # take one now, changed or not
window-layout delete 20261027-225125            # delete a snapshot
window-layout delete --session 20261027-225125  # ...or its whole session
window-layout undelete           # bring back what was deleted in the last day
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
  `~/.local/state/window-layout/snapshots/` only when the layout changed.
  A changed title alone doesn't count: a busy terminal's title has a
  spinner in it, and a browser's title is its current tab. Every run
  touches `snapshots/.checked`, saved or not, which is how the picker and
  `window-layout list` tell "unchanged" from "stopped". It
  exits quietly when there is no X display (during login, logout or a
  crash).
- **Sessions.** Each snapshot records its X session: the boot id plus the X
  server's pid and start time. `restore` defaults to the last snapshot of a
  session other than this one, which is the layout that was lost. Snapshots
  taken after you log back in can't hide it.
- **Pruning.** Snapshots are kept 14 days. The last snapshot of each of the
  five most recent sessions is kept regardless of age. Deleted snapshots
  wait in `snapshots/.deleted/` for a day, for undo, then are removed.
- **Deleted stays deleted.** `.checked` records the layout the last run
  saw, so a run that sees the same layout saves nothing, even if its
  snapshot was deleted in between.
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
| The picker (GTK 3) | `window_layout_gui.py`, symlinked as `~/.local/bin/window-layouts` |
| Tests | `test_window_layout.py` and `test_window_layout_gui.py`, run with `python3 -m unittest -v` here. The live tests open xmessage windows on `$DISPLAY`, and the picker tests click the real Restore button. |
| Icon | `window-layout.svg`, linked into `~/.local/share/icons/hicolor/scalable/apps/` |
| Desktop icon | `~/Desktop/window-layouts.desktop`, executable and marked trusted, so Caja runs it without asking |
| Timer and service | `~/.config/systemd/user/window-layout-snapshot.{timer,service}` |
| Menu entries | `~/.local/share/applications/window-layouts.desktop` (picker), `window-layout-restore.desktop` (one-click restore) |
| Snapshots | `~/.local/state/window-layout/snapshots/` |

To check that it's running, `window-layout list` starts with when it last
checked, or:

```
systemctl --user list-timers window-layout-snapshot.timer
journalctl --user -u window-layout-snapshot
```

To remove it:

```
systemctl --user disable --now window-layout-snapshot.timer
```

Then delete the files in the table above, and the desktop icon.
