"""Piper synthesis + raw playback shared by read-aloud.py and reader_overlay.py.

Three things here exist to stop the "starts late" and "bla bla bubbling"
problems:

* Lead-in / tail-out silence. A Bluetooth headset suspends its audio link
  when idle; waking it takes a moment during which the first audio is
  garbled or dropped, and closing it discards whatever is still buffered.
  So the player opens immediately on LEAD_IN seconds of silence (which
  overlaps Piper's model load, so it costs no wall time) and ends on
  TAIL_OUT seconds of silence.

* Never let the player starve. Piper can run close to real time on a laptop
  CPU with a "high" voice. If pw-play's stdin runs dry mid-stream it xruns
  and the output glitches. Player.keep_fed() pads with silence whenever the
  queued audio drops below LOW_WATER, so a slow sentence becomes a pause
  instead of noise. Because we never starve it, written-bytes / rate is also
  an accurate clock for the overlay's word highlighting.

* Lower noise_scale / noise_w than Piper's defaults (0.667 / 0.8). noise_w
  especially drives the random mumbling: Piper samples fresh noise every
  run, so the same text occasionally renders garbled.

All three are tunable in ~/.config/piper-read/config.json:
  noise_scale, noise_w, lead_in_silence, tail_out_silence
"""

import json
import os
import shutil
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PIPER_DIR = os.path.join(HERE, "piper")
PIPER_BIN = os.path.join(PIPER_DIR, "piper")
ESPEAK_DATA = os.path.join(PIPER_DIR, "espeak-ng-data")
CONFIG = os.path.join(os.path.expanduser("~"), ".config", "piper-read", "config.json")

NOISE_SCALE = 0.5
NOISE_W = 0.4
LEAD_IN = 0.8
TAIL_OUT = 0.5
LOW_WATER = 0.25  # seconds of queued audio below which we pad with silence
PAD = 0.05        # size of each silence pad


def _tunable(key, fallback, lo=0.0, hi=2.0):
    try:
        with open(CONFIG) as fh:
            value = json.load(fh).get(key)
        return fallback if value is None else max(lo, min(hi, float(value)))
    except (OSError, ValueError, TypeError, AttributeError):
        return fallback


def piper_env():
    env = dict(os.environ)
    prev = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = PIPER_DIR + (os.pathsep + prev if prev else "")
    return env


def piper_cmd(info, length_scale, sentence_silence, *extra):
    return [PIPER_BIN, "-m", info["model"], "-c", info["config"],
            "--espeak_data", ESPEAK_DATA, "-q",
            "--length_scale", f"{length_scale}",
            "--sentence_silence", f"{sentence_silence}",
            "--noise_scale", f"{_tunable('noise_scale', NOISE_SCALE)}",
            "--noise_w", f"{_tunable('noise_w', NOISE_W)}",
            *extra]


def player_cmd(rate, volume=None):
    """A player that takes raw s16le mono PCM on stdin."""
    if shutil.which("pw-play"):
        cmd = ["pw-play", "--raw", "--format", "s16", "--rate", str(rate), "--channels", "1"]
        if volume is not None:
            cmd += ["--volume", f"{volume}"]
        return cmd + ["-"]
    if shutil.which("aplay"):
        return ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-c", "1", "-r", str(rate), "-"]
    if shutil.which("ffplay"):
        vol = ["-volume", str(int(max(0.0, min(2.0, volume if volume is not None else 1.0)) * 100))]
        return ["ffplay", "-hide_banner", "-loglevel", "quiet", "-nodisp", "-autoexit",
                *vol, "-f", "s16le", "-ar", str(rate), "-ch_layout", "mono", "-i", "-"]
    return None


class Player:
    """A raw-PCM player process plus a clock of how much audio it was given.

    Opening it starts the lead-in silence right away. Feed PCM with write(),
    call keep_fed() whenever you are waiting on Piper, and finish() at the end.
    """

    def __init__(self, rate, volume=None):
        cmd = player_cmd(rate, volume)
        if not cmd:
            raise RuntimeError("no audio player found (need pw-play, aplay or ffplay)")
        self.rate = rate
        self.bytes_per_sec = rate * 2
        self.written = 0
        self.dead = False
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        self.t0 = time.monotonic()
        self.write_silence(_tunable("lead_in_silence", LEAD_IN))

    def position(self):
        """Seconds of audio played so far (approximate)."""
        return time.monotonic() - self.t0

    def offset(self):
        """Seconds into the stream at which the next write will be heard."""
        return self.written / self.bytes_per_sec

    def write(self, pcm):
        if self.dead or not pcm:
            return
        try:
            self.proc.stdin.write(pcm)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            self.dead = True
            return
        self.written += len(pcm)

    def write_silence(self, secs):
        self.write(b"\x00\x00" * int(secs * self.rate))

    def keep_fed(self):
        """Pad with a little silence if the player is about to run dry."""
        if self.offset() - self.position() < LOW_WATER:
            self.write_silence(PAD)

    def finish(self):
        self.write_silence(_tunable("tail_out_silence", TAIL_OUT))
        try:
            self.proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        self.proc.wait()

    def kill(self):
        self.dead = True
        try:
            self.proc.terminate()
        except OSError:
            pass
