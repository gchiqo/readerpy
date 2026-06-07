#!/usr/bin/env python3
"""System-wide read-aloud for Linux/X11 using Piper neural voices.

Press the hotkey (Super+Shift+A) to read the current X11 PRIMARY selection
(i.e. whatever text is highlighted in any app). Press again to stop.

  read-aloud.py            read selection / toggle stop if already speaking
  read-aloud.py --stop     stop any current playback
  read-aloud.py --text "…" read the given text (handy for testing)
  read-aloud.py --voice ID --speed 1.2
  read-aloud.py --list-voices

Code and symbols are cleaned before speaking so they don't "bubble":
code blocks of symbols, URLs and hashes are dropped, identifiers like
getUserName / foo_bar become real words.
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
PIPER_DIR = os.path.join(HERE, "piper")
PIPER_BIN = os.path.join(PIPER_DIR, "piper")
ESPEAK_DATA = os.path.join(PIPER_DIR, "espeak-ng-data")
VOICES_DIR = os.path.join(HERE, "voices")

CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "piper-read")
LOCK = os.path.join(CACHE_DIR, "play.pgid")
CONFIG = os.path.join(os.path.expanduser("~"), ".config", "piper-read", "config.json")

PREFERRED_VOICE = "en_US-ryan-high"

# ---------------------------------------------------------------------------
# Speech cleaning  (mirrors the logic that worked well in testing)
# ---------------------------------------------------------------------------

ABBREV = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc",
    "e.g", "i.e", "fig", "no", "vol", "pp", "al", "inc", "ltd", "co",
    "dept", "est", "min", "max", "approx", "ca", "cf", "ed", "eds",
}

CTRL_RE = re.compile(
    "[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F\u200B-\u200F\u2028\u2029\uFEFF]"
)
URL_RE = re.compile(r"\bhttps?://[^\s)]+", re.I)
WWW_RE = re.compile(r"\bwww\.[^\s)]+", re.I)
EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
RULE_RE = re.compile(r"[=_*~`#>|^]{2,}")
DASHES_RE = re.compile(r"-{2,}")
TOKEN_RE = re.compile(r"\S+")
SYMBOL_BAD_RE = re.compile(r"[^A-Za-z0-9'.,!?;:%$\u00C0-\u024F-]")
CODEY_RE = re.compile(r"[_(){}\[\]<>/\\=+*&|^~`@#]|::|->|=>")


def clean_token(tok):
    alnum = re.sub(r"[^A-Za-z0-9]", "", tok)
    if len(alnum) >= 15 and re.search(r"[0-9]", alnum) and re.search(r"[A-Za-z]", alnum):
        return ""  # hash / id / base64
    if re.match(r"^[A-Za-z0-9_\-]{30,}$", tok):
        return ""

    symbol_count = len(SYMBOL_BAD_RE.findall(tok))
    is_codey = (
        bool(CODEY_RE.search(tok))
        or bool(re.search(r"[a-z][A-Z]", tok))
        or bool(re.search(r"[A-Z]{2,}[a-z]", tok))
        or symbol_count >= 2
    )
    if not is_codey:
        return tok

    t = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", tok)       # camelCase
    t = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", t)      # HTMLParser -> HTML Parser
    t = re.sub(r"_+", " ", t)
    t = re.sub(r"\.", " ", t)
    t = re.sub(r"[^A-Za-z0-9 \u00C0-\u024F]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not re.search(r"[A-Za-z0-9]", t):
        return ""
    if re.match(r"^[A-Za-z0-9]{25,}$", re.sub(r"\s", "", t)):
        return ""
    return t


def clean_for_speech(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = CTRL_RE.sub("", s)
    s = URL_RE.sub(" ", s)
    s = WWW_RE.sub(" ", s)
    s = EMAIL_RE.sub(
        lambda m: re.sub(r"[._%+-]", " ", m.group(1)) + " at " + re.sub(r"\.", " dot ", m.group(2)),
        s,
    )
    s = RULE_RE.sub(" ", s)
    s = DASHES_RE.sub(" ", s)
    s = TOKEN_RE.sub(lambda m: clean_token(m.group(0)), s)
    s = s.replace("&", " and ")
    s = re.sub(r"[{}<>\[\]\\|`~^]", " ", s)
    s = re.sub(r"\s*/\s*", " ", s)
    s = re.sub(r"(^|\s)[!?;:,.]+(?=\s|$)", r"\1", s)  # space-isolated punctuation
    s = re.sub(r"\s+", " ", s).strip()
    if not re.search(r"[A-Za-z0-9\u00C0-\u024F]", s):
        return ""
    return s


# ---------------------------------------------------------------------------
# Chunking: keep each utterance short so Piper's model stays stable.
# A single huge utterance (e.g. a long selection with no sentence breaks)
# makes the neural voice garble / "bubble". We split on sentences + newlines
# and cap length, then feed Piper one chunk per line.
# ---------------------------------------------------------------------------

MAX_CHUNK = 160


def split_sentences(text):
    out = []
    n = len(text)
    start = 0
    i = 0
    while i < n:
        c = text[i]
        if c in ".!?":
            j = i
            while j + 1 < n and text[j + 1] in ".!?":
                j += 1
            nxt = text[j + 1] if j + 1 < n else ""
            if nxt == "" or nxt.isspace() or nxt in ')]"\u201D':
                if c == "." and i > 0 and text[i - 1].isdigit() and nxt.isdigit():
                    i = j + 1
                    continue  # decimal like 3.14
                if c == ".":
                    m = re.search(r"([A-Za-z.]+)$", text[:i])
                    if m:
                        w = m.group(1).lower().rstrip(".")
                        if w in ABBREV or len(w) == 1:
                            i = j + 1
                            continue  # abbreviation
                seg = text[start:j + 1].strip()
                if seg:
                    out.append(seg)
                start = j + 1
                i = j + 1
                continue
        i += 1
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def break_long(s, max_len):
    s = s.strip()
    if len(s) <= max_len:
        return [s] if s else []
    out = []
    while len(s) > max_len:
        window = s[:max_len]
        cut = -1
        for m in re.finditer(r"[,;:)\]]\s|\s", window):
            cut = m.end()
        if cut <= 0:
            cut = max_len
        piece = s[:cut].strip()
        if piece:
            out.append(piece)
        s = s[cut:]
    s = s.strip()
    if s:
        out.append(s)
    return out


def prepare_text(raw):
    """Split raw text into bounded, sentence-sized chunks, clean each, and
    return them newline-joined (one Piper utterance per line)."""
    lines = []
    for block in re.split(r"\n+", raw):
        block = block.strip()
        if not block:
            continue
        for sent in split_sentences(block):
            for chunk in break_long(sent, MAX_CHUNK):
                spoken = clean_for_speech(chunk)
                if spoken:
                    lines.append(spoken)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Voices / config
# ---------------------------------------------------------------------------

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


def load_config():
    try:
        with open(CONFIG) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as fh:
        json.dump(cfg, fh, indent=2)


def pick_voice(voices, requested):
    if requested and requested in voices:
        return requested
    if PREFERRED_VOICE in voices:
        return PREFERRED_VOICE
    return next(iter(voices)) if voices else None


# ---------------------------------------------------------------------------
# Playback toggle / lock
# ---------------------------------------------------------------------------

def read_lock():
    try:
        with open(LOCK) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def remove_lock():
    try:
        os.remove(LOCK)
    except OSError:
        pass


def stop_running():
    """Kill the currently-speaking process group, if any. Returns True if one
    was alive (i.e. this call acted as a 'stop')."""
    pgid = read_lock()
    alive = False
    if pgid:
        try:
            os.killpg(pgid, 0)
            alive = True
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            alive = False
    remove_lock()
    return alive


# ---------------------------------------------------------------------------
# Synthesis + streaming playback
# ---------------------------------------------------------------------------

def player_cmd(rate, volume=None):
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


def speak(text, voice, length_scale, sentence_silence=0.2, volume=None):
    voices = discover_voices()
    vid = pick_voice(voices, voice)
    if not vid:
        sys.exit("no voices found in " + VOICES_DIR)
    info = voices[vid]

    player = player_cmd(info["rate"], volume)
    if not player:
        sys.exit("no audio player found (need pw-play, aplay or ffplay)")

    os.makedirs(CACHE_DIR, exist_ok=True)
    os.setpgrp()  # isolate into our own process group so we can be stopped cleanly
    pgid = os.getpgrp()
    with open(LOCK, "w") as fh:
        fh.write(str(pgid))

    def cleanup(*_):
        remove_lock()
        os._exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    env = dict(os.environ)
    prev = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = PIPER_DIR + (os.pathsep + prev if prev else "")

    piper = subprocess.Popen(
        [PIPER_BIN, "-m", info["model"], "-c", info["config"],
         "--espeak_data", ESPEAK_DATA, "--output_raw", "-q",
         "--length_scale", f"{length_scale}",
         "--sentence_silence", f"{sentence_silence}"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env=env,
    )
    play = subprocess.Popen(player, stdin=piper.stdout)
    piper.stdout.close()  # let piper get SIGPIPE if the player goes away

    try:
        piper.stdin.write((text + "\n").encode("utf-8"))
        piper.stdin.close()
    except BrokenPipeError:
        pass

    play.wait()
    try:
        piper.wait(timeout=2)
    except subprocess.TimeoutExpired:
        piper.kill()
    remove_lock()


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def get_selection():
    """Return the current PRIMARY selection.

    Wayland (GNOME on Wayland) doesn't expose the X11 PRIMARY selection to
    GTK in a way we can grab from a non-Wayland-native app, so on a Wayland
    session we shell out to `wl-paste --primary`. On X11 we use GTK.
    """
    if os.environ.get("WAYLAND_DISPLAY"):
        try:
            r = subprocess.run(
                ["wl-paste", "--primary", "--no-newline"],
                capture_output=True, timeout=2,
            )
            return r.stdout.decode("utf-8", "replace")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ""
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk, Gdk
        clip = Gtk.Clipboard.get(Gdk.SELECTION_PRIMARY)
        return clip.wait_for_text() or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------

def adjust_speed(cfg, set_to=None, delta=0.0):
    cur = float(cfg.get("speed", 1.0))
    if set_to is not None:
        cur = set_to
    cur = max(0.5, min(5.0, round(cur + delta, 2)))
    cfg["speed"] = cur
    save_config(cfg)
    return cur


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--text")
    ap.add_argument("--voice")
    ap.add_argument("--speed", type=float)
    ap.add_argument("--set-speed", type=float, help="persist this speed and exit")
    ap.add_argument("--faster", action="store_true", help="+0.1 speed (persisted)")
    ap.add_argument("--slower", action="store_true", help="-0.1 speed (persisted)")
    ap.add_argument("--set-voice", help="persist this voice as default and exit")
    ap.add_argument("--list-voices", action="store_true")
    args = ap.parse_args()

    if args.list_voices:
        for vid in discover_voices():
            print(vid)
        return

    if args.stop:
        stop_running()
        return

    cfg = load_config()
    voice = args.voice or cfg.get("voice")

    if args.set_voice is not None:
        cfg["voice"] = args.set_voice
        save_config(cfg)
        print(f"default voice -> {args.set_voice}")
        return

    def clamp(v, lo, hi):
        try:
            return max(lo, min(hi, float(v)))
        except (TypeError, ValueError):
            return None

    volume = clamp(cfg.get("volume"), 0.0, 2.0)
    sentence_silence = clamp(cfg.get("sentence_silence"), 0.0, 2.0)
    if sentence_silence is None:
        sentence_silence = 0.2

    if args.set_speed is not None or args.faster or args.slower:
        new = adjust_speed(cfg, set_to=args.set_speed,
                           delta=(0.1 if args.faster else 0.0) - (0.1 if args.slower else 0.0))
        ls = max(0.2, min(3.0, 1.0 /new))
        stop_running()  # don't overlap any current read
        speak(f"speed {new:.1f}", voice, ls, sentence_silence, volume)  # spoken confirmation
        return

    speed = args.speed or cfg.get("speed") or 1.0
    length_scale = max(0.2, min(3.0, 1.0 /speed))

    if args.text is not None:
        text = prepare_text(args.text)
        if text:
            speak(text, voice, length_scale, sentence_silence, volume)
        return

    # default action: toggle. If something is speaking, stop it; else read.
    if stop_running():
        return
    text = prepare_text(get_selection())
    if not text:
        return
    speak(text, voice, length_scale, sentence_silence, volume)


if __name__ == "__main__":
    main()
