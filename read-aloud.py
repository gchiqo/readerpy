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
import queue
import re
import signal
import subprocess
import sys
import threading
import unicodedata

import piper_stream

HERE = os.path.dirname(os.path.abspath(__file__))
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
CODEY_RE = re.compile(r"[_(){}\[\]<>/\\=+*&|^~`@#]|::|->|=>")
KEEP_PUNCT = set("'.,!?;:%$-")

# Separators inside dotted / colon-joined identifiers (CLAUDE.md, .env,
# config.v2, key:value) become a space. Left as-is they derail espeak's
# phonemiser and the whole utterance garbles. Decimals (3.14), digit:digit
# times/ratios (3:30) and sentence periods followed by a space are untouched.
_DOT_BETWEEN_WORDS = re.compile(r"(?<=[A-Za-z])\.(?=[A-Za-z0-9])")  # a.b, file.ext
_LEADING_DOT = re.compile(r"(?<!\S)\.(?=[A-Za-z])")                 # .env
_COLON_ID = re.compile(r"(?<=[A-Za-z]):(?=\w)|(?<=\w):(?=[A-Za-z])")


def normalize_identifiers(s):
    s = _DOT_BETWEEN_WORDS.sub(" ", s)
    s = _LEADING_DOT.sub(" ", s)
    s = _COLON_ID.sub(" ", s)
    return s


def strip_pictographs(s):
    """Emoji, dingbats and other pictographs (Unicode category So) -> space.

    espeak reads them by their Unicode names ("party popper", "white heavy
    check mark"), which is just noise. Currency (€ £) and math (∞ →) are
    read sensibly by espeak here, so those are kept.
    """
    return "".join(" " if unicodedata.category(c) == "So" else c for c in s)


def _has_letter_or_digit(s):
    return any(c.isalpha() or c.isdigit() for c in s)


def clean_token(tok):
    # Long alnum mash (hash / id / base64) \u2014 Latin-specific by design; a
    # Georgian/Cyrillic block wouldn't match either regex.
    ascii_alnum = re.sub(r"[^A-Za-z0-9]", "", tok)
    if len(ascii_alnum) >= 15 and re.search(r"[0-9]", ascii_alnum) and re.search(r"[A-Za-z]", ascii_alnum):
        return ""
    if re.match(r"^[A-Za-z0-9_\-]{30,}$", tok):
        return ""

    # Count "bad" chars: anything that isn't a Unicode letter/digit or an
    # allowed punctuation mark. Georgian/Cyrillic/CJK letters count as
    # letters here, so they no longer trip is_codey.
    symbol_count = sum(
        1 for c in tok
        if not (c.isalpha() or c.isdigit()) and c not in KEEP_PUNCT
    )
    is_codey = (
        bool(CODEY_RE.search(tok))
        or bool(re.search(r"[a-z][A-Z]", tok))          # camelCase
        or bool(re.search(r"[A-Z]{2,}[a-z]", tok))      # HTMLParser
        or symbol_count >= 2
    )
    if not is_codey:
        return tok

    t = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", tok)
    t = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", t)
    t = re.sub(r"_+", " ", t)
    t = re.sub(r"\.", " ", t)
    # Strip anything that's not a letter (ANY script), digit, or space.
    t = "".join(c if (c.isalpha() or c.isdigit() or c.isspace()) else " " for c in t)
    t = re.sub(r"\s+", " ", t).strip()
    if not _has_letter_or_digit(t):
        return ""
    if re.match(r"^[A-Za-z0-9]{25,}$", re.sub(r"\s", "", t)):
        return ""
    return t


def clean_for_speech(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = CTRL_RE.sub("", s)
    s = strip_pictographs(s)
    s = URL_RE.sub(" ", s)
    s = WWW_RE.sub(" ", s)
    s = EMAIL_RE.sub(
        lambda m: re.sub(r"[._%+-]", " ", m.group(1)) + " at " + re.sub(r"\.", " dot ", m.group(2)),
        s,
    )
    s = re.sub(r"\be\.g\.", "for example", s, flags=re.I)
    s = re.sub(r"\bi\.e\.", "that is", s, flags=re.I)
    s = normalize_identifiers(s)  # after URLs/emails so those aren't broken first
    s = RULE_RE.sub(" ", s)
    s = DASHES_RE.sub(" ", s)
    s = TOKEN_RE.sub(lambda m: clean_token(m.group(0)), s)
    s = s.replace("&", " and ")
    s = re.sub(r"[{}<>\[\]\\|`~^]", " ", s)
    s = re.sub(r"\s*/\s*", " ", s)
    s = re.sub(r"(^|\s)[!?;:,.]+(?=\s|$)", r"\1", s)  # space-isolated punctuation
    s = re.sub(r"\s+", " ", s).strip()
    if not _has_letter_or_digit(s):
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


# Clause boundaries. The cleaner strips most of these marks, so without a
# split the clauses reach Piper glued into one run-on utterance, and dense
# run-ons (identifiers, emails, acronyms) collapse into "bla bla" mid-way,
# even fairly short ones (verified by transcribing Piper's output). Splits:
# after ':' or ';', around a parenthetical, at ', ' (so 1,000 stays intact),
# at any em/en dash, and at a spaced hyphen.
CLAUSE_RE = re.compile(
    r"(?<=[:;])\s+|\s+(?=[(\[])|(?<=[)\]])\s+|,\s+|\s*[\u2012-\u2015]+\s*|\s+-+\s+"
)


def split_clauses(sent):
    return [p.strip() for p in CLAUSE_RE.split(sent) if p.strip()] or [sent]


# What garbles Piper is a dense utterance, not merely a long one. So after
# splitting at every clause boundary, adjacent clauses are greedily repacked
# (rejoined with commas, so the pauses stay) up to this many characters:
# ordinary comma-rich prose becomes one or two natural utterances, while dense
# clauses are long enough that they stay separate.
PACK_LIMIT = 90


def pack_clauses(clauses):
    out, cur = [], ""
    for clause in clauses:
        if not cur:
            cur = clause
        elif len(cur) + 2 + len(clause) <= PACK_LIMIT:
            cur = cur.rstrip(",;:") + ", " + clause
        else:
            out.append(cur)
            cur = clause
    if cur:
        out.append(cur)
    return out


def break_long(s, max_len):
    s = s.strip()
    if len(s) <= max_len:
        return [s] if s else []
    out = []
    while len(s) > max_len:
        window = s[:max_len]
        # Prefer the last comma-ish break in the back half, else the last space.
        cut = -1
        for m in re.finditer(r"[,;:)\]]\s", window):
            if m.end() > max_len // 2:
                cut = m.end()
        if cut <= 0:
            cut = window.rfind(" ") + 1
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
    """Split raw text into bounded, clause-sized chunks, clean each, and
    return them newline-joined (one Piper utterance per line).

    Clauses are cleaned before packing so the pack limit sees the length Piper
    actually gets (info@x.ge expands to "info at x dot ge")."""
    lines = []
    for block in re.split(r"\n+", raw):
        block = block.strip()
        if not block:
            continue
        for sent in split_sentences(block):
            clauses = [c for c in map(clean_for_speech, split_clauses(sent)) if c]
            for utterance in pack_clauses(clauses):
                lines.extend(break_long(utterance, MAX_CHUNK))
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

def speak(text, voice, length_scale, sentence_silence=0.2, volume=None):
    voices = discover_voices()
    vid = pick_voice(voices, voice)
    if not vid:
        sys.exit("no voices found in " + VOICES_DIR)
    info = voices[vid]

    if not piper_stream.player_cmd(info["rate"], volume):
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

    # Open the player first so a Bluetooth sink wakes on lead-in silence while
    # Piper is still loading its model.
    player = piper_stream.Player(info["rate"], volume)
    lines = [ln for ln in text.split("\n") if ln.strip()]
    for run_info, run_lines in piper_stream.voice_runs(lines, voices, vid):
        if player.dead:
            break
        _stream_piper(player, run_info, "\n".join(run_lines), length_scale, sentence_silence)
    player.finish()
    remove_lock()


def _stream_piper(player, info, text, length_scale, sentence_silence):
    """Synthesise `text` with one Piper process and stream it into `player`."""
    piper = subprocess.Popen(
        piper_stream.piper_cmd(info, length_scale, sentence_silence, "--output_raw"),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env=piper_stream.piper_env(),
    )

    def feed():
        try:
            piper.stdin.write((text + "\n").encode("utf-8"))
            piper.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    chunks = queue.Queue()

    def pull():
        while True:
            data = os.read(piper.stdout.fileno(), 8192)
            chunks.put(data or None)
            if not data:
                return

    threading.Thread(target=feed, daemon=True).start()
    threading.Thread(target=pull, daemon=True).start()

    odd = b""  # a pipe read can split a sample across two chunks
    while not player.dead:
        try:
            data = chunks.get(timeout=0.02)
        except queue.Empty:
            player.keep_fed()  # Piper is between sentences: pause, don't glitch
            continue
        if data is None:
            break
        data = odd + data
        odd, data = (data[-1:], data[:-1]) if len(data) % 2 else (b"", data)
        player.write(data)

    try:
        piper.wait(timeout=2)
    except subprocess.TimeoutExpired:
        piper.kill()


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

    if cfg.get("reading_mode"):
        overlay = os.path.join(HERE, "reader_overlay.py")
        if os.path.exists(overlay):
            cmd = ["python3", overlay,
                   "--speed", str(speed),
                   "--pause", str(sentence_silence)]
            if voice:
                cmd += ["--voice", voice]
            if volume is not None:
                cmd += ["--volume", str(volume)]
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            try:
                proc.stdin.write(text.encode("utf-8"))
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            return

    speak(text, voice, length_scale, sentence_silence, volume)


if __name__ == "__main__":
    main()
