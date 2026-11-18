#!/bin/sh
# Install window-layout for the user running this: the commands, the menu
# entries, a desktop icon, and the snapshot timer.  Run it again after
# moving the checkout, or to repair an install; it's safe to repeat.
#
#   ./install.sh                    install (or update) and start the timer
#   ./install.sh --no-desktop-icon  ...without the icon on the desktop
#   ./install.sh --uninstall        remove it all; snapshots are kept
#   ./install.sh --uninstall --purge   ...and delete the snapshots too
#
# Where things go follows the XDG variables, with the usual defaults:
#   commands     $BINDIR (default ~/.local/bin), symlinked to this checkout
#   menu         $XDG_DATA_HOME/applications
#   icon         $XDG_DATA_HOME/icons/hicolor/scalable/apps
#   timer        $XDG_CONFIG_HOME/systemd/user
#   desktop      $(xdg-user-dir DESKTOP), else ~/Desktop
#   snapshots    $XDG_STATE_HOME/window-layout

set -eu

HERE=$(cd "$(dirname "$0")" && pwd -P)
BIN=${BINDIR:-$HOME/.local/bin}
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
CONFIG=${XDG_CONFIG_HOME:-$HOME/.config}
STATE=${XDG_STATE_HOME:-$HOME/.local/state}
APPS=$DATA/applications
ICONS=$DATA/icons/hicolor
UNITS=$CONFIG/systemd/user
TIMER=window-layout-snapshot.timer
MENU="window-layouts.desktop window-layout-restore.desktop"
UNITFILES="window-layout-snapshot.service $TIMER"

desktop_dir() {
    d=$(xdg-user-dir DESKTOP 2>/dev/null || true)
    #  xdg-user-dir answers $HOME when no desktop folder is configured.
    if [ -n "$d" ] && [ "$d" != "$HOME" ]; then echo "$d"; else echo "$HOME/Desktop"; fi
}

have() { command -v "$1" >/dev/null 2>&1; }
say() { echo "window-layout: $*"; }

uninstall=0 purge=0 icon=1
for arg in "$@"; do
    case $arg in
        --uninstall) uninstall=1 ;;
        --purge) purge=1 ;;
        --no-desktop-icon) icon=0 ;;
        -h|--help) sed -n '2,19s/^# \{0,1\}//p' "$0"; exit 0 ;;
        *) echo "install.sh: unknown option $arg (try --help)" >&2; exit 2 ;;
    esac
done

if [ $uninstall = 1 ]; then
    if have systemctl; then
        systemctl --user disable --now "$TIMER" 2>/dev/null || true
    fi
    for f in $UNITFILES; do rm -f "$UNITS/$f"; done
    have systemctl && systemctl --user daemon-reload 2>/dev/null || true
    for f in $MENU; do rm -f "$APPS/$f"; done
    rm -f "$(desktop_dir)/window-layouts.desktop"
    rm -f "$ICONS/scalable/apps/window-layout.svg"
    #  Only our own links: a `window-layout` that isn't one stays.
    for f in window-layout window-layouts; do
        if [ -L "$BIN/$f" ]; then rm -f "$BIN/$f"; fi
    done
    have update-desktop-database && update-desktop-database -q "$APPS" 2>/dev/null || true
    if [ $purge = 1 ]; then
        rm -rf "$STATE/window-layout"
        say "removed, and the snapshots with it"
    else
        say "removed; snapshots are kept in $STATE/window-layout (--purge deletes them)"
    fi
    exit 0
fi

# -- What it needs ----------------------------------------------------------

missing=""
for tool in python3 wmctrl xprop xdotool xrandr pgrep; do
    have "$tool" || missing="$missing $tool"
done
if have python3 && ! python3 -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk' 2>/dev/null; then
    missing="$missing python3-gi(GTK3)"
fi
if [ -n "$missing" ]; then
    say "missing:$missing"
    say "on Debian, Ubuntu or Mint: sudo apt install python3 python3-gi gir1.2-gtk-3.0 wmctrl x11-utils xdotool x11-xserver-utils procps libnotify-bin"
    exit 1
fi
have notify-send || say "note: notify-send not found; the menu's one-click restore won't say how it went (libnotify-bin)"

# -- Install ----------------------------------------------------------------

mkdir -p "$BIN" "$APPS" "$ICONS/scalable/apps" "$UNITS"
chmod +x "$HERE/window_layout.py" "$HERE/window_layout_gui.py"
ln -sfn "$HERE/window_layout.py" "$BIN/window-layout"
ln -sfn "$HERE/window_layout_gui.py" "$BIN/window-layouts"
ln -sfn "$HERE/window-layout.svg" "$ICONS/scalable/apps/window-layout.svg"

# The templates in this checkout name @BIN@; written out, it's $BIN.
render() {   # template destination
    sed "s|@BIN@|$BIN|g" "$HERE/$1" > "$2.tmp" && mv "$2.tmp" "$2"
}
for f in $MENU; do render "$f" "$APPS/$f"; done
for f in $UNITFILES; do render "$f" "$UNITS/$f"; done

if [ $icon = 1 ]; then
    desk=$(desktop_dir)
    mkdir -p "$desk"
    render window-layouts.desktop "$desk/window-layouts.desktop"
    chmod +x "$desk/window-layouts.desktop"
    #  Caja and Nautilus run a trusted launcher without asking first.
    have gio && gio set "$desk/window-layouts.desktop" metadata::trusted true 2>/dev/null || true
fi

have gtk-update-icon-cache && gtk-update-icon-cache -q -t "$ICONS" 2>/dev/null || true
have update-desktop-database && update-desktop-database -q "$APPS" 2>/dev/null || true

if have systemctl && systemctl --user daemon-reload 2>/dev/null; then
    systemctl --user enable --now "$TIMER"
    #  The snapshot runs as a user service, which reads windows through the
    #  user manager's DISPLAY.  Most desktops hand it over at login.
    if ! systemctl --user show-environment 2>/dev/null | grep -q '^DISPLAY='; then
        if [ -n "${DISPLAY:-}" ]; then
            systemctl --user import-environment DISPLAY XAUTHORITY 2>/dev/null || true
        fi
        say "note: the systemd user manager had no DISPLAY; if snapshots never"
        say "appear, add 'systemctl --user import-environment DISPLAY XAUTHORITY'"
        say "to your session's startup programs"
    fi
else
    say "note: no systemd user session, so no timer; run '$BIN/window-layout snapshot'"
    say "every couple of minutes some other way (cron, your session's autostart)"
fi

case ":$PATH:" in
    *":$BIN:"*) ;;
    *) say "note: $BIN isn't on your PATH; the menu entries work anyway, but to type"
       say "'window-layout' in a terminal, add it (most ~/.profile files do at next login)" ;;
esac

say "installed: commands in $BIN, menu entries under Accessories, timer running"
say "try: window-layout snapshot -v && window-layout list"
