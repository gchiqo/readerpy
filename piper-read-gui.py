#!/usr/bin/env python3
"""Settings GUI for Piper Read Aloud.

A small GTK window to choose voice / speed / volume / pause and manage the
keyboard shortcut. Reading itself still happens via the global hotkey
(Super+Shift+A) handled by read-aloud.py; this app just writes the same
config file (~/.config/piper-read/config.json) and offers Test/Read/Stop.
"""

import json
import os
import subprocess

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
READER = os.path.join(HERE, "read-aloud.py")
HOTKEY = os.path.join(HERE, "setup-hotkey.py")
CONFIG = os.path.join(os.path.expanduser("~"), ".config", "piper-read", "config.json")

DEFAULTS = {
    "voice": "",
    "speed": 1.0,
    "volume": 1.0,
    "sentence_silence": 0.2,
    "hotkey": "<Super><Shift>a",
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG) as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as fh:
        json.dump(cfg, fh, indent=2)


def list_voices():
    try:
        out = subprocess.check_output(["python3", READER, "--list-voices"], text=True)
        return [ln.strip() for ln in out.splitlines() if ln.strip()]
    except (OSError, subprocess.CalledProcessError):
        return []


def run_reader(*args):
    try:
        subprocess.Popen(["python3", READER, *args])
    except OSError:
        pass


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Piper Read Aloud")
        self.set_border_width(18)
        self.set_default_size(420, -1)
        self.set_resizable(False)
        self.cfg = load_config()
        self._loading = True

        grid = Gtk.Grid(row_spacing=14, column_spacing=14)
        self.add(grid)
        r = 0

        title = Gtk.Label(xalign=0)
        title.set_markup("<b>Piper Read Aloud</b>")
        grid.attach(title, 0, r, 3, 1)
        r += 1
        hint = Gtk.Label(xalign=0)
        hint.set_markup("<small>Select text anywhere and press the shortcut to read it.</small>")
        grid.attach(hint, 0, r, 3, 1)
        r += 1

        # Voice
        grid.attach(self._lbl("Voice"), 0, r, 1, 1)
        self.voice = Gtk.ComboBoxText()
        self.voices = list_voices()
        for v in self.voices:
            self.voice.append_text(v)
        if self.cfg["voice"] in self.voices:
            self.voice.set_active(self.voices.index(self.cfg["voice"]))
        elif self.voices:
            self.voice.set_active(0)
        self.voice.set_hexpand(True)
        self.voice.connect("changed", self.on_change)
        grid.attach(self.voice, 1, r, 2, 1)
        r += 1

        # Speed
        grid.attach(self._lbl("Speed"), 0, r, 1, 1)
        self.speed = self._scale(0.5, 2.5, 0.1, 1)
        self.speed.set_value(float(self.cfg["speed"]))
        for m in (0.5, 1.0, 1.5, 2.0, 2.5):
            self.speed.add_mark(m, Gtk.PositionType.BOTTOM, None)
        self.speed.connect("value-changed", self.on_change)
        grid.attach(self.speed, 1, r, 2, 1)
        r += 1

        # Volume
        grid.attach(self._lbl("Volume"), 0, r, 1, 1)
        self.volume = self._scale(0, 150, 5, 0)
        self.volume.set_value(float(self.cfg["volume"]) * 100)
        self.volume.add_mark(100, Gtk.PositionType.BOTTOM, "100%")
        self.volume.connect("value-changed", self.on_change)
        grid.attach(self.volume, 1, r, 2, 1)
        r += 1

        # Pause between sentences
        grid.attach(self._lbl("Pause (s)"), 0, r, 1, 1)
        self.pause = self._scale(0.0, 1.0, 0.05, 2)
        self.pause.set_value(float(self.cfg["sentence_silence"]))
        self.pause.connect("value-changed", self.on_change)
        grid.attach(self.pause, 1, r, 2, 1)
        r += 1

        # Action buttons
        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, homogeneous=True)
        b_test = Gtk.Button(label="Test voice")
        b_read = Gtk.Button(label="Read selection")
        b_stop = Gtk.Button(label="Stop")
        b_test.connect("clicked", self.on_test)
        b_read.connect("clicked", lambda *_: run_reader())
        b_stop.connect("clicked", lambda *_: run_reader("--stop"))
        for b in (b_test, b_read, b_stop):
            btns.pack_start(b, True, True, 0)
        grid.attach(btns, 0, r, 3, 1)
        r += 1

        # Shortcut
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        grid.attach(sep, 0, r, 3, 1)
        r += 1
        grid.attach(self._lbl("Shortcut"), 0, r, 1, 1)
        self.hotkey = Gtk.Entry()
        self.hotkey.set_text(self.cfg.get("hotkey", "<Super><Shift>a"))
        self.hotkey.set_tooltip_text("GNOME accelerator, e.g. <Super><Shift>a or <Control><Alt>r")
        grid.attach(self.hotkey, 1, r, 1, 1)
        b_apply = Gtk.Button(label="Apply")
        b_apply.connect("clicked", self.on_apply_hotkey)
        grid.attach(b_apply, 2, r, 1, 1)
        r += 1

        self.status = Gtk.Label(xalign=0)
        grid.attach(self.status, 0, r, 3, 1)

        if not self.voices:
            self._flash("No voices found in ./voices — check the install", True)

        self._loading = False
        self.show_all()

    # -- widget helpers --
    def _lbl(self, text):
        lbl = Gtk.Label(label=text, xalign=0)
        return lbl

    def _scale(self, lo, hi, step, digits):
        s = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
        s.set_digits(digits)
        s.set_value_pos(Gtk.PositionType.RIGHT)
        s.set_hexpand(True)
        return s

    def _flash(self, text, error=False):
        color = "#c0392b" if error else "#2e7d32"
        self.status.set_markup(f"<small><span foreground='{color}'>{GLib.markup_escape_text(text)}</span></small>")

    # -- handlers --
    def on_change(self, *_):
        if self._loading:
            return
        self.cfg["voice"] = self.voice.get_active_text() or self.cfg.get("voice", "")
        self.cfg["speed"] = round(self.speed.get_value(), 2)
        self.cfg["volume"] = round(self.volume.get_value() / 100.0, 2)
        self.cfg["sentence_silence"] = round(self.pause.get_value(), 2)
        save_config(self.cfg)
        self._flash("Saved")

    def on_test(self, *_):
        self.on_change()
        run_reader("--text", "This is a test of the selected Piper voice.")

    def on_apply_hotkey(self, *_):
        key = self.hotkey.get_text().strip()
        if not key:
            return
        try:
            subprocess.run(["python3", HOTKEY, "--key", key], check=True)
            self.cfg["hotkey"] = key
            save_config(self.cfg)
            self._flash(f"Shortcut set to {key}")
        except (OSError, subprocess.CalledProcessError) as exc:
            self._flash(f"Could not set shortcut: {exc}", True)


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.piper.readaloud")
        self.win = None

    def do_activate(self):
        if not self.win:
            self.win = Window(self)
        self.win.present()


if __name__ == "__main__":
    App().run(None)
