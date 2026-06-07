#!/usr/bin/env python3
"""Register (or remove) the GNOME custom keyboard shortcut for read-aloud.

  setup-hotkey.py            bind Super+Shift+A (with a Georgian-layout alt) -> read-aloud.py
  setup-hotkey.py --remove   remove both bindings
  setup-hotkey.py --key '<Super>a'           use a different primary shortcut
  setup-hotkey.py --alt-key '<Super><Shift>Georgian_an'   secondary shortcut for another layout
  setup-hotkey.py --no-alt   don't install the secondary alt binding

GNOME custom shortcuts bind to keysyms produced AFTER the active keyboard
layout is applied. With a Georgian (or other non-US) layout active,
'<Super><Shift>a' produces a Georgian letter and the shortcut won't fire.
The alt binding solves that by also matching the Georgian 'A' key.
"""

import ast
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COMMAND = f"python3 {os.path.join(HERE, 'read-aloud.py')}"

SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
LIST_KEY = "custom-keybindings"

PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/piper-read/"
ENTRY_SCHEMA = f"org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:{PATH}"
NAME = "Piper Read Aloud"
DEFAULT_KEY = "<Super><Shift>a"

ALT_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/piper-read-alt/"
ALT_ENTRY_SCHEMA = f"org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:{ALT_PATH}"
ALT_NAME = "Piper Read Aloud (alt layout)"
DEFAULT_ALT_KEY = "<Super><Shift>Georgian_an"


def gget(schema, key):
    return subprocess.check_output(["gsettings", "get", schema, key], text=True).strip()


def gset(schema, key, value):
    subprocess.check_call(["gsettings", "set", schema, key, value])


def current_list():
    raw = gget(SCHEMA, LIST_KEY)
    if raw.startswith("@as"):
        raw = raw[3:].strip()
    try:
        val = ast.literal_eval(raw)
        return [str(x) for x in val]
    except (ValueError, SyntaxError):
        return []


def _ensure_paths(paths_wanted):
    paths = current_list()
    changed = False
    for p in paths_wanted:
        if p not in paths:
            paths.append(p)
            changed = True
    if changed:
        gset(SCHEMA, LIST_KEY, str(paths))


def _set_entry(entry_schema, name, key):
    gset(entry_schema, "name", name)
    gset(entry_schema, "command", COMMAND)
    gset(entry_schema, "binding", key)


def install(key, alt_key=None):
    wanted = [PATH] + ([ALT_PATH] if alt_key else [])
    _ensure_paths(wanted)
    _set_entry(ENTRY_SCHEMA, NAME, key)
    print(f"Bound {key}  ->  {COMMAND}")
    if alt_key:
        _set_entry(ALT_ENTRY_SCHEMA, ALT_NAME, alt_key)
        print(f"Also bound {alt_key} for non-US layouts")
    print("Select any text, then press the shortcut. Press again to stop.")


def remove():
    paths = [p for p in current_list() if p not in (PATH, ALT_PATH)]
    gset(SCHEMA, LIST_KEY, str(paths))
    for entry in (ENTRY_SCHEMA, ALT_ENTRY_SCHEMA):
        try:
            subprocess.check_call(["gsettings", "reset-recursively", entry])
        except subprocess.CalledProcessError:
            pass
    print("Removed the Piper Read Aloud shortcut(s).")


def _arg(args, name, default):
    if name in args:
        try:
            return args[args.index(name) + 1]
        except IndexError:
            return default
    return default


def main():
    args = sys.argv[1:]
    if "--remove" in args:
        remove()
        return
    key = _arg(args, "--key", DEFAULT_KEY)
    alt_key = None if "--no-alt" in args else _arg(args, "--alt-key", DEFAULT_ALT_KEY)
    install(key, alt_key)


if __name__ == "__main__":
    main()
