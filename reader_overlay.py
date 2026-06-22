#!/usr/bin/env python3
"""Fullscreen 'reading mode' overlay for Piper Read Aloud.

A semi-transparent fullscreen GTK window that shows the text in large font
and highlights each word as it is spoken (karaoke style). Dismiss with
Escape, q, space, or a mouse click.

  reader_overlay.py --voice ID --speed N --volume V --pause P [--text "..."]
  (text is read from stdin if --text isn't given)

This script is normally spawned by read-aloud.py when 'reading_mode' is on
in ~/.config/piper-read/config.json. Piper doesn't emit word-time alignment,
so we synthesise the whole utterance first, then estimate per-word timings
from each word's character length over the audio's actual duration.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave

import cairo  # for the empty-region click-through input shape
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PIPER_DIR = os.path.join(HERE, "piper")
PIPER_BIN = os.path.join(PIPER_DIR, "piper")
ESPEAK_DATA = os.path.join(PIPER_DIR, "espeak-ng-data")
VOICES_DIR = os.path.join(HERE, "voices")
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "piper-read")
LOCK = os.path.join(CACHE_DIR, "play.pgid")
PREFERRED_VOICE = "en_US-libritts_r-medium"


# ---- voice discovery -----------------------------------------------------

def discover_voices():
    voices = {}
    if not os.path.isdir(VOICES_DIR):
        return voices
    for fn in sorted(os.listdir(VOICES_DIR)):
        if not fn.endswith(".onnx"):
            continue
        model = os.path.join(VOICES_DIR, fn)
        cfg = model + ".json"
        if not os.path.exists(cfg):
            continue
        vid = fn[:-len(".onnx")]
        rate = 22050
        try:
            with open(cfg) as fh:
                rate = json.load(fh).get("audio", {}).get("sample_rate", 22050)
        except (OSError, ValueError):
            pass
        voices[vid] = {"model": model, "config": cfg, "rate": rate}
    return voices


def pick_voice(voices, requested):
    if requested and requested in voices:
        return requested
    if PREFERRED_VOICE in voices:
        return PREFERRED_VOICE
    return next(iter(voices)) if voices else None


# ---- synthesis + playback helpers ----------------------------------------

def synth_to_wav(text, voice_info, length_scale, sentence_silence):
    out = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    env = dict(os.environ)
    prev = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = PIPER_DIR + (os.pathsep + prev if prev else "")
    cmd = [PIPER_BIN, "-m", voice_info["model"], "-c", voice_info["config"],
           "--espeak_data", ESPEAK_DATA, "-f", out, "-q",
           "--length_scale", f"{length_scale}",
           "--sentence_silence", f"{sentence_silence}"]
    proc = subprocess.run(cmd, input=text.encode("utf-8"),
                          env=env, stderr=subprocess.DEVNULL)
    if proc.returncode != 0 or not os.path.exists(out):
        try: os.unlink(out)
        except OSError: pass
        return None, 0.0
    try:
        with wave.open(out, "rb") as w:
            dur = w.getnframes() / float(w.getframerate())
    except (wave.Error, EOFError):
        dur = 0.0
    return out, dur


def player_cmd(volume):
    if shutil.which("pw-play"):
        cmd = ["pw-play"]
        if volume is not None:
            cmd += ["--volume", f"{volume}"]
        return cmd
    if shutil.which("aplay"):
        return ["aplay", "-q"]
    if shutil.which("ffplay"):
        return ["ffplay", "-hide_banner", "-loglevel", "quiet", "-nodisp", "-autoexit"]
    return None


# ---- word timing estimator -----------------------------------------------

def word_timings(words, total_duration):
    """Estimate (start, end) seconds for each word based on its char length
    and trailing punctuation. Piper doesn't expose alignments, so this is
    approximate but usually within ~150 ms per word."""
    if not words or total_duration <= 0:
        return []
    weights = []
    for w in words:
        base = max(2, len(w))
        if w and w[-1] in ",;:":
            base += 3
        elif w and w[-1] in ".!?":
            base += 5
        weights.append(base)
    tot = sum(weights)
    out, t = [], 0.0
    for wt in weights:
        dur = total_duration * wt / tot
        out.append((t, t + dur))
        t += dur
    return out


# ---- the overlay window --------------------------------------------------

class Overlay(Gtk.Window):
    BG = (0.05, 0.07, 0.12, 0.5)
    COLOR_PAST = "#bbbbbb"
    COLOR_FUTURE = "#777777"
    COLOR_CURRENT = "#ffd54f"
    FONT_BIG = "Sans 32"

    def __init__(self, text, voice_info, length_scale, sentence_silence, volume):
        Gtk.Window.__init__(self, type=Gtk.WindowType.TOPLEVEL)
        self.set_title("Piper Reading")
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_decorated(False)

        # Click-through, non-focus-stealing overlay. NOTIFICATION type-hint is
        # the canonical choice on Mutter/X11 for transient on-top surfaces that
        # don't reserve workarea (unlike DOCK). set_accept_focus(False) +
        # set_focus_on_map(False) keep the underlying window focused so the
        # user can keep typing/clicking in their app.
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_resizable(False)
        self.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        self.connect("realize", self._on_realize)

        # transparency: try to get an RGBA visual (works on X11 with a
        # compositor; on Wayland the compositor handles it natively)
        self.set_app_paintable(True)
        screen = self.get_screen()
        visual = screen.get_rgba_visual() if screen else None
        if visual:
            self.set_visual(visual)

        self.connect("draw", self._on_draw)
        self.connect("key-press-event", self._on_key)
        self.connect("button-press-event", self._on_click)
        self.connect("destroy", self._on_destroy)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.KEY_PRESS_MASK)

        self.label = Gtk.Label()
        self.label.set_line_wrap(True)
        self.label.set_justify(Gtk.Justification.CENTER)
        self.label.set_xalign(0.5)
        self.label.set_yalign(0.5)
        self.label.set_max_width_chars(60)
        self.label.set_margin_top(60)
        self.label.set_margin_bottom(60)
        self.label.set_margin_start(80)
        self.label.set_margin_end(80)
        self.add(self.label)

        self.text = text
        self.words = text.split()
        self.voice_info = voice_info
        self.length_scale = length_scale
        self.sentence_silence = sentence_silence
        self.volume = volume
        self.current = -1
        self.player = None
        self.wav_path = None
        self.timings = []
        self.start_time = None
        self._render(-1)

    # ---- rendering ----
    def _render(self, idx):
        parts = []
        for i, w in enumerate(self.words):
            esc = GLib.markup_escape_text(w)
            if i == idx:
                parts.append(
                    f"<span foreground='{self.COLOR_CURRENT}' weight='bold'>{esc}</span>")
            elif i < idx:
                parts.append(f"<span foreground='{self.COLOR_PAST}'>{esc}</span>")
            else:
                parts.append(f"<span foreground='{self.COLOR_FUTURE}'>{esc}</span>")
        self.label.set_markup(f"<span font='{self.FONT_BIG}'>{' '.join(parts)}</span>")

    def _on_draw(self, _w, cr):
        cr.set_source_rgba(*self.BG)
        cr.set_operator(1)  # cairo OPERATOR_SOURCE
        cr.paint()
        return False

    def _on_realize(self, *_):
        # Make the overlay click-through: an EMPTY cairo.Region() = no clickable
        # pixels, so all pointer events fall through to whatever is underneath.
        # Important: pass cairo.Region(), NOT None (None removes the shape and
        # the whole window becomes clickable again).
        # Must run after realize because get_window() returns None in __init__.
        win = self.get_window()
        if win is not None:
            try:
                win.input_shape_combine_region(cairo.Region(), 0, 0)
            except Exception:
                pass

    # ---- input ----
    # Once click-through is active (X11), neither button-press nor key-press
    # signals fire on this window — the handlers below are no-ops in normal
    # operation. They stay only as a safety net for the (rare) backends
    # where input-shaping doesn't take effect (e.g. some Wayland sessions).
    def _on_key(self, _w, ev):
        if ev.keyval in (Gdk.KEY_Escape, Gdk.KEY_q, Gdk.KEY_Q, Gdk.KEY_space):
            self._dismiss()
        return False

    def _on_click(self, *_):
        self._dismiss()
        return False

    def _on_destroy(self, *_):
        self._cleanup()
        Gtk.main_quit()

    def _dismiss(self):
        try:
            self.destroy()
        except Exception:
            pass

    def _cleanup(self):
        if self.player:
            try: self.player.terminate()
            except Exception: pass
            self.player = None
        if self.wav_path and os.path.exists(self.wav_path):
            try: os.unlink(self.wav_path)
            except OSError: pass
        try:
            with open(LOCK) as fh:
                pgid = int(fh.read().strip())
            if pgid == os.getpgrp():
                os.unlink(LOCK)
        except (OSError, ValueError):
            pass

    # ---- synthesis & playback ----
    def start(self):
        threading.Thread(target=self._synth_in_bg, daemon=True).start()

    def _synth_in_bg(self):
        wav, dur = synth_to_wav(self.text, self.voice_info,
                                self.length_scale, self.sentence_silence)
        GLib.idle_add(self._begin_playback, wav, dur)

    def _begin_playback(self, wav, dur):
        if not wav:
            self.label.set_markup(
                "<span font='Sans 22' foreground='#f88'>Synthesis failed.</span>")
            return False
        self.wav_path = wav
        self.timings = word_timings(self.words, dur)
        cmd = player_cmd(self.volume)
        if cmd:
            try:
                self.player = subprocess.Popen(cmd + [wav])
            except OSError:
                self.player = None
        self.start_time = time.monotonic()
        GLib.timeout_add(40, self._tick)
        return False

    def _tick(self):
        if not self.timings or self.start_time is None:
            return False
        elapsed = time.monotonic() - self.start_time
        idx = -1
        for i, (s, _e) in enumerate(self.timings):
            if elapsed >= s:
                idx = i
            else:
                break
        if idx != self.current:
            self.current = idx
            self._render(idx)
        if elapsed > self.timings[-1][1] + 0.4:
            self._dismiss()
            return False
        return True


# ---- main ---------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--volume", type=float, default=None)
    ap.add_argument("--pause", type=float, default=0.2)
    ap.add_argument("--text")
    args = ap.parse_args()

    text = (args.text if args.text is not None else sys.stdin.read()).strip()
    if not text:
        return

    voices = discover_voices()
    vid = pick_voice(voices, args.voice)
    if not vid:
        sys.exit("no voices found")

    length_scale = max(0.2, min(3.0, 1.0 / max(0.1, args.speed)))

    # become our own process group + take the lock so a second Super+Shift+A
    # stops us cleanly via read-aloud.py's stop_running().
    try:
        os.setpgrp()
    except OSError:
        pass
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        with open(LOCK, "w") as fh:
            fh.write(str(os.getpgrp()))
    except OSError:
        pass

    def on_sig(*_):
        try:
            with open(LOCK) as fh:
                pgid = int(fh.read().strip())
            if pgid == os.getpgrp():
                os.unlink(LOCK)
        except (OSError, ValueError):
            pass
        os._exit(0)
    signal.signal(signal.SIGTERM, on_sig)
    signal.signal(signal.SIGINT, on_sig)

    win = Overlay(text, voices[vid], length_scale, args.pause, args.volume)
    # Cover the primary monitor explicitly instead of fullscreen() — the
    # latter triggers focus/activation that interferes with click-through and
    # NOTIFICATION-type hints. Plain geometry leaves the underlying app
    # focused and active.
    screen = win.get_screen()
    geom = None
    try:
        monitor = screen.get_display().get_primary_monitor()
        if monitor:
            geom = monitor.get_geometry()
    except Exception:
        geom = None
    if geom:
        win.set_default_size(geom.width, geom.height)
        win.move(geom.x, geom.y)
    else:
        win.set_default_size(screen.get_width(), screen.get_height())
        win.move(0, 0)
    win.show_all()
    win.start()
    Gtk.main()


if __name__ == "__main__":
    main()
