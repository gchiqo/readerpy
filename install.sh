#!/usr/bin/env bash
# Installs the "Piper Read Aloud" settings app into your GNOME Apps menu.
# Run again any time (e.g. after moving the folder) to refresh the path.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
APPS="$HOME/.local/share/applications"
DESKTOP="$APPS/piper-read.desktop"

mkdir -p "$APPS"
cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Piper Read Aloud
GenericName=Text to Speech
Comment=Read selected text aloud with Piper neural voices
Exec=python3 $DIR/piper-read-gui.py
Icon=$DIR/icon.svg
Terminal=false
Categories=Utility;Accessibility;
Keywords=tts;speech;read;aloud;piper;voice;
StartupNotify=true
EOF
chmod +x "$DESKTOP" 2>/dev/null || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null || true

# Make sure the global read-aloud shortcut is bound too.
python3 "$DIR/setup-hotkey.py" >/dev/null 2>&1 || true

echo "Installed: $DESKTOP"
echo "Open your Apps menu and search for 'Piper Read Aloud'."
