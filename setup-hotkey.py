#!/usr/bin/env python3
"""Register (or remove) the GNOME custom keyboard shortcut for read-aloud.

  setup-hotkey.py            bind Super+Shift+A -> read-aloud.py
  setup-hotkey.py --remove   remove the binding
  setup-hotkey.py --key '<Super>a'   use a different shortcut

Works on GNOME (X11 or Wayland) via gsettings; no extra packages needed.
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


def install(key):
    paths = current_list()
    if PATH not in paths:
        paths.append(PATH)
        gset(SCHEMA, LIST_KEY, str(paths))
    gset(ENTRY_SCHEMA, "name", NAME)
    gset(ENTRY_SCHEMA, "command", COMMAND)
    gset(ENTRY_SCHEMA, "binding", key)
    print(f"Bound {key}  ->  {COMMAND}")
    print("Select any text, then press the shortcut. Press again to stop.")


def remove():
    paths = [p for p in current_list() if p != PATH]
    gset(SCHEMA, LIST_KEY, str(paths))
    try:
        subprocess.check_call(["gsettings", "reset-recursively", ENTRY_SCHEMA])
    except subprocess.CalledProcessError:
        pass
    print("Removed the Piper Read Aloud shortcut.")


def main():
    args = sys.argv[1:]
    if "--remove" in args:
        remove()
        return
    key = DEFAULT_KEY
    if "--key" in args:
        key = args[args.index("--key") + 1]
    install(key)


if __name__ == "__main__":
    main()
