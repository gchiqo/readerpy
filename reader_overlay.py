#!/usr/bin/env python3
"""Fullscreen 'reading mode' overlay for Piper Read Aloud.

A semi-transparent fullscreen GTK window that shows the text in large font
and highlights each word as it is spoken (karaoke style). Dismiss with
Escape, q, space, or a mouse click.

  reader_overlay.py --voice ID --speed N --volume V --pause P [--text "..."]
  (text is read from stdin if --text isn't given)

This script is normally spawned by read-aloud.py when 'reading_mode' is on
in ~/.config/piper-read/config.json. Piper doesn't emit word-time alignment,
so each line is synthesised to its own WAV (one Piper process, --json-input),
played as soon as it is ready, and its words' timings are estimated from
their character lengths over that line's actual duration. Speaking starts
after the first line, not after the whole selection.
"""

import argparse
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import wave

import piper_stream

# Force the X11/XWayland backend BEFORE importing gi.
# Reason: on Wayland, gtk_window_move() is a no-op (the compositor owns
# window position), so we can't actually park the small mode in the
# bottom-right corner. XWayland gives us X11 semantics, where move() works,
# and Mutter still composites the surface with proper per-pixel alpha for
# our wildcard-CSS + cairo-CLEAR-then-OVER background paint.
os.environ["GDK_BACKEND"] = "x11"

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
VOICES_DIR = os.path.join(HERE, "voices")
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "piper-read")
LOCK = os.path.join(CACHE_DIR, "play.pgid")
PREFERRED_VOICE = "en_US-libritts_r-medium"

# Size of the "small" windowed mode (bottom-right corner). Big mode is fullscreen.
SMALL_W, SMALL_H = 520, 220
SMALL_MARGIN = 28  # distance from screen edge


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
    BG = (0.10, 0.12, 0.18, 0.55)  # slightly lighter so transparency is visible if it works
    COLOR_PAST = "#bbbbbb"
    COLOR_FUTURE = "#777777"
    COLOR_CURRENT = "#ffd54f"
    FONT_BIG = "Sans 32"

    def __init__(self, text, voice_info, length_scale, sentence_silence, volume, tmpdir):
        # Normal top-level window so Alt+Tab can find it. Undecorated because
        # we draw our own close button. set_keep_above is best-effort: works
        # on X11, may be ignored on Wayland (the WM controls stacking there).
        Gtk.Window.__init__(self, type=Gtk.WindowType.TOPLEVEL)
        self.set_title("Piper Reading")
        self.set_decorated(False)
        self.set_keep_above(True)
        # transparency
        self.set_app_paintable(True)
        screen = self.get_screen()
        visual = screen.get_rgba_visual() if screen else None
        if visual:
            self.set_visual(visual)

        # Force every relevant Adwaita/theme-painted surface to transparent.
        # The key selector is `window.background` — that's what Adwaita uses
        # for the window's fill, so plain `window` loses on specificity.
        # `decoration` kills the client-side shadow/border GTK still draws
        # even with set_decorated(False).
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(
            # Wildcard nuke of widget backgrounds, plus explicit selectors
            # for things Adwaita styles aggressively.
            b"* { background-color: transparent; background-image: none; } "
            b"window, window.background, window.solid-csd, "
            b"scrolledwindow, scrolledwindow viewport, "
            b"box, eventbox, overlay, label { "
            b"  background-color: transparent; "
            b"  background-image: none; "
            b"} "
            b"decoration, window decoration { "
            b"  background: transparent; box-shadow: none; "
            b"  border: none; margin: 0; "
            b"} "
            b"button.piper-x { "
            b"  background-color: rgba(0,0,0,0.45); color: #fff; "
            b"  border-radius: 18px; padding: 0; font-weight: bold; "
            b"  border: none; box-shadow: none; "
            b"} "
            b"button.piper-x:hover { background-color: rgba(255,80,80,0.85); }"
        )
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_USER,  # user (800) beats theme (200)
        )


        self.connect("draw", self._on_draw)
        self.connect("key-press-event", self._on_key)
        self.connect("destroy", self._on_destroy)

        # Track whether we're fullscreen ("big") or shrunken to the corner.
        # main() will fullscreen() us on startup, so start with True.
        self.is_big = True

        self.label = Gtk.Label()
        self.label.set_line_wrap(True)
        self.label.set_justify(Gtk.Justification.CENTER)
        self.label.set_xalign(0.5)
        self.label.set_max_width_chars(60)
        self.label.set_margin_start(80)
        self.label.set_margin_end(80)

        # Vertical-centering trick that also handles overflow gracefully:
        # two vexpand spacers around the label split the leftover viewport
        # height evenly, so short text sits centred. When the label's natural
        # height exceeds the viewport the spacers collapse to zero and the
        # ScrolledWindow's vertical scrollbar takes over. We also auto-scroll
        # proportional to reading progress (see _maybe_autoscroll) so the
        # current word stays in view.
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        inner.set_margin_top(60)
        inner.set_margin_bottom(60)
        top_pad = Gtk.Box(); top_pad.set_vexpand(True)
        bot_pad = Gtk.Box(); bot_pad.set_vexpand(True)
        inner.pack_start(top_pad, True, True, 0)
        inner.pack_start(self.label, False, False, 0)
        inner.pack_start(bot_pad, True, True, 0)

        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        try:
            self.scroll.set_overlay_scrolling(True)
        except AttributeError:
            pass
        self.scroll.add(inner)

        # EventBox so we can catch clicks on the (otherwise non-input) label
        # area and use them to toggle big/small mode. The X button (added
        # via Gtk.Overlay below) sits above and gets its own click first.
        self.click_target = Gtk.EventBox()
        self.click_target.add(self.scroll)
        self.click_target.connect("button-press-event", self._on_toggle_click)

        # Close button overlaid in the top-right corner.
        self.x_button = Gtk.Button(label="✕")
        self.x_button.set_halign(Gtk.Align.END)
        self.x_button.set_valign(Gtk.Align.START)
        self.x_button.set_margin_top(12)
        self.x_button.set_margin_end(12)
        self.x_button.set_size_request(34, 34)
        self.x_button.set_relief(Gtk.ReliefStyle.NONE)
        self.x_button.set_can_focus(False)
        self.x_button.set_tooltip_text("Close (Esc)")
        self.x_button.get_style_context().add_class("piper-x")
        self.x_button.connect("clicked", lambda *_: self._dismiss())

        self.olay = Gtk.Overlay()
        self.olay.add(self.click_target)
        self.olay.add_overlay(self.x_button)
        self.add(self.olay)

        self.text = text
        self.lines = [ln for ln in text.split("\n") if ln.strip()]
        self.words = text.split()
        self.voice_info = voice_info
        self.length_scale = length_scale
        self.sentence_silence = sentence_silence
        self.volume = volume
        self.tmpdir = tmpdir
        self.current = -1
        self.player = None
        self.piper = None
        self.stopped = False
        self.failed = False
        # (start, end) per word in stream seconds; None until its line is
        # synthesised. Filled by the feeder thread, read by _tick.
        self.timings = [None] * len(self.words)
        self.audio_end = None
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
        # Clear surface fully transparent first, then paint our semi-transparent
        # colour over. More robust than OPERATOR_SOURCE alone on Wayland.
        cr.set_operator(0)  # CLEAR
        cr.paint()
        cr.set_operator(2)  # OVER
        cr.set_source_rgba(*self.BG)
        cr.paint()
        return False

    # ---- input ----
    def _on_key(self, _w, ev):
        # Escape and q close the window. (Space removed — too easy to bump.)
        if ev.keyval in (Gdk.KEY_Escape, Gdk.KEY_q, Gdk.KEY_Q):
            self._dismiss()
        return False

    def _on_toggle_click(self, _w, event):
        """Click on the text area toggles between fullscreen and a small
        bottom-right window. The X button has its own handler."""
        if event.type == Gdk.EventType.BUTTON_PRESS and event.button == 1:
            self._toggle_size()
            return True
        return False

    def _toggle_size(self):
        """Toggle between fullscreen-sized window and a small bottom-right
        corner window. We never enter actual fullscreen state (Mutter forces
        opacity on those for direct-scanout). The hide/reconfigure/show
        cycle is required because Mutter ignores in-place resize/move after
        a couple of cycles."""
        geom = self._monitor_geom()
        if self.is_big:
            tgt_w, tgt_h = SMALL_W, SMALL_H
            if geom is not None:
                tgt_x = geom.x + geom.width - SMALL_W - SMALL_MARGIN
                tgt_y = geom.y + geom.height - SMALL_H - SMALL_MARGIN
            else:
                tgt_x, tgt_y = 100, 100
            self.is_big = False
        else:
            if geom is not None:
                tgt_w, tgt_h = geom.width, geom.height
                tgt_x, tgt_y = geom.x, geom.y
            else:
                tgt_w, tgt_h, tgt_x, tgt_y = 1920, 1080, 0, 0
            self.is_big = True

        self.hide()
        self.set_position(Gtk.WindowPosition.NONE)
        self.resize(tgt_w, tgt_h)
        self.move(tgt_x, tgt_y)
        self.show()
        self.set_keep_above(True)
        self.present()

    def _monitor_geom(self):
        screen = self.get_screen()
        if screen is None:
            return None
        display = screen.get_display()
        monitor = None
        try:
            gdkwin = self.get_window()
            monitor = (display.get_monitor_at_window(gdkwin)
                       if gdkwin is not None else None)
        except Exception:
            monitor = None
        if monitor is None:
            try:
                monitor = display.get_primary_monitor()
            except Exception:
                monitor = None
        return monitor.get_geometry() if monitor else None

    def _on_destroy(self, *_):
        self._cleanup()
        Gtk.main_quit()

    def _dismiss(self):
        try:
            self.destroy()
        except Exception:
            pass

    def _cleanup(self):
        self.stopped = True
        if self.player:
            self.player.kill()
        if self.piper:
            try: self.piper.terminate()
            except Exception: pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        try:
            with open(LOCK) as fh:
                pgid = int(fh.read().strip())
            if pgid == os.getpgrp():
                os.unlink(LOCK)
        except (OSError, ValueError):
            pass

    # ---- synthesis & playback ----
    def start(self):
        try:
            # Opened now so a Bluetooth sink wakes on the lead-in silence
            # while Piper loads its model.
            self.player = piper_stream.Player(self.voice_info["rate"], self.volume)
        except (RuntimeError, OSError):
            self._show_failure()
            return
        threading.Thread(target=self._synth_and_play, daemon=True).start()
        GLib.timeout_add(40, self._tick)

    def _synth_and_play(self):
        """One Piper process writes each line to its own WAV and prints the
        path when it is done. Each line is queued to the player as soon as it
        arrives; while waiting on Piper, the player is padded with silence."""
        player = self.player
        paths = [os.path.join(self.tmpdir, f"{k:05d}.wav") for k in range(len(self.lines))]
        self.piper = subprocess.Popen(
            piper_stream.piper_cmd(self.voice_info, self.length_scale, self.sentence_silence,
                                   "--json-input", "--output_dir", self.tmpdir),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=piper_stream.piper_env(),
        )

        def feed():
            try:
                for line, path in zip(self.lines, paths):
                    self.piper.stdin.write(
                        (json.dumps({"text": line, "output_file": path}) + "\n").encode("utf-8"))
                self.piper.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

        done = queue.Queue()

        def pull():
            for _ in self.piper.stdout:  # Piper prints each finished path, in order
                done.put(True)
            done.put(False)

        threading.Thread(target=feed, daemon=True).start()
        threading.Thread(target=pull, daemon=True).start()

        word = 0
        for line, path in zip(self.lines, paths):
            while True:
                if self.stopped or player.dead:
                    return
                try:
                    ok = done.get(timeout=0.02)
                    break
                except queue.Empty:
                    player.keep_fed()
            if not ok:
                break  # Piper exited early
            try:
                with wave.open(path, "rb") as w:
                    pcm = w.readframes(w.getnframes())
                os.unlink(path)
            except (OSError, wave.Error, EOFError):
                pcm = b""
            n = len(line.split())
            start = player.offset()
            dur = len(pcm) / float(player.bytes_per_sec)
            for i, (s, e) in enumerate(word_timings(self.words[word:word + n], dur)):
                self.timings[word + i] = (start + s, start + e)
            word += n
            player.write(pcm)

        if word == 0:
            self.failed = True
        self.audio_end = player.offset()
        player.finish()

    def _show_failure(self):
        self.label.set_markup(
            "<span font='Sans 22' foreground='#f88'>Synthesis failed.</span>")

    def _tick(self):
        if self.stopped:
            return False
        if self.failed:
            self._show_failure()
            return False
        elapsed = self.player.position()
        idx = -1
        for i, t in enumerate(self.timings):
            if t is None or elapsed < t[0]:
                break
            idx = i
        if idx != self.current:
            self.current = idx
            self._render(idx)
            self._maybe_autoscroll(idx)
        if self.audio_end is not None and elapsed > self.audio_end + 0.4:
            self._dismiss()
            return False
        return True

    def _maybe_autoscroll(self, idx):
        """If the text overflows the viewport, slide the scroll position
        proportional to reading progress so the current word stays in view."""
        if idx < 0 or len(self.words) <= 1 or self.scroll is None:
            return
        adj = self.scroll.get_vadjustment()
        if adj is None:
            return
        upper = adj.get_upper() - adj.get_page_size()
        if upper <= 1:
            return  # content fits, no scroll needed
        frac = idx / max(1, len(self.words) - 1)
        target = max(0.0, min(float(upper), frac * upper))
        # ease toward target so the scroll isn't jumpy (tick is 40 ms)
        current = adj.get_value()
        adj.set_value(current + (target - current) * 0.3)


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

    tmpdir = tempfile.mkdtemp(prefix="overlay-", dir=CACHE_DIR)

    def on_sig(*_):
        shutil.rmtree(tmpdir, ignore_errors=True)
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

    win = Overlay(text, voices[vid], length_scale, args.pause, args.volume, tmpdir)
    # Cover the primary monitor without calling fullscreen(): Mutter on
    # Wayland forces fullscreen surfaces opaque for direct-scanout, killing
    # our per-pixel alpha. Plain geometry keeps the surface compositable.
    screen = win.get_screen()
    geom = None
    try:
        disp = screen.get_display() if screen else None
        mon = disp.get_primary_monitor() if disp else None
        if mon:
            geom = mon.get_geometry()
    except Exception:
        geom = None
    if geom:
        win.set_default_size(geom.width, geom.height)
        win.show_all()
        win.move(geom.x, geom.y)
    else:
        win.set_default_size(1920, 1080)
        win.show_all()
        win.move(0, 0)
    win.start()
    Gtk.main()


if __name__ == "__main__":
    main()
