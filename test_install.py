"""Tests for install.sh.  `python3 -m unittest -v` in this folder.

Each test installs into a throwaway HOME, with `systemctl` and `gio`
replaced by stubs that write down how they were called -- the real user
session, its timer and its desktop are never touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATES = ["window-layouts.desktop", "window-layout-restore.desktop",
             "window-layout-snapshot.service", "window-layout-snapshot.timer"]
STUB = """#!/bin/sh
echo "$(basename "$0") $*" >> "$STUB_LOG"
case "$*" in
    *show-environment*) echo DISPLAY=:0 ;;
esac
exit 0
"""


class Install(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home)
        self.stubs = self.home / "stubs"
        self.stubs.mkdir()
        for name in ("systemctl", "gio"):
            stub = self.stubs / name
            stub.write_text(STUB)
            stub.chmod(0o755)
        self.log = self.home / "stub.log"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("XDG_") and k != "BINDIR"}
        self.env.update(HOME=str(self.home), STUB_LOG=str(self.log),
                        PATH=f"{self.stubs}:{os.environ['PATH']}")
        self.bin = self.home / ".local/bin"
        self.apps = self.home / ".local/share/applications"
        self.units = self.home / ".config/systemd/user"

    def run_sh(self, *args, env=None):
        return subprocess.run([str(HERE / "install.sh"), *args],
                              env=env or self.env, capture_output=True,
                              text=True, cwd=self.home)

    def calls(self):
        return self.log.read_text() if self.log.exists() else ""

    def desktop(self):
        """Where install.sh puts the desktop icon in this HOME."""
        out = subprocess.run(["xdg-user-dir", "DESKTOP"], env=self.env,
                             capture_output=True, text=True).stdout.strip() \
            if shutil.which("xdg-user-dir") else ""
        return Path(out) if out and out != str(self.home) else \
            self.home / "Desktop"

    def test_the_templates_name_no_ones_home(self):
        for name in TEMPLATES:
            text = (HERE / name).read_text()
            self.assertNotIn("/home/", text, name)
            if name != "window-layout-snapshot.timer":
                self.assertIn("@BIN@", text, name)

    def test_install_writes_this_users_paths(self):
        got = self.run_sh()
        self.assertEqual(got.returncode, 0, got.stderr + got.stdout)
        for cmd, target in (("window-layout", "window_layout.py"),
                            ("window-layouts", "window_layout_gui.py")):
            link = self.bin / cmd
            self.assertTrue(link.is_symlink(), cmd)
            self.assertEqual(link.resolve(), HERE / target)
        icon = self.home / ".local/share/icons/hicolor/scalable/apps/window-layout.svg"
        self.assertEqual(icon.resolve(), HERE / "window-layout.svg")
        written = [self.apps / "window-layouts.desktop",
                   self.apps / "window-layout-restore.desktop",
                   self.units / "window-layout-snapshot.service",
                   self.units / "window-layout-snapshot.timer",
                   self.desktop() / "window-layouts.desktop"]
        for path in written:
            text = path.read_text()
            self.assertNotIn("@BIN@", text, path)
        self.assertIn(f"Exec={self.bin}/window-layout restore --open --notify",
                      (self.apps / "window-layout-restore.desktop").read_text())
        self.assertIn(f"ExecStart={self.bin}/window-layout snapshot",
                      (self.units / "window-layout-snapshot.service").read_text())
        self.assertTrue(os.access(self.desktop() / "window-layouts.desktop",
                                  os.X_OK))
        self.assertIn("systemctl --user enable --now window-layout-snapshot.timer",
                      self.calls())
        self.assertIn("metadata::trusted true", self.calls())

    def test_what_it_writes_runs_and_validates(self):
        self.assertEqual(self.run_sh().returncode, 0)
        #  The service's command, as the timer would run it.
        got = subprocess.run([str(self.bin / "window-layout"), "--help"],
                             capture_output=True, text=True)
        self.assertEqual(got.returncode, 0, got.stderr)
        if shutil.which("desktop-file-validate"):
            for name in ("window-layouts.desktop", "window-layout-restore.desktop"):
                v = subprocess.run(["desktop-file-validate", str(self.apps / name)],
                                   capture_output=True, text=True)
                self.assertEqual(v.returncode, 0, v.stdout + v.stderr)

    def test_bindir_and_xdg_folders_are_honoured(self):
        env = dict(self.env, BINDIR=str(self.home / "tools"),
                   XDG_CONFIG_HOME=str(self.home / "cfg"),
                   XDG_DATA_HOME=str(self.home / "data"))
        self.assertEqual(self.run_sh("--no-desktop-icon", env=env).returncode, 0)
        self.assertTrue((self.home / "tools/window-layout").is_symlink())
        self.assertIn(f"ExecStart={self.home}/tools/window-layout snapshot",
                      (self.home / "cfg/systemd/user/window-layout-snapshot.service")
                      .read_text())
        self.assertIn(f"Exec={self.home}/tools/window-layouts",
                      (self.home / "data/applications/window-layouts.desktop")
                      .read_text())
        self.assertFalse((self.desktop() / "window-layouts.desktop").exists())

    def test_running_it_twice_is_harmless(self):
        self.assertEqual(self.run_sh().returncode, 0)
        before = (self.units / "window-layout-snapshot.service").read_text()
        got = self.run_sh()
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual((self.units / "window-layout-snapshot.service").read_text(),
                         before)

    def test_uninstall_keeps_snapshots_unless_purged(self):
        self.assertEqual(self.run_sh().returncode, 0)
        snaps = self.home / ".local/state/window-layout/snapshots"
        snaps.mkdir(parents=True)
        (snaps / "20261118-120000.json").write_text("{}")
        foreign = self.bin / "something-else"
        foreign.write_text("mine")
        got = self.run_sh("--uninstall")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("disable --now window-layout-snapshot.timer", self.calls())
        for gone in (self.bin / "window-layout", self.bin / "window-layouts",
                     self.apps / "window-layouts.desktop",
                     self.apps / "window-layout-restore.desktop",
                     self.units / "window-layout-snapshot.service",
                     self.units / "window-layout-snapshot.timer",
                     self.desktop() / "window-layouts.desktop"):
            self.assertFalse(gone.exists() or gone.is_symlink(), gone)
        self.assertTrue(foreign.exists())
        self.assertTrue(snaps.exists())
        self.assertIn("snapshots are kept", got.stdout)
        self.assertEqual(self.run_sh("--uninstall", "--purge").returncode, 0)
        self.assertFalse(snaps.exists())

    def test_uninstall_leaves_a_window_layout_that_isnt_ours(self):
        self.bin.mkdir(parents=True)
        (self.bin / "window-layout").write_text("someone else's")
        self.assertEqual(self.run_sh("--uninstall").returncode, 0)
        self.assertTrue((self.bin / "window-layout").exists())

    def test_missing_tools_are_named_with_how_to_get_them(self):
        #  A PATH with what the script itself needs, but no wmctrl.
        tools = self.home / "tools"
        tools.mkdir()
        for name in ("sh", "dirname", "sed", "python3", "basename", "cat"):
            found = shutil.which(name)
            if found:
                (tools / name).symlink_to(found)
        env = dict(self.env, PATH=f"{self.stubs}:{tools}")
        got = self.run_sh(env=env)
        self.assertEqual(got.returncode, 1)
        self.assertIn("wmctrl", got.stdout)
        self.assertIn("sudo apt install", got.stdout)
        self.assertFalse((self.bin / "window-layout").exists())

    def test_help_and_unknown_options(self):
        got = self.run_sh("--help")
        self.assertEqual(got.returncode, 0)
        self.assertIn("--uninstall", got.stdout)
        self.assertNotIn("set -eu", got.stdout)
        self.assertEqual(self.run_sh("--bogus").returncode, 2)


if __name__ == "__main__":
    unittest.main()
