#!/usr/bin/python3
"""Window Layouts: pick a saved layout and put your windows back.

The picker over `window_layout`: every snapshot on file, grouped by the X
session it was taken in, with the session before this one -- the layout a
crash or logout lost -- chosen to start.  Choosing a snapshot draws it to
scale, workspace by workspace, and lists its windows: each one open now
(moved by Restore), not open but startable (Restore starts it from the
command it was running, then places it), or not open with nothing to run.
Untick a window to leave it alone.  The list follows the desktop while the
picker is open, so an app you open from here on joins the restore without
reopening the picker.

Snapshots can be deleted one at a time or a session at a time (the trash
button, the Delete key, or a right-click), with Undo.
"""

from __future__ import annotations

import colorsys
import hashlib
import subprocess
import threading
from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

import window_layout as wl  # noqa: E402

APP_ID = "local.window-layout.picker"
#: How often the window list re-reads the desktop while the picker is open.
REFRESH_S = 3


# ---------------------------------------------------------------------------
# Pure helpers (tested without a display)
# ---------------------------------------------------------------------------


def app_color(wm_class: str) -> tuple[float, float, float]:
    """A steady, distinct colour per application: the same app is the same
    colour in every snapshot."""
    digest = hashlib.md5(wm_class.lower().encode()).digest()
    hue = digest[0] / 255
    return colorsys.hsv_to_rgb(hue, 0.55, 0.92)


def bounds(monitors: list[wl.Monitor],
           windows: list[wl.Window]) -> tuple[int, int, int, int]:
    """The area a preview draws: the monitors, or the windows' extent when
    a snapshot recorded none."""
    rects = [(m.x, m.y, m.w, m.h) for m in monitors] or \
        [w.rect for w in windows] or [(0, 0, 1920, 1080)]
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[0] + r[2] for r in rects)
    y1 = max(r[1] + r[3] for r in rects)
    return x0, y0, x1 - x0, y1 - y0


def scaler(area: tuple[int, int, int, int], width: int, height: int,
           margin: int = 12):
    """(x, y, w, h) on the desktop -> the same rectangle in a widget of
    width x height, aspect kept, centred."""
    ax, ay, aw, ah = area
    scale = min((width - 2 * margin) / max(aw, 1),
                (height - 2 * margin) / max(ah, 1))
    ox = (width - aw * scale) / 2
    oy = (height - ah * scale) / 2

    def to_widget(rect):
        x, y, w, h = rect
        return (ox + (x - ax) * scale, oy + (y - ay) * scale,
                w * scale, h * scale)
    return to_widget


def session_groups(every: list[dict],
                   here: str) -> list[tuple[str, str, list[dict]]]:
    """Snapshots by session, newest session first, each as (name, span,
    snapshots newest first): "This session", "Previous session", then
    "Earlier session", with the times it covers."""
    order: dict[str, list[dict]] = {}
    for snap in every:
        order.setdefault(snap["session"], []).append(snap)
    sessions = sorted(order.items(), key=lambda kv: kv[1][-1]["taken"],
                      reverse=True)
    out, seen_previous = [], False
    for key, snaps in sessions:
        first = datetime.fromisoformat(snaps[0]["taken"])
        last = datetime.fromisoformat(snaps[-1]["taken"])
        span = (f"{first:%a %b %-d, %H:%M}" if first.date() == last.date()
                else f"{first:%b %-d %H:%M}")
        span += f" – {last:%H:%M}" if first.date() == last.date() \
            else f" – {last:%b %-d %H:%M}"
        if key == here:
            name = "This session"
        elif not seen_previous:
            name = "Previous session"
            seen_previous = True
        else:
            name = "Earlier session"
        out.append((name, span, list(reversed(snaps))))
    return out


def windows(n: int) -> str:
    return f"{n} window{'s' * (n != 1)}"


def snapshots(n: int) -> str:
    return f"{n} snapshot{'s' * (n != 1)}"


def state_label(states: list[str]) -> str:
    """How a saved window was, in words: "maximized", "minimized"..."""
    words = []
    vert, horz = "maximized_vert" in states, "maximized_horz" in states
    if vert and horz:
        words.append("maximized")
    elif vert:
        words.append("maximized vertically")
    elif horz:
        words.append("maximized horizontally")
    names = {"fullscreen": "fullscreen", "hidden": "minimized",
             "sticky": "on every workspace", "above": "always on top",
             "shaded": "rolled up"}
    words += [names[s] for s in states if s in names]
    return ", ".join(words)


def startable(st: wl.Step) -> bool:
    """Not open, but Restore can start it."""
    return st.now is None and wl.launch_argv(st.saved) is not None


def summary(steps: list[wl.Step]) -> str:
    open_ = sum(1 for st in steps if st.now)
    text = f"{open_} of {windows(len(steps))} " \
           f"{'is' if open_ == 1 and len(steps) == 1 else 'are'} open now"
    start = sorted({wl.app_name(st.saved) for st in steps if startable(st)})
    if start:
        text += f"  ·  can be started: {', '.join(start)}"
    missing = sorted({wl.app_name(st.saved) for st in steps
                      if not st.now and not startable(st)})
    if missing:
        text += f"  ·  not open: {', '.join(missing)}"
    return text


# ---------------------------------------------------------------------------
# Icons
# ---------------------------------------------------------------------------

_icon_cache: dict[str, Gio.Icon | None] = {}


def app_icon(wm_class: str) -> Gio.Icon:
    """The app's own icon: its WM_CLASS names in the icon theme, else the
    .desktop file that claims it (StartupWMClass or a matching name)."""
    if wm_class in _icon_cache:
        return _icon_cache[wm_class]
    theme = Gtk.IconTheme.get_default()
    #  res_name.res_class -- and res_name may have dots of its own
    #  (`org.inkscape.Inkscape.Inkscape`).
    name, _, cls = wm_class.rpartition(".")
    name = name or cls
    icon = None
    for candidate in (name, name.lower(), cls.lower()):
        if candidate and theme.has_icon(candidate):
            icon = Gio.ThemedIcon.new(candidate)
            break
    if icon is None:
        for info in Gio.AppInfo.get_all():
            wmc = info.get_startup_wm_class() if isinstance(
                info, Gio.DesktopAppInfo) else None
            ident = (info.get_id() or "").removesuffix(".desktop").lower()
            exe = (info.get_executable() or "").rsplit("/", 1)[-1].lower()
            names = (name.lower(), cls.lower())
            if (wmc and wmc.lower() in names) or ident in names or \
                    exe in names:
                icon = info.get_icon()
                if icon:
                    break
    icon = icon or Gio.ThemedIcon.new("application-x-executable")
    _icon_cache[wm_class] = icon
    return icon


# ---------------------------------------------------------------------------
# The preview
# ---------------------------------------------------------------------------


class Preview(Gtk.DrawingArea):
    """The chosen snapshot to scale: its monitors, and the windows on one
    workspace (or all), each in its app's colour, the one picked in the
    list outlined."""

    def __init__(self):
        super().__init__()
        self.set_size_request(-1, 240)
        self.snap = None
        self.steps: list[wl.Step] = []
        self.workspace: int | None = None          # None: every workspace
        self.highlight: wl.Window | None = None
        self.connect("draw", self._draw)

    def show_snapshot(self, snap, steps):
        self.snap, self.steps = snap, steps
        self.queue_draw()

    def _draw(self, widget, cr):
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()
        style = widget.get_style_context()
        fg = style.get_color(Gtk.StateFlags.NORMAL)
        if not self.snap:
            return
        monitors = self.snap["monitors"]
        to = scaler(bounds(monitors, [st.saved for st in self.steps]),
                    width, height)
        #  Monitors: the screens the windows were laid out on.
        for m in monitors or []:
            x, y, w, h = to((m.x, m.y, m.w, m.h))
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.06)
            cr.rectangle(x, y, w, h)
            cr.fill_preserve()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.35)
            cr.set_line_width(1)
            cr.stroke()
        shown = [st for st in self.steps
                 if self.workspace is None or st.saved.desktop in
                 (self.workspace, -1)]
        for st in shown:
            if "hidden" in st.saved.states:
                continue                           # minimized: not on screen
            x, y, w, h = to(st.saved.rect)
            r, g, b = app_color(st.saved.wm_class)
            picked = st.saved is self.highlight
            dim = 1.0 if (self.highlight is None or picked) else 0.45
            alpha = (0.55 if st.now else 0.22) * dim
            cr.set_source_rgba(r, g, b, alpha)
            cr.rectangle(x, y, w, h)
            cr.fill_preserve()
            cr.set_source_rgba(r, g, b, (1.0 if st.now else 0.6) * dim)
            cr.set_line_width(2.5 if picked else 1)
            if not st.now:
                cr.set_dash([4, 3])
            cr.stroke()
            cr.set_dash([])
            #  The app's name, if the box has room for it.
            if w > 40 and h > 16:
                self._chip(cr, wl.app_name(st.saved), x + 4, y + 4, w - 8,
                           (r, g, b), dim)
        #  Monitor names last, so no window covers them.
        cr.select_font_face("Sans")
        cr.set_font_size(10)
        for m in monitors or []:
            x, y, w, h = to((m.x, m.y, m.w, m.h))
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
            cr.move_to(x + 6, y + h - 6)
            cr.show_text(m.name)

    @staticmethod
    def _chip(cr, text, x, y, room, rgb, dim):
        """A label on a dark chip in the app's colour, cut to `room`."""
        cr.save()
        cr.select_font_face("Sans")
        cr.set_font_size(11)
        while text and cr.text_extents(text).x_advance + 10 > room:
            text = text[:-2] + "…" if len(text) > 2 else ""
        if text:
            ext = cr.text_extents(text)
            cr.set_source_rgba(0.08, 0.08, 0.1, 0.78 * dim)
            cr.rectangle(x, y, ext.x_advance + 10, 17)
            cr.fill()
            r, g, b = rgb
            cr.set_source_rgba(min(1, r + 0.25), min(1, g + 0.25),
                               min(1, b + 0.25), dim)
            cr.move_to(x + 5, y + 12.5)
            cr.show_text(text)
        cr.restore()


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------

# Columns of the snapshot tree.
S_LABEL, S_DETAIL, S_PATH, S_BOLD, S_SESSION = range(5)
# Columns of the window list.
W_TICK, W_CAN, W_ICON, W_APP, W_TITLE, W_WS, W_STATUS, W_INDEX, W_DIM = \
    range(9)


class Picker(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Window Layouts")
        self.set_default_size(1040, 680)
        self.set_icon_name("window-layout")
        self.every: list[dict] = []
        self.snap: dict | None = None
        self.steps: list[wl.Step] = []
        #  Saved windows the user unticked, by position in the snapshot;
        #  kept across the periodic refresh.
        self.unticked: set[int] = set()
        self.busy = False
        #  The snapshot the window list was last built for.
        self.planned: dict | None = None

        self.header = header = Gtk.HeaderBar(show_close_button=True,
                                             title="Window Layouts")
        self.set_titlebar(header)
        self.restore_button = Gtk.Button(label="Restore")
        self.restore_button.get_style_context().add_class("suggested-action")
        self.restore_button.connect("clicked", self.on_restore)
        header.pack_end(self.restore_button)
        snap_button = Gtk.Button.new_from_icon_name("camera-photo-symbolic",
                                                    Gtk.IconSize.BUTTON)
        snap_button.set_tooltip_text("Save the layout as it is now")
        snap_button.connect("clicked", self.on_snapshot_now)
        header.pack_start(snap_button)
        self.delete_button = Gtk.Button.new_from_icon_name(
            "user-trash-symbolic", Gtk.IconSize.BUTTON)
        self.delete_button.connect("clicked", self.on_delete)
        header.pack_start(self.delete_button)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(340)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.pack_start(paned, True, True, 0)
        self.add(outer)

        # Left: sessions and their snapshots.
        self.tree_store = Gtk.TreeStore(str, str, str, int, str)
        self.tree = Gtk.TreeView(model=self.tree_store, headers_visible=False)
        cell = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
        col = Gtk.TreeViewColumn("Snapshot", cell, markup=S_LABEL)
        col.set_expand(True)
        self.tree.append_column(col)
        detail = Gtk.CellRendererText(xalign=1.0)
        detail.set_property("foreground-rgba", Gdk.RGBA(0.6, 0.6, 0.6, 1))
        self.tree.append_column(Gtk.TreeViewColumn("", detail, text=S_DETAIL))
        self.tree.get_selection().connect("changed", self.on_snapshot_picked)
        self.tree.connect("key-press-event", self.on_tree_key)
        self.tree.connect("button-press-event", self.on_tree_click)
        self.menu = Gtk.Menu()
        self.menu_delete = Gtk.MenuItem(label="Delete")
        self.menu_delete.connect("activate", self.on_delete)
        self.menu.append(self.menu_delete)
        self.menu.show_all()
        left = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        left.add(self.tree)
        paned.pack1(left, resize=False, shrink=False)

        # Right: the preview, workspaces, the window list, the summary.
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                        margin=10)
        paned.pack2(right, resize=True, shrink=False)
        self.title_label = Gtk.Label(xalign=0)
        right.pack_start(self.title_label, False, False, 0)
        self.preview = Preview()
        frame = Gtk.Frame()
        frame.add(self.preview)
        right.pack_start(frame, False, True, 0)
        self.workspaces = Gtk.Box(spacing=0)
        self.workspaces.get_style_context().add_class("linked")
        ws_row = Gtk.Box(spacing=8)
        ws_row.pack_start(Gtk.Label(label="Workspace"), False, False, 0)
        ws_row.pack_start(self.workspaces, False, False, 0)
        right.pack_start(ws_row, False, False, 0)

        self.list_store = Gtk.ListStore(bool, bool, Gio.Icon, str, str, str,
                                        str, int, bool)
        self.list = Gtk.TreeView(model=self.list_store)
        tick = Gtk.CellRendererToggle()
        tick.connect("toggled", self.on_ticked)
        self.list.append_column(Gtk.TreeViewColumn(
            "", tick, active=W_TICK, activatable=W_CAN, sensitive=W_CAN))
        app_col = Gtk.TreeViewColumn("Application")
        icon = Gtk.CellRendererPixbuf()
        app_col.pack_start(icon, False)
        app_col.add_attribute(icon, "gicon", W_ICON)
        name = Gtk.CellRendererText()
        app_col.pack_start(name, True)
        app_col.add_attribute(name, "text", W_APP)
        app_col.add_attribute(name, "sensitive", W_CAN)
        self.list.append_column(app_col)
        for title, column, expand in (("Window", W_TITLE, True),
                                      ("Workspace", W_WS, False),
                                      ("Now", W_STATUS, False)):
            text = Gtk.CellRendererText(
                ellipsize=Pango.EllipsizeMode.END if expand else
                Pango.EllipsizeMode.NONE)
            c = Gtk.TreeViewColumn(title, text, text=column)
            c.add_attribute(text, "sensitive", W_CAN)
            c.set_expand(expand)
            c.set_resizable(expand)
            self.list.append_column(c)
        self.list.get_selection().connect("changed", self.on_window_picked)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.add(self.list)
        right.pack_start(scroll, True, True, 0)

        self.status = Gtk.Label(xalign=0, wrap=True)
        right.pack_start(self.status, False, False, 0)

        #  After a delete: what went, and Undo.
        self.undo_bar = Gtk.InfoBar(show_close_button=True)
        self.undo_label = Gtk.Label(xalign=0, wrap=True)
        self.undo_bar.get_content_area().add(self.undo_label)
        self.undo_bar.add_button("Undo", Gtk.ResponseType.REJECT)
        self.undo_bar.connect("response", self.on_undo_bar)
        self.undo_bar.set_no_show_all(True)
        self.undo_bar.get_content_area().show_all()
        self.deleted: list = []
        outer.pack_start(self.undo_bar, False, False, 0)

        self.load()
        GLib.timeout_add_seconds(REFRESH_S, self.on_tick)

    # -- data ------------------------------------------------------------

    def load(self, select: str | None = None):
        """(Re)read the snapshots and select one: `select` by path, else
        the one `restore` would use."""
        self.seen = self.on_file()
        self.every = wl.snapshots()
        here = wl.session_key()
        self.show_checked()
        default = wl.pick(self.every, here, "previous") or \
            (self.every[-1] if self.every else None)
        paths = {str(s["path"]) for s in self.every}
        #  The one asked for, unless it has since been pruned.
        want = select if select in paths else \
            (str(default["path"]) if default else None)
        self.tree_store.clear()
        chosen = None
        for name, span, snaps in session_groups(self.every, here):
            parent = self.tree_store.append(None, [
                f"<b>{name}</b>\n<span size='small' alpha='60%'>"
                f"{GLib.markup_escape_text(span)}</span>",
                f"{len(snaps)}", "", 1, snaps[0]["session"]])
            for snap in snaps:
                taken = datetime.fromisoformat(snap["taken"])
                apps = ", ".join(sorted({wl.app_name(w)
                                         for w in snap["windows"]}))
                row = self.tree_store.append(parent, [
                    f"{taken:%H:%M:%S}  <span size='small' alpha='60%'>"
                    f"{GLib.markup_escape_text(apps)}</span>",
                    windows(len(snap["windows"])), str(snap["path"]), 0,
                    snap["session"]])
                if str(snap["path"]) == want:
                    chosen = row
        if chosen is None:
            self.snap = None
            self.steps = []
            self.list_store.clear()
            self.fill_workspaces()
            self.preview.show_snapshot(None, [])
            self.status.set_text("")
            self.update_delete()
            self.title_label.set_markup(
                "<b>No snapshots yet.</b>  The timer saves one every two "
                "minutes; the camera button saves one now.")
            self.restore_button.set_sensitive(False)
            return
        path = self.tree_store.get_path(chosen)
        self.tree.expand_to_path(path)
        #  Open this session and the previous one; older ones stay folded.
        top = self.tree_store.get_iter_first()
        for _ in range(2):
            if top is None:
                break
            self.tree.expand_row(self.tree_store.get_path(top), False)
            top = self.tree_store.iter_next(top)
        self.tree.get_selection().select_iter(chosen)
        self.tree.scroll_to_cell(path, None, True, 0.3, 0)

    @staticmethod
    def on_file() -> list[str]:
        return sorted(p.name for p in wl.SNAPSHOTS.glob("*.json"))

    def show_checked(self):
        """The subtitle: when the desktop was last read, so a layout that
        hasn't changed since last night doesn't look like a stopped
        timer."""
        here = wl.session_key()
        mine = [s for s in self.every if s["session"] == here]
        newest = datetime.fromisoformat(mine[-1]["taken"]) if mine else None
        self.header.set_subtitle(wl.checked_note(wl.last_checked(), newest))

    def replan(self):
        """Match the chosen snapshot against the windows open now."""
        if not self.snap:
            return
        try:
            current = wl.read_windows()
            monitors = wl.read_monitors()
        except (OSError, subprocess.CalledProcessError):
            current, monitors = [], []
        steps = wl.plan(self.snap["windows"], current, monitors)
        #  Rebuild the list only when a match changed: the refresh runs
        #  every few seconds, and a rebuild would reset the scroll.
        same = [(st.now and st.now.id, st.offscreen) for st in steps] == \
            [(st.now and st.now.id, st.offscreen) for st in self.steps] and \
            len(self.list_store) == len(steps) and self.snap is self.planned
        self.planned = self.snap
        self.steps = steps
        if not same:
            self.fill_list()
        self.preview.show_snapshot(self.snap, self.steps)
        self.status.set_text(summary(self.steps))
        self.update_button()

    def fill_list(self):
        picked = self.selected_index()
        self.list_store.clear()
        for i, st in enumerate(self.steps):
            can = bool(st.now) or startable(st)
            if st.now:
                status = "open" + ("  (screen gone: fitted)"
                                   if st.offscreen else "")
            elif can:
                status = "can start"
            else:
                status = "not open"
            title = st.saved.title or "(untitled)"
            how = state_label(st.saved.states)
            if how:
                title += f"   [{how}]"
            self.list_store.append([
                can and i not in self.unticked, can,
                app_icon(st.saved.wm_class), wl.app_name(st.saved), title,
                wl.workspace_label(st.saved), status, i, not st.now])
        if picked is not None and picked < len(self.list_store):
            self.list.get_selection().select_path(Gtk.TreePath(picked))
        self.fill_workspaces()

    def fill_workspaces(self):
        for child in self.workspaces.get_children():
            self.workspaces.remove(child)
        used = sorted({st.saved.desktop for st in self.steps
                       if st.saved.desktop >= 0})
        first = None
        for ws in [None, *used]:
            label = "All" if ws is None else str(ws + 1)
            button = Gtk.RadioButton.new_with_label_from_widget(first, label)
            button.set_mode(False)                 # a toggle, not a radio dot
            first = first or button
            if ws == self.preview.workspace:
                button.set_active(True)
            button.connect("toggled", self.on_workspace, ws)
            self.workspaces.pack_start(button, False, False, 0)
        self.workspaces.show_all()

    def selected_index(self) -> int | None:
        model, it = self.list.get_selection().get_selected()
        return model[it][W_INDEX] if it else None

    def update_button(self):
        ticked = [row[W_INDEX] for row in self.list_store if row[W_TICK]]
        n = len(ticked)
        start = sum(1 for i in ticked
                    if i < len(self.steps) and not self.steps[i].now)
        self.restore_button.set_label(f"Restore {windows(n)}" if n
                                      else "Restore")
        self.restore_button.set_tooltip_text(
            f"Start {start} that {'is' if start == 1 else 'are'} not open, "
            f"then put all {n} back" if start else
            "Put the ticked windows back")
        self.restore_button.set_sensitive(n > 0 and not self.busy)

    # -- events ----------------------------------------------------------

    def on_snapshot_picked(self, selection):
        self.update_delete()
        model, it = selection.get_selected()
        if it is None or not model[it][S_PATH]:
            return
        path = model[it][S_PATH]
        snap = next((s for s in self.every if str(s["path"]) == path), None)
        if snap is None:
            return
        if self.snap is None or self.snap["path"] != snap["path"]:
            self.unticked.clear()
            self.preview.workspace = None
            self.preview.highlight = None
        self.snap = snap
        taken = datetime.fromisoformat(snap["taken"])
        self.title_label.set_markup(
            f"<b>{taken:%A, %B %-d at %H:%M:%S}</b>   "
            f"<span alpha='60%'>{windows(len(snap['windows']))}  ·  "
            f"{GLib.markup_escape_text(snap['path'].name)}</span>")
        self.replan()

    def on_window_picked(self, selection):
        i = self.selected_index()
        self.preview.highlight = self.steps[i].saved if i is not None and \
            i < len(self.steps) else None
        self.preview.queue_draw()

    def on_ticked(self, cell, path):
        row = self.list_store[path]
        row[W_TICK] = not row[W_TICK]
        (self.unticked.discard if row[W_TICK] else self.unticked.add)(
            row[W_INDEX])
        self.update_button()

    def on_workspace(self, button, ws):
        if button.get_active():
            self.preview.workspace = ws
            self.preview.queue_draw()

    def on_tick(self):
        if not self.busy and self.get_visible():
            #  The timer saves while the picker is open: show new snapshots,
            #  keeping the one picked.
            if self.on_file() != self.seen:
                self.load(select=str(self.snap["path"]) if self.snap else None)
            else:
                self.show_checked()
            self.replan()
        return True

    def on_snapshot_now(self, button):
        #  Asked for: saved even if nothing moved since the last one.
        wl.cmd_snapshot(type("Args", (), {"verbose": False, "force": True})())
        self.load(select=str(self.snap["path"]) if self.snap else None)

    # -- deleting --------------------------------------------------------

    def doomed(self) -> tuple[list, str]:
        """What Delete would delete, from the row selected: that snapshot,
        or every snapshot of a session row.  Returns (snapshots, what)."""
        model, it = self.tree.get_selection().get_selected()
        if it is None:
            return [], ""
        row = model[it]
        if row[S_PATH]:
            snaps = [s for s in self.every if str(s["path"]) == row[S_PATH]]
            if not snaps:
                return [], ""
            taken = datetime.fromisoformat(snaps[0]["taken"])
            return snaps, f"the {taken:%H:%M:%S} snapshot"
        snaps = [s for s in self.every if s["session"] == row[S_SESSION]]
        name = next((n for n, _, group in
                     session_groups(self.every, wl.session_key())
                     if group[0]["session"] == row[S_SESSION]), "the session")
        return snaps, f"{name.lower()}: {snapshots(len(snaps))}"

    def update_delete(self):
        snaps, what = self.doomed()
        self.delete_button.set_sensitive(bool(snaps))
        self.menu_delete.set_sensitive(bool(snaps))
        self.delete_button.set_tooltip_text(
            f"Delete {what}" if snaps else "Delete the selected snapshot or session")
        self.menu_delete.set_label(
            "Delete this snapshot" if what.startswith("the ") else
            f"Delete this session ({snapshots(len(snaps))})")

    def on_delete(self, *_):
        snaps, what = self.doomed()
        if not snaps:
            return
        gone = {str(s["path"]) for s in snaps}
        #  Then select the next one down the list (an older snapshot), or
        #  else the one above: the list doesn't jump to the top.
        order = [str(s["path"]) for _, _, group in
                 session_groups(self.every, wl.session_key()) for s in group]
        first = min(order.index(g) for g in gone)
        after = [o for o in order[first:] if o not in gone] + \
            [o for o in reversed(order[:first]) if o not in gone]
        keep = self.snap and str(self.snap["path"])
        select = keep if keep and keep not in gone else \
            (after[0] if after else None)
        self.deleted = wl.delete([s["path"] for s in snaps])
        self.load(select=select)
        self.undo_label.set_text(f"Deleted {what}.")
        self.undo_bar.set_message_type(Gtk.MessageType.INFO)
        self.undo_bar.show()

    def on_undo_bar(self, bar, response):
        if response == Gtk.ResponseType.REJECT and self.deleted:
            back = wl.undelete(self.deleted)
            self.load(select=str(back[0]) if back else None)
        self.deleted = []
        bar.hide()

    def on_tree_key(self, tree, event):
        if event.keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            self.on_delete()
            return True
        return False

    def on_tree_click(self, tree, event):
        if event.type != Gdk.EventType.BUTTON_PRESS or event.button != 3:
            return False
        hit = tree.get_path_at_pos(int(event.x), int(event.y))
        if hit is None:
            return False
        tree.get_selection().select_path(hit[0])
        self.update_delete()
        self.menu.popup_at_pointer(event)
        return True

    def on_restore(self, button):
        ticked = [row[W_INDEX] for row in self.list_store if row[W_TICK]]
        if not ticked or not self.snap:
            return
        self.busy = True
        self.update_button()
        closed = [self.steps[i].saved for i in ticked if not self.steps[i].now]
        self.status.set_text(
            f"Starting {windows(len(closed))}…" if closed else
            f"Putting back {windows(len(ticked))}…")
        threading.Thread(target=self._restore,
                         args=(self.snap, list(self.steps), ticked, closed),
                         daemon=True).start()

    def _restore(self, snap, steps, ticked, closed):
        """Off the GTK thread: starting apps and placing windows wait on
        them and on the window manager, and the picker should not freeze
        while they do."""
        unstarted = []
        if closed:
            unstarted = wl.reopen(closed)
            #  Matched again: the new windows, and every saved window's
            #  place in the snapshot, ticked by position, as before.
            try:
                current, monitors = wl.read_windows(), wl.read_monitors()
            except (OSError, subprocess.CalledProcessError):
                current, monitors = [], []
            steps = wl.plan(snap["windows"], current, monitors)
            GLib.idle_add(self.status.set_text,
                          f"Putting back {windows(len(ticked))}…")
        desktops = wl.desktop_count()
        moved, failed = [], []
        for i in ticked:
            st = steps[i]
            if not st.now:
                continue
            try:
                wl.place(st.now.id, st.saved, st.rect, desktops)
                moved.append(st)
            except subprocess.CalledProcessError:
                failed.append(st)
        GLib.idle_add(self._restored, len(moved), failed, unstarted)

    def _restored(self, done, failed, unstarted=()):
        self.busy = False
        self.replan()
        text = f"Put back {windows(done)}."
        if failed:
            text += "  Could not move: " + ", ".join(
                wl.app_name(st.saved) for st in failed) + "."
        if unstarted:
            text += "  Could not start: " + ", ".join(
                sorted({wl.app_name(w) for w in unstarted})) + "."
        missing = sorted({wl.app_name(st.saved) for st in self.steps
                          if not st.now})
        if missing:
            text += (f"  Not open: {', '.join(missing)} -- open them and "
                     f"Restore again.")
        self.status.set_text(text)
        self.present()
        return False


class App(Gtk.Application):
    def __init__(self, unique: bool = True):
        #  Unique: a second launch (a double-click on the icon) raises the
        #  picker already open.  Tests make several in one process, which
        #  an application id would refuse.
        super().__init__(application_id=APP_ID if unique else None)

    def do_activate(self):
        #  One picker: a second launch raises the first.
        window = self.get_active_window() or Picker(self)
        window.show_all()
        window.present()


def main() -> int:
    GLib.set_prgname(wl.OWN_CLASS)                 # WM_CLASS: never restored
    GLib.set_application_name("Window Layouts")
    return App().run(None)


if __name__ == "__main__":
    raise SystemExit(main())
